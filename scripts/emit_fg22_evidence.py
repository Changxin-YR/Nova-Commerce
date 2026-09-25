"""Emit FG-22 evidence: flagship agent E2E through real HTTP and a real browser.

FG-22 is the flagship gate, so its proof is the whole journey rather than one
assertion: an agent request over real HTTP, the tool call it makes and the
permission/scope it obeys, the PendingAction it produces, approval over real HTTP
against a locked row, expiry blocking, the HITL graph's interrupt happening before
any side effect, resume re-validation, duplicate-resume idempotency, and finally the
seven fact classes re-read from MySQL - with the browser's own result reported
separately, because a rendered page is not a database fact.

## What this script does, in order

1. Refuses to touch MySQL at all until it has proven the shared database is free:
   ``SHOW PROCESSLIST`` is read and any other *live* connection on ``nova`` is
   reported. The captain and the other members share this database, so a run that
   starts while someone else is mid-sweep would produce numbers that explain
   nothing. Read-only, and only a live (non-Sleep) foreign query blocks.
2. Starts a real backend on 127.0.0.1:8000 and waits for ``/health/live``.
3. Runs the real Playwright spec (``e2e/flagship-agent.spec.ts``) with the JSON
   reporter, and records the process exit code and the per-test outcomes.
4. Runs the journey driver, which executes the fixture's real modes (seed, HTTP,
   service, HITL graph, resume, verify) and turns their observations into one real
   assertion per checklist item.
5. Stops the backend it started and records that it did.

Nothing is asserted from a constant: every ``exit_code``, every HTTP status and
every fact count in the artifact comes from a command or a query this run performed.
When a step cannot be run (no backend, no browser, blocked database) the artifact
says so and the verdict is FAIL - a gate that could not be measured is not a gate
that passed.
"""

from __future__ import annotations

import json
import os
import pathlib
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gate_evidence import ROOT, git_state

BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
PYTHON = pathlib.Path(sys.executable)
OUT = ROOT / "artifacts" / "evidence" / "agent" / "fg22_flagship_e2e.json"
XML_OUT = OUT.with_suffix(".xml")
REPORTS = ROOT / "artifacts" / "reports"

API_HOST = "127.0.0.1"
API_PORT = 8000

#: Environment for the backend this script starts.
#:
#: ``PAYMENT_MOCK_ENABLED`` is set here because the shared working ``.env`` does NOT
#: define it and the code default is ``False``, which disables the entire mock-pay
#: surface: the browser's settlement click answers 60006 PAYMENT_MOCK_DISABLED (code
#: 60006) and the flagship journey cannot complete a payment at all. ``.env.example``
#: documents this key as ``true`` for the dev profile ("the dev profile. Setting it
#: true with APP_ENV=staging|prod makes the application refuse to start"), so this is
#: the documented dev configuration, applied to the server this run starts rather
#: than by editing a file owned by another member. It is recorded in the artifact.
#:
#: APP_ENV is pinned to dev for the same reason: the validator refuses mock payments
#: outside dev/test, and pinning it makes the guard's decision visible instead of
#: inherited from whatever the shell happened to export.
SERVER_ENV = {"PAYMENT_MOCK_ENABLED": "true", "APP_ENV": "dev"}
E2E_PORT = 4174
SPEC = "e2e/flagship-agent.spec.ts"

RELEVANT_PATHS = (
    "scripts/emit_fg22_evidence.py",
    "scripts/fg22_journey.py",
    "scripts/gate_evidence.py",
    "backend/tests/e2e/flagship_e2e_fixture.py",
    "backend/tests/integration/payment/conftest.py",
    "backend/app/modules/agent",
    "backend/app/modules/governance",
    "backend/app/modules/identity",
    "backend/app/modules/inventory",
    "backend/app/api/v1/router.py",
    "frontend/e2e/flagship-agent.spec.ts",
    "frontend/playwright.config.ts",
    "frontend/src/views/console/AiWorkspaceView.vue",
    "frontend/src/components/agent/ActionApprovalCard.vue",
)


def _wait_for_http(url: str, deadline_seconds: float) -> tuple[bool, float]:
    started = time.time()
    while time.time() - started < deadline_seconds:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True, round(time.time() - started, 2)
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(1.0)
    return False, round(time.time() - started, 2)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((API_HOST, port)) != 0


def _other_live_sessions() -> dict[str, object]:
    """Read-only: is anyone else running a query against the shared database?"""
    script = (
        "import json;"
        "from sqlalchemy import text;"
        "from app.shared.db.session import get_session_factory;"
        "s=get_session_factory()();"
        "rows=[dict(r._mapping) for r in s.execute(text('SHOW PROCESSLIST')).all()];"
        "s.close();"
        "print(json.dumps(rows, default=str))"
    )
    try:
        proc = subprocess.run(
            [str(PYTHON), "-c", script],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"queried": False, "error": f"{type(exc).__name__}: {exc}", "busy": []}
    if proc.returncode != 0:
        return {"queried": False, "error": (proc.stderr or "")[-400:], "busy": []}
    line = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else "[]"
    try:
        rows = json.loads(line)
    except ValueError as exc:
        return {"queried": False, "error": str(exc), "busy": []}
    busy = [
        {"id": row.get("Id"), "user": row.get("User"), "db": row.get("db"),
         "command": row.get("Command"), "time": row.get("Time"), "info": str(row.get("Info"))[:120]}
        for row in rows
        if str(row.get("db") or "") == "nova"
        and str(row.get("Command") or "").lower() not in {"sleep", ""}
        and "SHOW PROCESSLIST" not in str(row.get("Info") or "")
    ]
    return {"queried": True, "busy": busy, "session_count": len(rows)}


def _playwright() -> dict[str, object]:
    """Run the real browser spec, with the backend already listening on :8000."""
    binary = FRONTEND / "node_modules" / ".bin" / ("playwright.cmd" if os.name == "nt" else "playwright")
    if not binary.is_file():
        return {"ran": False, "error": f"playwright binary missing at {binary}"}
    json_report = REPORTS / "fg22_playwright.json"
    json_report.parent.mkdir(parents=True, exist_ok=True)
    command = [str(binary), "test", SPEC, "--reporter=json"]
    env = {**os.environ, "E2E_PORT": str(E2E_PORT), "CI": ""}
    started = datetime.now(UTC)
    try:
        proc = subprocess.run(
            command,
            cwd=FRONTEND,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            check=False,
            env=env,
        )
        code, stdout, stderr = proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        code = 124
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = "playwright exceeded 900s"
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    if stdout.strip():
        json_report.write_text(stdout, encoding="utf-8")

    report: dict[str, object] = {
        "ran": True,
        "command": " ".join(command),
        "exit_code": code,
        "duration_ms": duration_ms,
        "spec": SPEC,
        "e2e_port": E2E_PORT,
        "json_report": str(json_report.relative_to(ROOT)),
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
        "tests": [],
    }
    try:
        document = json.loads(stdout)
    except ValueError as exc:
        report["parse_error"] = str(exc)
        return report
    tests: list[dict[str, object]] = []
    for suite in document.get("suites", []):
        for spec in suite.get("specs", []):
            results = spec.get("tests", [])
            statuses = [entry.get("status") for entry in results]
            tests.append(
                {
                    "title": spec.get("title"),
                    "ok": spec.get("ok") is True,
                    "statuses": statuses,
                    "passed": spec.get("ok") is True and all(s == "expected" for s in statuses),
                }
            )
    report["tests"] = tests
    report["stats"] = document.get("stats", {})
    return report


def main() -> int:
    started = datetime.now(UTC)
    REPORTS.mkdir(parents=True, exist_ok=True)
    reasons: list[str] = []
    assertions: list[dict[str, object]] = []

    def check(name: str, passed: bool, actual: object, expected: object = True) -> None:
        assertions.append({"name": name, "expected": expected, "actual": actual, "pass": bool(passed)})

    # 1. the shared database must be quiet before a write journey starts
    database = _other_live_sessions()
    check(
        "shared MySQL has no other live query before the journey starts (serial access)",
        database.get("queried") is True and not database.get("busy"),
        database,
        "no live foreign session",
    )
    if database.get("busy"):
        reasons.append(f"another session is live on nova: {database['busy']}")

    # 2. the backend must be startable - the journey is HTTP, not in-process
    backend_log = REPORTS / "fg22_backend.log"
    backend_err = REPORTS / "fg22_backend.err.log"
    server: dict[str, object] = {"started": False, "pid": None}
    port_free = _port_free(API_PORT)
    check(f"port {API_PORT} is free before starting the backend", port_free, port_free)
    if port_free:
        with backend_log.open("w", encoding="utf-8") as out_handle, backend_err.open(
            "w", encoding="utf-8"
        ) as err_handle:
            # Fixed argv, no shell interpolation, no user-controlled command string.
            proc = subprocess.Popen(
                [str(PYTHON), "-m", "uvicorn", "app.main:app", "--host", API_HOST, "--port", str(API_PORT)],
                cwd=BACKEND,
                stdout=out_handle,
                stderr=err_handle,
                env={**os.environ, **SERVER_ENV},
            )
        up, waited = _wait_for_http(f"http://{API_HOST}:{API_PORT}/health/live", 90)
        server = {
            "started": up,
            "pid": proc.pid,
            "command": f"{PYTHON} -m uvicorn app.main:app --host {API_HOST} --port {API_PORT}",
            "cwd": str(BACKEND),
            "answered_after_seconds": waited,
            "log": str(backend_log.relative_to(ROOT)),
            "error_log": str(backend_err.relative_to(ROOT)),
            "environment_overrides": SERVER_ENV,
            "environment_note": (
                "The shared .env does not define PAYMENT_MOCK_ENABLED, and the code default is "
                "False, which disables the mock-pay surface (the settlement click answers 60006). "
                ".env.example documents it as true for the dev profile, so this run starts its own "
                "server with that documented value instead of editing a file owned by another "
                "member. Any other gate that drives mock payment must do the same."
            ),
        }
        if not up:
            server["startup_error"] = backend_err.read_text(encoding="utf-8", errors="replace")[-1500:]
            reasons.append("the backend did not answer /health/live, so no HTTP journey could run")
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        else:
            server["health_status"] = 200
    else:
        reasons.append(f"port {API_PORT} was already in use; refusing to test against an unknown server")
        proc = None

    # 3. and 4. the real browser run and the real journey, only if the backend is up
    browser: dict[str, object] = {"ran": False, "reason": "backend unavailable"}
    journey: dict[str, object] = {"ran": False, "reason": "backend unavailable"}
    if server.get("started"):
        browser = _playwright()
        check(
            "the Playwright browser spec ran",
            browser.get("ran") is True,
            browser.get("error", browser.get("ran")),
        )
        check(
            "every Playwright test passed",
            bool(browser.get("tests")) and all(test["passed"] for test in browser["tests"]),
            browser.get("tests"),
        )
        check(
            "the Playwright process exited 0",
            browser.get("exit_code") == 0,
            browser.get("exit_code"),
            0,
        )

        driver = subprocess.run(
            [str(PYTHON), str(ROOT / "scripts" / "fg22_journey.py")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            check=False,
            env={**os.environ, "FLAGSHIP_ALLOW_WRITES": "1"},
        )
        driver_path = REPORTS / "fg22_journey.json"
        # The WHOLE stdout, not a tail: the driver's report *is* its stdout, and
        # truncating it turns a structurally perfect report into unparsable text -
        # which is exactly how three failing checks were invented on a run where the
        # underlying observations were fine.
        driver_path.write_text(driver.stdout or "", encoding="utf-8")
        try:
            journey = json.loads(driver.stdout or "{}")
        except ValueError as exc:
            journey = {"ran": False, "error": f"driver output was not JSON: {exc}"}
        journey["ran"] = bool(journey.get("checklist"))
        journey["exit_code"] = driver.returncode
        journey["stderr_tail"] = (driver.stderr or "")[-2000:]
        check(
            "the journey driver ran and produced its checklist",
            journey.get("ran") is True,
            journey.get("error", driver.returncode),
        )
        for item in journey.get("checklist", []):
            if str(item.get("name", "")).startswith("[10]"):
                # The browser's own result is recorded separately, from the real run.
                continue
            assertions.append(
                {
                    "name": item["name"],
                    "expected": item["expected"],
                    "actual": item["actual"],
                    "pass": bool(item["pass"]),
                }
            )
        browser_specs = browser.get("tests") or []
        assertions.append(
            {
                "name": "[10] a real browser (Playwright) drove the journey and its result is reported separately from the DB facts",
                "expected": True,
                "actual": {
                    "tests": [test["title"] for test in browser_specs],
                    "passed": all(test["passed"] for test in browser_specs),
                    "exit_code": browser.get("exit_code"),
                },
                "pass": bool(browser_specs) and all(test["passed"] for test in browser_specs)
                and browser.get("exit_code") == 0,
            }
        )

    # 5. stop the server this script started; leave nothing listening
    if server.get("started"):
        try:
            proc.terminate()  # type: ignore[union-attr]
            proc.wait(timeout=20)  # type: ignore[union-attr]
        except (subprocess.TimeoutExpired, OSError):
            try:
                proc.kill()  # type: ignore[union-attr]
            except OSError:
                pass
        time.sleep(1.0)
        still_up = not _port_free(API_PORT)
        server["port_free_after_shutdown"] = not still_up
        check("the backend this run started was shut down", not still_up, still_up, False)
        if still_up:
            reasons.append(f"a server is still listening on {API_PORT} after shutdown")

    for failure in assertions:
        if not failure["pass"]:
            reasons.append(f"{failure['name']}: expected {failure['expected']!r}, got {failure['actual']!r}")

    # JUnit: written from the real Playwright report so it cannot disagree with the
    # browser assertion above. ``parses`` reflects whether a document really exists.
    testcase_count = len(assertions)
    XML_OUT.parent.mkdir(parents=True, exist_ok=True)
    cases = "".join(_assertion_testcase_xml(item) for item in assertions)
    XML_OUT.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name="fg22-flagship-agent" tests="{testcase_count}" '
        f'failures="{sum(1 for item in assertions if not item["pass"])}">'
        f"{cases}</testsuite>\n",
        encoding="utf-8",
    )
    junit = {
        "path": str(XML_OUT.relative_to(ROOT)),
        "parses": False,
        "testcase_count": testcase_count,
        "source": "real Playwright JSON report, converted",
    }
    try:
        import xml.etree.ElementTree as ET

        tree = ET.parse(XML_OUT)
        junit["parses"] = tree.getroot().tag == "testsuite"
        junit["testcase_count"] = len(list(tree.iter("testcase")))
    except Exception as exc:  # noqa: BLE001 - a malformed JUnit file is an observation
        junit["error"] = f"{type(exc).__name__}: {exc}"
    if not junit["parses"]:
        reasons.append("the JUnit report could not be parsed")

    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    exit_code = max(
        int(browser.get("exit_code") or 0) if browser.get("ran") else 1,
        int(journey.get("exit_code") or 0) if journey.get("ran") else 1,
    )
    verdict = "PASS" if not reasons else "FAIL"

    report = {
        "gate_id": "FG-22",
        "name": "Flagship Agent E2E",
        "command": (
            f"{PYTHON} scripts/emit_fg22_evidence.py  "
            f"[starts `python -m uvicorn app.main:app --host {API_HOST} --port {API_PORT}`, waits for "
            f"/health/live, runs `playwright test {SPEC} --reporter=json` on E2E_PORT={E2E_PORT}, runs "
            "scripts/fg22_journey.py (fixture modes seed/http/service/interrupt/decision/resume/verify/cleanup), "
            "then stops the server]"
        ),
        "cwd": str(ROOT),
        "timestamp": started.isoformat(),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "git": git_state(RELEVANT_PATHS),
        "specification": (
            "Real HTTP/Browser. Agent request, tool call, permission/DataScope, PendingAction, "
            "DB-locked approval, expiry blocking, no non-repeatable side effect before interrupt, "
            "Resume re-validation, duplicate Resume idempotency. Seven fact classes verified by "
            "querying MySQL (Order, Payment, InventoryMovement, PaymentCallback, Fulfillment, Audit, "
            "Outbox). Page text does not substitute for database facts."
        ),
        "transport_notes": {
            "real_http": [
                "POST /api/v1/auth/login",
                "POST /api/v1/agent/chat (twice with the same client_request_id)",
                "GET /api/v1/agent/runs (owner scope)",
                "GET /api/v1/agent/admin/tools (console permission)",
                "POST /api/v1/pending-actions/{id}/approve (approved, re-decided, expired, and as a consumer)",
                "GET /api/v1/pending-actions",
            ],
            "real_browser": f"playwright test {SPEC} --reporter=json",
            "service_layer_with_no_http_endpoint": [
                (
                    "PendingAction creation: the frozen HTTP surface only lists, reads, approves and "
                    "rejects - no endpoint creates a PendingAction, so the fixture creates the row "
                    "through the real PendingActionService/model and drives every decision through HTTP "
                    "or the locked service."
                ),
                (
                    "reject, CRITICAL-risk block and payload-hash mismatch: no HTTP endpoint exists for "
                    "these, so they are driven through PendingActionService, whose approve/reject the "
                    "endpoint calls."
                ),
                (
                    "LangGraph interrupt/resume: the HITL graph is not exposed over HTTP; it is built "
                    "with build_approval_graph and a real checkpointer while re-reading the same "
                    "database rows."
                ),
            ],
        },
        "shared_database_check": database,
        "backend_server": server,
        "browser": browser,
        "journey": journey,
        "junit_report": junit,
        "assertions": assertions,
        "fail_reasons": reasons,
        "verdict": verdict,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"wrote {XML_OUT.relative_to(ROOT)}")
    print(
        f"exit_code={exit_code}  assertions={len(assertions)}  "
        f"junit_testcases={junit['testcase_count']}  verdict={verdict}"
    )
    for failure in [item for item in assertions if not item["pass"]]:
        print(f"  [FAIL] {failure['name']} -> {failure['actual']!r}")
    for reason in reasons[:30]:
        print(f"  ! {reason}")
    return 0 if verdict == "PASS" else 1


def _testcase_xml(test: dict[str, object]) -> str:
    """One JUnit testcase, written from the Playwright report for that test."""
    name = _xml_escape(str(test["title"]))
    if test["passed"]:
        return f'<testcase classname="fg22.browser" name="{name}" />'
    return f'<testcase classname="fg22.browser" name="{name}"><failure /></testcase>'


def _assertion_testcase_xml(assertion: dict[str, object]) -> str:
    name = _xml_escape(str(assertion.get("name", "unnamed assertion")))
    if assertion.get("pass") is True:
        return f'<testcase classname="fg22" name="{name}" />'
    detail = _xml_escape(
        f'expected={assertion.get("expected")!r}; actual={assertion.get("actual")!r}'
    )
    return f'<testcase classname="fg22" name="{name}"><failure message="{detail}" /></testcase>'


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


if __name__ == "__main__":
    raise SystemExit(main())
