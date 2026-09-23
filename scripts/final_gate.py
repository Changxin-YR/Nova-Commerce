"""Regenerate FINAL_GATE.md from the frozen gate inventory and run artifacts."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _artifact_status(proof: Path) -> tuple[str, str]:
    if not proof.is_file():
        return "MISSING", "No proof artifact"
    evidence = proof if proof.suffix == ".json" else proof.with_suffix(".json")
    if not evidence.is_file():
        return "UNVERIFIED", "No machine-readable run artifact"
    try:
        data = json.loads(evidence.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "UNVERIFIED", "Run artifact cannot be parsed"
    if data.get("exit_code") is None or not isinstance(data.get("assertions"), list):
        return "UNVERIFIED", "Exit code or assertions absent"
    assertions = data["assertions"]
    if not assertions:
        return "UNVERIFIED", "No recorded assertions"
    report = data.get("junit_report")
    if report is not None and (
        not isinstance(report, dict)
        or not report.get("parses")
        or report.get("testcase_count") != len(assertions)
    ):
        return "FAIL", "JUnit report disagrees with assertions"
    if data["exit_code"] != 0 or any(
        item.get("pass") is not True for item in assertions
    ):
        return "FAIL", "A recorded assertion or process failed"

    git = data.get("git")
    if not isinstance(git, dict) or git.get("relevant_paths_dirty") is not False:
        return "UNVERIFIED", "Run lacks a clean watched-path revision"
    revision = str(git.get("revision", "")).strip()
    paths = git.get("relevant_paths")
    if not revision or not isinstance(paths, list) or not paths:
        return "UNVERIFIED", "Run lacks watched paths"
    if _git("cat-file", "-e", f"{revision}^{{commit}}") is None:
        return "UNVERIFIED", "Recorded revision is unavailable"
    watched = [str(path) for path in paths]
    changed = _git("diff", "--name-only", revision, "HEAD", "--", *watched)
    dirty = _git("status", "--porcelain", "--", *watched)
    if changed is None or dirty is None:
        return "UNVERIFIED", "Cannot check watched paths"
    if changed or dirty:
        return "STALE", "Watched paths changed since the run"
    return "PASS", f"{len(assertions)} assertions at {revision[:7]}"


def render() -> str:
    baseline = yaml.safe_load(
        (ROOT / "PROJECT_BASELINE.yaml").read_text(encoding="utf-8-sig")
    )
    mandatory = {entry["gate"] for entry in baseline["non_waivable"]}
    rows: list[str] = []
    states: list[str] = []
    for gate in baseline["final_gates"]:
        proof = Path(gate["proof"])
        state, reason = _artifact_status(ROOT / proof)
        states.append(state)
        rows.append(
            f"| {gate['id']} | {gate['name']} | "
            f"{'Yes' if gate['id'] in mandatory else ''} | "
            f"[{proof.as_posix()}]({proof.as_posix()}) | {state} | {reason} |"
        )
    status = (
        "FAIL"
        if "FAIL" in states
        else "PASS"
        if all(s == "PASS" for s in states)
        else "IN PROGRESS"
    )
    revision = _git("rev-parse", "--short", "HEAD") or "unknown"
    return "\n".join(
        [
            "# FINAL GATE",
            "",
            "This is an index of run artifacts. A gate passes only when its recorded tests",
            "passed and its watched paths still match the tested revision.",
            "",
            f"Repository revision: `{revision}`",
            f"**PROJECT STATUS: {status}**",
            "",
            "| Gate | Name | Mandatory | Artifact | Status | Evidence check |",
            "|---|---|---|---|---|---|",
            *rows,
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "FINAL_GATE.md")
    args = parser.parse_args()
    output = args.out if args.out.is_absolute() else ROOT / args.out
    output.write_text(render(), encoding="utf-8", newline="\n")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
