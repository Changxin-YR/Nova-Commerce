"""Emit FG-23 evidence: health checks and availability probes.

Two independent questions, both answered by observation rather than assumption:

  1. **Is every long-running container of the ``nova`` project up and healthy?**
     Answered by running the repository's own compose file for real
     (``docker compose --env-file .env -f ops/docker-compose.yml ps --all --format json``)
     and reading the ``State`` / ``Health`` fields Docker reported. Docker Desktop
     does intermittently answer this with an HTTP 500 while the engine is flaky, so
     the call is retried a few times and the retry count is recorded - a flaky
     engine must not be laundered into either a PASS or a hard FAIL.

  2. **Is each service actually reachable from the host?** Compose health status is
     a claim made from *inside* the container's network namespace. A container can
     be ``healthy`` while the published host port is not forwarded, so every
     service is also probed from here: a real TCP connect to its published port,
     plus a real HTTP GET for the services that expose an HTTP health endpoint.

Container names in this project are ``nx-*`` (``ops/docker-compose.yml`` declares
``container_name``), and services under the optional ``tools`` / ``observability``
profiles are not running by default - they are reported as absent, not counted as
failures, because they are optional by design (spec section 128).

## Why the recorded ``exit_code`` is 0 even when the verdict is FAIL

``exit_code`` in the artifact is the exit code of the command this gate *observed* -
the docker/minio/git/parser call that produced the assertions. It is deliberately not
the exit status of this script, because the two answer different questions and
conflating them is how an artifact ends up claiming a command succeeded when it did
not. When this emitter's own work is inconclusive (the endpoint is unreachable, the
command could not be run) the recorded ``exit_code`` is non-zero, because then the
observation itself failed.

The script's own process exit status *is* the verdict (0 for PASS, 1 for FAIL), so a
caller or a shell chain can still branch on it."""

from __future__ import annotations

import json
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

OUT = ROOT / "artifacts" / "evidence" / "docker" / "fg23_health.json"

COMPOSE_FILE = ROOT / "ops" / "docker-compose.yml"
ENV_FILE = ROOT / ".env"
PS_ATTEMPTS = 5
PS_RETRY_DELAY_SECONDS = 5.0

#: Published host ports from ops/docker-compose.yml, and the HTTP health endpoint
#: each service documents (None where the service has no HTTP surface to probe).
#: ``container``/``service`` name the compose entry so the finding can be traced
#: back to the compose file.
SERVICES: tuple[dict[str, object], ...] = (
    {
        "name": "MySQL",
        "container": "nx-mysql",
        "service": "mysql",
        "port": 13306,
        "url": None,
    },
    {
        "name": "Redis",
        "container": "nx-redis",
        "service": "redis",
        "port": 16379,
        "url": None,
    },
    {
        "name": "MinIO",
        "container": "nx-minio",
        "service": "minio",
        "port": 19000,
        "url": "http://127.0.0.1:19000/minio/health/live",
    },
    {
        "name": "Qdrant",
        "container": "nx-qdrant",
        "service": "qdrant",
        "port": 16333,
        "url": "http://127.0.0.1:16333/healthz",
    },
    {
        "name": "Keycloak",
        "container": "nx-keycloak",
        "service": "oauth-provider",
        "port": 18080,
        "url": "http://127.0.0.1:18080/realms/master/.well-known/openid-configuration",
    },
)

RELEVANT_PATHS = (
    "scripts/emit_fg23_evidence.py",
    "scripts/gate_evidence.py",
    "ops/docker-compose.yml",
    ".env.example",
)


def _compose_ps() -> dict[str, object]:
    """Run ``docker compose ps --all --format json``, retrying a flaky engine."""
    command = [
        "docker",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "-f",
        str(COMPOSE_FILE),
        "ps",
        "--all",
        "--format",
        "json",
    ]
    outcome: dict[str, object] = {
        "command": command,
        "attempts": 0,
        "exit_codes": [],
        "stdout_tail": "",
        "stderr_tail": "",
        "containers": [],
        "parsed": False,
        "parse_error": None,
    }
    raw = ""
    for attempt in range(1, PS_ATTEMPTS + 1):
        outcome["attempts"] = attempt
        try:
            proc = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
            code, raw, err = proc.returncode, proc.stdout or "", proc.stderr or ""
        except (OSError, subprocess.TimeoutExpired) as exc:
            code, raw, err = 127, "", f"{type(exc).__name__}: {exc}"
        outcome["exit_codes"].append(code)
        outcome["stdout_tail"] = raw[-4000:]
        outcome["stderr_tail"] = err[-4000:]
        if code == 0 and raw.strip():
            break
        if attempt < PS_ATTEMPTS:
            # 500 from the engine, or an empty list while the socket wakes up.
            time.sleep(PS_RETRY_DELAY_SECONDS)

    containers: list[dict[str, object]] = []
    parse_error: str | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            # Older/newer compose versions emit a single JSON array instead of
            # newline-delimited objects; handle both rather than guessing.
            try:
                decoded = json.loads(raw)
            except ValueError as exc:
                parse_error = f"docker compose ps output is not JSON: {exc}"
                break
            items = decoded if isinstance(decoded, list) else [decoded]
            containers = [_summarise(entry) for entry in items if isinstance(entry, dict)]
            break
        containers.append(_summarise(item))
    if parse_error is None and not containers and raw.strip():
        parse_error = "docker compose ps returned no parsable container entries"
    outcome["containers"] = containers
    outcome["parsed"] = parse_error is None and bool(containers)
    outcome["parse_error"] = parse_error
    return outcome


def _summarise(entry: dict[str, object]) -> dict[str, object]:
    return {
        "name": entry.get("Name") or entry.get("name"),
        "service": entry.get("Service") or entry.get("service"),
        "state": (entry.get("State") or entry.get("state") or ""),
        "health": (entry.get("Health") or entry.get("health") or ""),
        "status": entry.get("Status") or entry.get("status") or "",
        "exit_code": entry.get("ExitCode"),
        "publishers": entry.get("Publishers") or [],
    }


def _tcp(port: int) -> dict[str, object]:
    started = datetime.now(UTC)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(5.0)
        code = probe.connect_ex(("127.0.0.1", port))
    return {
        "host": "127.0.0.1",
        "port": port,
        "connect_ex": code,
        "open": code == 0,
        "duration_ms": int((datetime.now(UTC) - started).total_seconds() * 1000),
    }


def _http(url: str) -> dict[str, object]:
    result: dict[str, object] = {"url": url, "status": None, "body_head": "", "error": None}
    started = datetime.now(UTC)
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read(2000).decode("utf-8", "replace")
            result["status"] = response.status
            result["body_head"] = body[:2000]
    except urllib.error.HTTPError as exc:
        result["status"] = exc.code
        result["body_head"] = exc.read(2000).decode("utf-8", "replace")
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["duration_ms"] = int((datetime.now(UTC) - started).total_seconds() * 1000)
    return result


def main() -> int:
    started = datetime.now(UTC)
    ps = _compose_ps()
    by_container = {str(item["name"]): item for item in ps["containers"]}

    probes: list[dict[str, object]] = []
    for spec in SERVICES:
        container = str(spec["container"])
        entry = by_container.get(container)
        probe: dict[str, object] = {
            "name": spec["name"],
            "container": container,
            "service": spec["service"],
            "compose_entry": entry,
            "tcp": _tcp(int(spec["port"])),
            "http": None,
        }
        if spec["url"]:
            probe["http"] = _http(str(spec["url"]))

        probes.append(probe)

    assertions: list[dict[str, object]] = []

    def check(name: str, expected: object, actual: object) -> None:
        assertions.append(
            {"name": name, "expected": expected, "actual": actual, "pass": actual == expected}
        )

    check("docker compose ps returned parsable JSON", True, ps["parsed"])
    check(
        "docker compose ps reported every service declared in ops/docker-compose.yml",
        sorted(str(spec["container"]) for spec in SERVICES),
        sorted(by_container),
    )
    for spec in SERVICES:
        container = str(spec["container"])
        entry = by_container.get(container)
        health = str((entry or {}).get("health", "")).lower()
        state = str((entry or {}).get("state", "")).lower()
        # Both are stated per service, because "healthy" and "running" are different
        # claims: a container with no healthcheck can only ever report the latter.
        check(f"{container} reports healthy", True, health == "healthy" or (health == "" and state == "running"))
        check(f"{container} is running", True, state == "running")

    for probe in probes:
        name = str(probe["name"])
        tcp = probe["tcp"]
        assert isinstance(tcp, dict)
        check(f"{name} port {tcp['port']} accepts TCP connections", True, tcp["open"])
        http = probe["http"]
        if isinstance(http, dict):
            check(f"{name} HTTP health endpoint returns 200", 200, http["status"])

    reasons: list[str] = []
    if not ps["parsed"]:
        reasons.append(f"docker compose ps unusable after {ps['attempts']} attempt(s): {ps['parse_error']}")
        if ps["stderr_tail"]:
            reasons.append(f"docker compose ps stderr: {ps['stderr_tail'][-300:]}")
    for failure in assertions:
        if not failure["pass"]:
            reasons.append(f"{failure['name']}: expected {failure['expected']!r}, got {failure['actual']!r}")

    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    verdict = "PASS" if not reasons else "FAIL"
    # The exit codes this run actually observed: the compose call, plus every probe.
    # A non-zero here means a command could not be run at all, which is recorded
    # rather than replaced with a tidy zero.
    observed_exit_codes: dict[str, int] = {
        "docker_compose_ps": int(ps["exit_codes"][-1]) if ps["exit_codes"] else 127,
    }
    for probe in probes:
        entry_name = str(probe["container"])
        tcp = probe["tcp"]
        observed_exit_codes[f"{entry_name}_tcp"] = int(tcp["connect_ex"])
        http = probe["http"]
        if isinstance(http, dict):
            # A success status is 0; a refusing/erroring endpoint keeps its own code,
            # and an HTTP error status is recorded as itself.
            status = http["status"]
            observed_exit_codes[f"{entry_name}_http"] = 0 if status == 200 else int(status or 7)
    exit_code = max(observed_exit_codes.values(), default=127)

    report = {
        "gate_id": "FG-23",
        "name": "Health Checks",
        "command": (
            f"{sys.executable} scripts/emit_fg23_evidence.py  "
            "[runs: docker compose --env-file .env -f ops/docker-compose.yml ps --all --format json, "
            "then TCP probes on 13306/16379/19000/16333/18080 and HTTP GETs on the MinIO, Qdrant "
            "and Keycloak health endpoints]"
        ),
        "cwd": str(ROOT),
        "timestamp": started.isoformat(),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "observed_exit_codes": observed_exit_codes,
        "git": git_state(RELEVANT_PATHS),
        "compose_ps": ps,
        "probes": probes,
        "optional_profiles": {
            "note": (
                "ops/docker-compose.yml declares optional 'tools' (nx-migrate) and "
                "'observability' (nx-otel, nx-prometheus, nx-grafana) profiles. They are "
                "not started by default and are reported here for completeness only."
            ),
            "expected_default_containers": [str(spec["container"]) for spec in SERVICES],
            "observed_containers": sorted(by_container),
        },
        "assertions": assertions,
        "fail_reasons": reasons,
        "verdict": verdict,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(
        f"exit_code={exit_code}  assertions={len(assertions)}  verdict={verdict}  "
        f"compose_ps_attempts={ps['attempts']}"
    )
    for item in assertions:
        print(f"  [{'PASS' if item['pass'] else 'FAIL'}] {item['name']}: {item['actual']!r}")
    for reason in reasons:
        print(f"  ! {reason}")
    return 0 if verdict == "PASS" else 1



if __name__ == "__main__":
    raise SystemExit(main())