"""Emit the frontend build, Vitest and browser gates from actual tool results."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from gate_evidence import ROOT, git_state

FRONTEND = ROOT / "frontend"
EVIDENCE = ROOT / "artifacts" / "evidence"
WATCHED = (
    "frontend/src",
    "frontend/tests",
    "frontend/e2e",
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/vite.config.ts",
    "frontend/playwright.config.ts",
    "scripts/emit_frontend_evidence.py",
    "scripts/gate_evidence.py",
)


def _binary(name: str) -> str:
    suffix = ".cmd" if os.name == "nt" else ""
    return str(FRONTEND / "node_modules" / ".bin" / f"{name}{suffix}")


def _run(command: list[str], *, timeout: int) -> tuple[int, str, str, int]:
    started = datetime.now(UTC)
    try:
        result = subprocess.run(
            command,
            cwd=FRONTEND,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return (
            result.returncode,
            result.stdout,
            result.stderr,
            int((datetime.now(UTC) - started).total_seconds() * 1000),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
        return 124, stdout, stderr, int((datetime.now(UTC) - started).total_seconds() * 1000)


def _junit(path: Path) -> tuple[list[dict[str, object]], bool]:
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return [], False
    assertions = []
    for case in tree.iter("testcase"):
        outcome = "PASSED"
        if any(child.tag in {"failure", "error", "skipped"} for child in case):
            outcome = "FAILED"
        assertions.append(
            {
                "name": case.get("name") or "<unnamed>",
                "expected": "PASSED",
                "actual": outcome,
                "pass": outcome == "PASSED",
            }
        )
    return assertions, True


def _playwright_cases(suites: list[dict]) -> list[dict[str, object]]:
    assertions: list[dict[str, object]] = []
    for suite in suites:
        for spec in suite.get("specs", []):
            passed = spec.get("ok") is True and all(
                test.get("status") == "expected" for test in spec.get("tests", [])
            )
            assertions.append(
                {
                    "name": spec.get("title", "<unnamed>"),
                    "expected": "PASSED",
                    "actual": "PASSED" if passed else "FAILED",
                    "pass": passed,
                }
            )
        assertions.extend(_playwright_cases(suite.get("suites", [])))
    return assertions


def emit(gate: str) -> int:
    source_state = git_state(WATCHED)
    started = datetime.now(UTC).isoformat()
    if gate == "FG-03":
        proof = EVIDENCE / "build" / "fg03_frontend_build.txt"
        command = ["npm.cmd" if os.name == "nt" else "npm", "run", "build"]
        code, stdout, stderr, duration = _run(command, timeout=600)
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.write_text(stdout + stderr, encoding="utf-8")
        assertions = [{
            "name": "vue-tsc and Vite build",
            "expected": "PASSED",
            "actual": "PASSED" if code == 0 else "FAILED",
            "pass": code == 0,
        }]
        report = None
    elif gate == "FG-20":
        proof = EVIDENCE / "unit" / "fg20_vitest.xml"
        proof.parent.mkdir(parents=True, exist_ok=True)
        proof.unlink(missing_ok=True)
        command = [_binary("vitest"), "run", "--reporter=junit", f"--outputFile={proof}"]
        code, stdout, stderr, duration = _run(command, timeout=600)
        assertions, parses = _junit(proof)
        report = {"path": str(proof.relative_to(ROOT)), "parses": parses, "testcase_count": len(assertions)}
    elif gate == "FG-21":
        proof = EVIDENCE / "e2e" / "fg21_playwright.json"
        proof.parent.mkdir(parents=True, exist_ok=True)
        command = [_binary("playwright"), "test", "--reporter=json"]
        code, stdout, stderr, duration = _run(command, timeout=300)
        raw_path = proof.with_name("fg21_playwright.report.json")
        raw_path.write_text(stdout, encoding="utf-8")
        try:
            raw = json.loads(stdout)
        except ValueError:
            raw = {}
        assertions = _playwright_cases(raw.get("suites", []))
        report = {
            "path": str(raw_path.relative_to(ROOT)),
            "stats": raw.get("stats", {}),
            "parses": bool(raw),
        }
    else:
        raise ValueError(f"unknown gate: {gate}")

    passed = (
        code == 0
        and bool(assertions)
        and all(item["pass"] is True for item in assertions)
        and source_state["relevant_paths_dirty"] is False
        and (report is None or report["parses"] is True)
    )
    evidence = {
        "gate_id": gate,
        "command": command,
        "cwd": str(FRONTEND),
        "timestamp": started,
        "duration_ms": duration,
        "exit_code": code,
        "git": source_state,
        "junit_report": report if gate == "FG-20" else None,
        "browser_report": report if gate == "FG-21" else None,
        "assertions": assertions,
        "stderr_tail": stderr[-2000:],
        "verdict": "PASS" if passed else "FAIL",
    }
    json_path = proof if proof.suffix == ".json" else proof.with_suffix(".json")
    json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"{gate}: {evidence['verdict']} ({len(assertions)} checks, exit_code={code})")
    print(json_path.relative_to(ROOT))
    return 0 if passed else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gate", choices=("FG-03", "FG-20", "FG-21"))
    args = parser.parse_args()
    return emit(args.gate)


if __name__ == "__main__":
    raise SystemExit(main())
