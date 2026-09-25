"""Emit FG-02 evidence: backend static analysis and real process startup.

Four real observations, none of them inferred:

  (a) ``python -m compileall -q app``     - does the package byte-compile at all?
  (b) ``python -c "import app.main"``     - does the ASGI application import, which
                                            executes the whole module graph including
                                            every router's decorators?
  (c) ``python -m mypy app``              - the project's own type gate
                                            (``backend/pyproject.toml`` [tool.mypy]).
  (d) an actual ``uvicorn`` process on 127.0.0.1:8011 that is polled on
      ``GET /health/live`` until it answers or the deadline passes, then terminated.

Step (d) is the one that matters: (a)-(c) are static, and a backend can pass all
three and still fail to serve - a missing runtime dependency, a lifespan hook that
raises, a settings field that is not populated. So this gate starts the server for
real and records the HTTP status and the response envelope it actually returned,
plus the server's own stdout/stderr.

Nothing here is a hardcoded verdict or a hardcoded exit code: every ``exit_code``
in the assertions below comes from the subprocess that produced it.

The proof path is a ``.txt`` file (frozen in PROJECT_BASELINE.yaml), so the raw
combined output goes there and the machine-readable report is written to the
sibling ``.json``, which is the file the Final Gate parses.
"""

from __future__ import annotations

import json
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gate_evidence import BACKEND, ROOT, git_state

PYTHON = pathlib.Path(sys.executable)
TXT_OUT = ROOT / "artifacts" / "evidence" / "build" / "fg02_backend_static.txt"
JSON_OUT = TXT_OUT.with_suffix(".json")

HOST = "127.0.0.1"
PORT = 8011
BASE_URL = f"http://{HOST}:{PORT}"
STARTUP_DEADLINE_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 1.0

RELEVANT_PATHS = (
    "scripts/emit_fg02_evidence.py",
    "scripts/gate_evidence.py",
    "backend/app",
    "backend/pyproject.toml",
    "backend/alembic.ini",
    ".env.example",
)


def _run(args: list[str], *, timeout: int) -> tuple[int, str, str, int]:
    """Run a command in ``backend/`` and return (exit_code, stdout, stderr, ms)."""
    started = datetime.now(UTC)
    try:
        proc = subprocess.run(
            args,
            cwd=BACKEND,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return (
            proc.returncode,
            proc.stdout or "",
            proc.stderr or "",
            int((datetime.now(UTC) - started).total_seconds() * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        return 124, out, f"{err}\ncommand exceeded {timeout}s", int(
            (datetime.now(UTC) - started).total_seconds() * 1000
        )
    except OSError as exc:
        return 127, "", f"could not execute {args!r}: {exc}", 0


def _port_free() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((HOST, PORT)) != 0


def _startup_probe(server_command: list[str]) -> dict[str, object]:
    """Start uvicorn for real, poll /health/live, then terminate it."""
    result: dict[str, object] = {
        "command": server_command,
        "url": f"{BASE_URL}/health/live",
        "listening_before_start": not _port_free(),
        "process_exit_code": None,
        "http_status": None,
        "body": None,
        "body_json": None,
        "answered_after_seconds": None,
        "attempts": 0,
        "stdout_tail": "",
        "stderr_tail": "",
        "error": None,
    }
    if result["listening_before_start"]:
        result["error"] = f"port {PORT} was already in use before the probe started"
        return result

    # The server's output goes to temporary files rather than pipes: a pipe nobody
    # is draining can fill and block uvicorn mid-request, which would turn this
    # probe into a hang that looks like a startup failure.
    log_dir = pathlib.Path(tempfile.mkdtemp(prefix="fg02-uvicorn-"))
    stdout_path = log_dir / "uvicorn.stdout.log"
    stderr_path = log_dir / "uvicorn.stderr.log"
    started = datetime.now(UTC)
    with stdout_path.open("w", encoding="utf-8") as out_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as err_handle:
        # Fixed argv, no shell interpolation, no user-controlled command string.
        proc = subprocess.Popen(
            server_command,
            cwd=BACKEND,
            stdout=out_handle,
            stderr=err_handle,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    try:
        url = f"{BASE_URL}/health/live"
        while (datetime.now(UTC) - started).total_seconds() < STARTUP_DEADLINE_SECONDS:
            if proc.poll() is not None:
                result["error"] = (
                    f"uvicorn exited before answering (exit_code={proc.returncode})"
                )
                break
            result["attempts"] = int(result["attempts"]) + 1
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    body = response.read().decode("utf-8", "replace")
                    result["http_status"] = response.status
                    result["body"] = body
                    try:
                        result["body_json"] = json.loads(body)
                    except ValueError:
                        result["body_json"] = None
                    result["answered_after_seconds"] = round(
                        (datetime.now(UTC) - started).total_seconds(), 3
                    )
                    break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")
                result["http_status"] = exc.code
                result["body"] = body
                try:
                    result["body_json"] = json.loads(body)
                except ValueError:
                    result["body_json"] = None
                result["answered_after_seconds"] = round(
                    (datetime.now(UTC) - started).total_seconds(), 3
                )
                break
            except (urllib.error.URLError, ConnectionError, OSError):
                # Not listening yet - that is the normal case for the first few polls.
                time.sleep(POLL_INTERVAL_SECONDS)
        else:
            result["error"] = (
                f"no answer within {STARTUP_DEADLINE_SECONDS}s "
                f"({result['attempts']} polls)"
            )
        if result["http_status"] is None and result["error"] is None:
            result["error"] = "probe loop ended without an answer"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=15)
        result["process_exit_code"] = proc.returncode
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.is_file() else ""
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.is_file() else ""
        result["stdout_tail"] = stdout[-4000:]
        result["stderr_tail"] = stderr[-4000:]
        result["log_dir"] = str(log_dir)
    return result


def main() -> int:
    started = datetime.now(UTC)
    transcript: list[str] = []

    compile_command = [str(PYTHON), "-m", "compileall", "-q", "app"]
    compile_code, compile_out, compile_err, compile_ms = _run(compile_command, timeout=300)

    import_command = [str(PYTHON), "-c", "import app.main"]
    import_code, import_out, import_err, import_ms = _run(import_command, timeout=300)

    mypy_command = [str(PYTHON), "-m", "mypy", "app"]
    mypy_code, mypy_out, mypy_err, mypy_ms = _run(mypy_command, timeout=900)

    server_command = [
        str(PYTHON),
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        HOST,
        "--port",
        str(PORT),
    ]
    server = _startup_probe(server_command)

    for label, args, code, out, err, ms in (
        ("(a) compileall", compile_command, compile_code, compile_out, compile_err, compile_ms),
        ("(b) import app.main", import_command, import_code, import_out, import_err, import_ms),
        ("(c) mypy", mypy_command, mypy_code, mypy_out, mypy_err, mypy_ms),
    ):
        transcript.append(f"$ {' '.join(args)}")
        transcript.append(f"[{label}] exit_code={code} duration_ms={ms}")
        transcript.append(out.rstrip())
        if err.strip():
            transcript.append("--- stderr ---")
            transcript.append(err.rstrip())
        transcript.append("")
    transcript.append(f"$ {' '.join(server_command)}")
    transcript.append(
        f"(d) startup probe: exit_code={server['process_exit_code']} "
        f"http_status={server['http_status']} after_seconds={server['answered_after_seconds']} "
        f"polls={server['attempts']}"
    )
    transcript.append(f"GET {server['url']} -> {server['body']}")
    if server["error"]:
        transcript.append(f"probe error: {server['error']}")
    transcript.append("--- uvicorn stdout ---")
    transcript.append(str(server["stdout_tail"]).rstrip())
    transcript.append("--- uvicorn stderr ---")
    transcript.append(str(server["stderr_tail"]).rstrip())

    body_json = server["body_json"]
    envelope_code = body_json.get("code") if isinstance(body_json, dict) else None
    envelope_keys = sorted(body_json.keys()) if isinstance(body_json, dict) else []

    assertions: list[dict[str, object]] = []

    def check(name: str, expected: object, actual: object) -> None:
        assertions.append(
            {"name": name, "expected": expected, "actual": actual, "pass": actual == expected}
        )

    check("python -m compileall -q app exit code", 0, compile_code)
    check("python -c 'import app.main' exit code", 0, import_code)
    check("python -m mypy app exit code", 0, mypy_code)
    check("uvicorn started and answered GET /health/live", True, server["http_status"] is not None)
    check("GET /health/live HTTP status", 200, server["http_status"])
    check("health envelope business code", 0, envelope_code)
    check(
        "health envelope carries the documented keys",
        ["code", "data", "message", "trace_id"],
        envelope_keys,
    )
    check("uvicorn terminated cleanly under our control", True, server["process_exit_code"] is not None)

    reasons: list[str] = []
    if compile_code != 0:
        reasons.append(f"compileall exit_code={compile_code}")
    if import_code != 0:
        reasons.append(f"import app.main exit_code={import_code}")
    if mypy_code != 0:
        reasons.append(f"mypy exit_code={mypy_code}")
    if server["error"]:
        reasons.append(f"startup probe: {server['error']}")
    if server["http_status"] != 200:
        reasons.append(f"GET /health/live answered {server['http_status']}")
    if envelope_code != 0:
        reasons.append(f"health envelope code={envelope_code!r}")

    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    # The recorded exit code is the exit code of the static analyzers only. The
    # startup probe is a child process this script starts and then terminates, so
    # its exit status says nothing about the build; that observation is carried by
    # the `http_status` assertion instead.
    exit_code = max(compile_code, import_code, mypy_code, 0)
    verdict = "PASS" if not reasons else "FAIL"

    report = {
        "gate_id": "FG-02",
        "name": "Backend Static / Startup Check",
        "command": (
            f"{PYTHON} scripts/emit_fg02_evidence.py  "
            "[runs: python -m compileall -q app; python -c 'import app.main'; "
            f"python -m mypy app; python -m uvicorn app.main:app --host {HOST} --port {PORT} "
            "+(GET /health/live)]"
        ),
        "cwd": str(BACKEND),
        "timestamp": started.isoformat(),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "git": git_state(RELEVANT_PATHS),
        "steps": {
            "compileall": {
                "command": compile_command,
                "exit_code": compile_code,
                "duration_ms": compile_ms,
                "stdout_tail": compile_out[-2000:],
                "stderr_tail": compile_err[-2000:],
            },
            "import_app_main": {
                "command": import_command,
                "exit_code": import_code,
                "duration_ms": import_ms,
                "stdout_tail": import_out[-2000:],
                "stderr_tail": import_err[-2000:],
            },
            "mypy": {
                "command": mypy_command,
                "exit_code": mypy_code,
                "duration_ms": mypy_ms,
                "summary": mypy_out.strip().splitlines()[-1] if mypy_out.strip() else "",
                "stdout_tail": mypy_out[-4000:],
                "stderr_tail": mypy_err[-2000:],
            },
            "startup_probe": server,
        },
        "assertions": assertions,
        "fail_reasons": reasons,
        "verdict": verdict,
    }

    TXT_OUT.parent.mkdir(parents=True, exist_ok=True)
    TXT_OUT.write_text("\n".join(transcript) + "\n", encoding="utf-8")
    JSON_OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {TXT_OUT.relative_to(ROOT)}")
    print(f"wrote {JSON_OUT.relative_to(ROOT)}")
    print(f"exit_code={exit_code}  assertions={len(assertions)}  verdict={verdict}")
    for item in assertions:
        print(f"  [{'PASS' if item['pass'] else 'FAIL'}] {item['name']}: {item['actual']!r}")
    for reason in reasons:
        print(f"  ! {reason}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())