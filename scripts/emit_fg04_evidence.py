"""Emit the FG-04 (project Docker build) evidence artifact from a real build.

Spec section 144: a gate artifact must carry the command, timestamp, exit code and
per-assertion results, and it must be regenerable by a script. Spec section 143 is
explicit that evidence is produced by *running* the gate: this emitter therefore
performs a real ``docker build`` of the application image and records what the
engine actually did.

Why this gate exists at all, and why it was missing
---------------------------------------------------
``ops/docker-compose.yml`` declares a ``migrate`` service whose build is

    context:  ..
    dockerfile: ops/docker/Dockerfile.api

and a final-gate inventory that demands a real project Docker build. Until now
``ops/docker/Dockerfile.api`` did not exist, so the reference was aspirational and
the gate had no evidence. The image is built here (real engine, real layers) and
the observed image id/size are recorded, so a future reader can tell a genuine
build from a claim.

The build context is the repository root; ``.dockerignore`` keeps ``.venv/``,
``node_modules/`` and ``artifacts/`` out of it. Both the Dockerfile and the
ignore file are part of the watched paths, because changing either changes what
this evidence means.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

from gate_evidence import ROOT, git_state

PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
DOCKERFILE = "ops/docker/Dockerfile.api"
IMAGE_TAG = "nova-commerce-api:fg04"
TXT_OUT = ROOT / "artifacts" / "evidence" / "docker" / "fg04_docker_build.txt"
JSON_OUT = TXT_OUT.with_suffix(".json")

DOCKERFILE_PATH = ROOT / DOCKERFILE
DOCKERIGNORE_PATH = ROOT / ".dockerignore"

#: Production target, then the fallbacks a registry-isolated host may have cached.
#: A build that silently substituted an interpreter would be dishonest, so the
#: base is chosen from what is *actually present* in the local engine and the
#: choice is recorded in the artifact.
PREFERRED_BASES = ("python:3.11-slim", "python:3.12-slim")


def _run(command: list[str], *, timeout: int) -> tuple[int, str]:
    """Run a command from the repo root and return (exit_code, combined output)."""
    try:
        proc = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")
        return 124, stdout + stderr + f"\n[docker build exceeded the {timeout}s cap]\n"
    except OSError as exc:
        return 127, f"could not execute {command[0]}: {exc}\n"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _local_images() -> set[str]:
    code, output = _run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=120)
    return {line.strip() for line in output.splitlines() if line.strip()} if code == 0 else set()


def _choose_base(local: set[str]) -> str | None:
    """First preferred interpreter already present locally.

    Docker Hub is unreachable from this build host (``auth.docker.io`` times out),
    so an uncached base cannot be pulled. Choosing a cached interpreter that still
    satisfies ``requires-python`` keeps the dependency install - which does reach
    PyPI - genuinely exercised, instead of failing for a reason unrelated to the
    project.
    """
    return next((candidate for candidate in PREFERRED_BASES if candidate in local), None)


def main() -> int:
    started = datetime.now(UTC)
    TXT_OUT.parent.mkdir(parents=True, exist_ok=True)

    assertions: list[dict[str, object]] = []

    def check(name: str, passed: bool, actual: str) -> None:
        assertions.append({"name": name, "expected": "PASSED", "actual": actual, "pass": passed})

    # --- the inputs the gate depends on -------------------------------------
    check("ops/docker/Dockerfile.api exists", DOCKERFILE_PATH.is_file(),
          "present" if DOCKERFILE_PATH.is_file() else "MISSING")
    check(".dockerignore exists (keeps the build context small)", DOCKERIGNORE_PATH.is_file(),
          "present" if DOCKERIGNORE_PATH.is_file() else "MISSING")

    # --- base interpreter ---------------------------------------------------
    local = _local_images()
    base = _choose_base(local)
    check(
        "an allowed Python base image is available to the engine without a registry pull",
        base is not None,
        base if base is not None else f"none of {PREFERRED_BASES} present locally",
    )

    # --- the real build -----------------------------------------------------
    if base is None:
        build_code, build_output = 127, (
            "no allowed base image is present locally and Docker Hub is unreachable "
            f"(tried {', '.join(PREFERRED_BASES)})\n"
        )
        build_cmd = ["docker", "build", "--file", DOCKERFILE, "--tag", IMAGE_TAG, "."]
    else:
        build_cmd = [
            "docker", "build",
            "--file", DOCKERFILE,
            "--build-arg", f"PYTHON_BASE={base}",
            "--tag", IMAGE_TAG,
            ".",
        ]
        build_code, build_output = _run(build_cmd, timeout=3600)
    check("docker build exits 0", build_code == 0, f"exit_code={build_code}")

    inspect_code = 1
    image_meta = ""
    if build_code == 0:
        inspect_cmd = ["docker", "image", "inspect", IMAGE_TAG, "--format", "{{.Id}} {{.Size}}"]
        inspect_code, image_meta = _run(inspect_cmd, timeout=120)
        check("built image is present in the local engine", inspect_code == 0 and bool(image_meta.strip()),
              (image_meta.strip() or f"exit_code={inspect_code}").splitlines()[0])

    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    source_state = git_state(
        (
            DOCKERFILE,
            ".dockerignore",
            "backend/pyproject.toml",
            "backend/app",
            "backend/alembic.ini",
            "backend/migrations",
            "ops/docker-compose.yml",
            "scripts/emit_fg04_evidence.py",
            "scripts/gate_evidence.py",
        )
    )

    verdict = (
        "PASS"
        if build_code == 0
        and inspect_code == 0
        and all(item["pass"] is True for item in assertions)
        and source_state["relevant_paths_dirty"] is False
        else "FAIL"
    )

    # Human-readable proof: the real engine output, verbatim.
    TXT_OUT.write_text(
        "\n".join(
            [
                "# FG-04 project Docker build",
                f"# command: {' '.join(build_cmd)}",
                f"# cwd: {ROOT}",
                f"# started: {started.isoformat()}",
                f"# python base image: {base or '(none available locally)'}",
                f"# exit_code: {build_code}",
                f"# image: {IMAGE_TAG}",
                f"# image inspect: {image_meta.strip() or f'(not inspected, exit {inspect_code})'}",
                "",
                build_output,
            ]
        ),
        encoding="utf-8",
    )

    report = {
        "gate_id": "FG-04",
        "name": "Docker Build",
        "mandatory": False,
        "spec": "section 128/144 - the application image builds from the frozen Dockerfile and the real engine confirms it",
        "command": " ".join(build_cmd),
        "cwd": str(ROOT),
        "timestamp": started.isoformat(),
        "duration_ms": duration_ms,
        "exit_code": build_code,
        "python_base_image": base,
        "image": {"tag": IMAGE_TAG, "inspect": image_meta.strip()},
        "git": source_state,
        "assertions": assertions,
        "junit_report": None,
        "summary": build_output.strip().splitlines()[-1] if build_output.strip() else "",
        "stderr_tail": build_output[-2000:],
        "verdict": verdict,
    }
    JSON_OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {TXT_OUT.relative_to(ROOT)}")
    print(f"wrote {JSON_OUT.relative_to(ROOT)}")
    print(f"exit_code={build_code} assertions={len(assertions)} verdict={verdict}")
    for item in assertions:
        print(f"  [{'PASS' if item['pass'] else 'FAIL'}] {item['name']} -> {item['actual']}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
