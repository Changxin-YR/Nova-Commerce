"""Emit the FG-10 evidence artifact from a real run.

Spec section 144 requires each gate to produce a machine-readable report with the
command, timestamp, exit code and per-assertion results. Spec section 145 is
blunter still: FINAL_GATE.md is an *echo* and must never be treated as evidence.
This script is therefore the thing that actually produces the FG-10 proof, by
running the real order-workflow tests against real MySQL and recording what
happened - it never writes a verdict it did not observe.

FG-10 is mandatory-4 (PROJECT_BASELINE.yaml): "Order snapshot correctness" -
product edits must never mutate a historical order. The frozen proof path is
``artifacts/evidence/integration/fg10_workflow.xml`` (a JUnit report), so this
emitter writes *both* the XML that the baseline names and a JSON sidecar holding
the richer fields (per-assertion results, the infrastructure rationale, the
recomputed verdict).

Why the JUnit file is not merely a copy of the JSON
---------------------------------------------------
A pytest run can fail *before* it executes a single test - a syntax error in the
test module, an import of a module that does not exist yet, or a wrong path. In
every one of those cases pytest still writes a JUnit file, it just contains zero
``<testcase>`` elements. An emitter that only looked at the process exit code
would be able to report whatever it liked. So the verdict below requires, all at
once:

  * the test file exists on disk;
  * pytest actually reported per-test outcomes;
  * the JUnit XML parses and contains the same number of tests;
  * every observed outcome is PASSED;
  * the process exited 0.

Zero observations is a FAIL, never a vacuous PASS. That single rule is what makes
this artifact worth committing.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime

ROOT = pathlib.Path(r"C:\Users\27363\Desktop\store")
BACKEND = ROOT / "backend"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

#: The frozen FG-10 target, owned by the order-flow implementer (task t3). This
#: emitter deliberately does not create it: a gate that supplies its own test is
#: no longer an independent check.
TEST_FILE = BACKEND / "tests" / "integration" / "order" / "test_order_workflow.py"
#: Repo-relative form for the recorded command, so the artifact is readable.
TEST_TARGET = "tests/integration/order/test_order_workflow.py"

#: PROJECT_BASELINE.yaml names this exact path as the FG-10 proof.
XML_OUT = ROOT / "artifacts" / "evidence" / "integration" / "fg10_workflow.xml"
JSON_OUT = ROOT / "artifacts" / "evidence" / "integration" / "fg10_workflow.json"

#: pytest's own per-test verbose line, e.g.
#: ``tests/integration/order/test_order_workflow.py::test_x PASSED [ 50%]``.
#: Outcomes are read from pytest's report rather than our own bookkeeping.
_OUTCOME_RE = re.compile(r"^\S+::(\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)")
#: The trailing ``===== 8 passed in 1.23s =====`` banner.
_SUMMARY_RE = re.compile(r"^=+ (.*?) =+$")


def _parse_stdout_outcomes(stdout: str) -> list[dict[str, object]]:
    """Per-test outcomes, taken from pytest's own report."""
    assertions: list[dict[str, object]] = []
    for line in stdout.splitlines():
        match = _OUTCOME_RE.match(line.strip())
        if not match:
            continue
        name, outcome = match.group(1), match.group(2)
        assertions.append(
            {
                "name": name,
                "expected": "PASSED",
                "actual": outcome,
                "pass": outcome == "PASSED",
            }
        )
    return assertions


def _parse_junit(xml_path: pathlib.Path) -> dict[str, object]:
    """Read back what pytest wrote, so the artifact is checked, not assumed.

    Returns a report of the JUnit file itself: whether it exists, whether it
    parses, and the per-test outcomes found inside it.
    """
    result: dict[str, object] = {
        "path": str(xml_path.relative_to(ROOT)),
        "exists": xml_path.is_file(),
        "parses": False,
        "testcase_count": 0,
        "outcomes": {},
        "error": None,
    }
    if not xml_path.is_file():
        result["error"] = "pytest did not write a JUnit report"
        return result

    try:
        # The file was written by the pytest we just ran, in this process tree;
        # it is not untrusted input.
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        result["error"] = f"JUnit report could not be parsed: {exc}"
        return result

    result["parses"] = True
    outcomes: dict[str, str] = {}
    count = 0
    for case in tree.iter("testcase"):
        count += 1
        name = case.get("name") or "<unnamed>"
        outcome = "PASSED"
        for child in case:
            if child.tag in {"failure", "error", "skipped"}:
                outcome = {
                    "failure": "FAILED",
                    "error": "ERROR",
                    "skipped": "SKIPPED",
                }[child.tag]
                break
        outcomes[name] = outcome
    result["testcase_count"] = count
    result["outcomes"] = outcomes
    return result


def _summary_from_stdout(stdout: str) -> str:
    for line in reversed(stdout.splitlines()):
        match = _SUMMARY_RE.match(line.strip())
        if match:
            return match.group(1).strip()
    return ""


def _build_plan(test_target: str, xml_out: pathlib.Path) -> list[str]:
    """The real pytest invocation.

    ``--junit-xml`` is passed as an ABSOLUTE path on purpose. pytest resolves a
    relative ``--junit-xml`` against its own working directory, and we run pytest
    with ``cwd=BACKEND`` - so a relative path quietly writes the report to
    ``backend/artifacts/...`` instead of the frozen proof path named in
    PROJECT_BASELINE.yaml. The artifact would then be missing and the gate would
    report FAIL forever with no obvious cause.
    """
    return [
        str(PYTHON),
        "-m",
        "pytest",
        test_target,
        "-v",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "-m",
        "integration",
        f"--junit-xml={xml_out}",
    ]


def _infrastructure_note() -> dict[str, str]:
    """Why real MySQL, in the words of the gate that requires it."""
    return {
        "engine": "MySQL 8.4 (real container, not mocked)",
        "why_real": (
            "FG-10 is mandatory condition 4 (order snapshot correctness) and the gate "
            "is only meaningful against a real engine. The properties under test are "
            "database behaviours that do not exist in a mock: SELECT ... FOR UPDATE "
            "taking an exclusive lock on the inventory rows, CHECK constraints refusing "
            "a bad amount, the UNIQUE (user_id, client_request_id) constraint acting as "
            "the second idempotency guard, and the rows a later product edit must not "
            "be able to change. Spec section 113 forbids proving this gate against a "
            "mock, because a mock that agrees with the implementation proves only that "
            "the implementation agrees with itself."
        ),
        "independence": (
            "The snapshot-immutability assertion (INV-014) re-reads the committed order "
            "after mutating the product/sku rows, so it observes durable state rather "
            "than the ORM's in-memory view."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Emit the FG-10 evidence artifact.")
    parser.add_argument(
        "--test-target",
        default=TEST_TARGET,
        help=(
            "pytest target, relative to backend/ (default: the frozen FG-10 test module). "
            "Use this if the implementer's module lands under a different name."
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=900,
        help="hard wall-clock cap for the pytest run (default: 900)",
    )
    args = parser.parse_args(argv)

    xml_out = XML_OUT
    #: The frozen proof path is absolute under the repo root. Record a readable
    #: repo-relative form in the artifact, but execute with the absolute one.
    try:
        xml_display = str(xml_out.relative_to(ROOT))
    except ValueError:  # pragma: no cover - only reachable if ROOT is reconfigured
        xml_display = str(xml_out)

    plan = _build_plan(args.test_target, xml_out)
    command = " ".join(plan).replace(str(xml_out), xml_display)

    xml_out.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)

    # A stale artifact from a previous run must not be mistakable for this run's.
    for stale in (xml_out, JSON_OUT):
        if stale.is_file():
            stale.unlink()

    test_file_exists = (BACKEND / args.test_target).is_file()

    started = datetime.now(UTC)
    timed_out = False
    if not test_file_exists:
        # Do not invent a run. Record the truth: the gate has nothing to execute.
        stdout, stderr, exit_code = "", f"test target not found: {BACKEND / args.test_target}\n", 4
    else:
        try:
            # Fixed argv, no shell=True, no shell interpolation.
            proc = subprocess.run(
                plan,
                cwd=BACKEND,
                capture_output=True,
                text=True,
                timeout=args.timeout_seconds,
                check=False,
            )
            stdout, stderr, exit_code = proc.stdout or "", proc.stderr or "", proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout or "" if isinstance(exc.stdout, str) else ""
            stderr = f"pytest exceeded the {args.timeout_seconds}s cap\n"
            exit_code = 124
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)

    assertions = _parse_stdout_outcomes(stdout)
    junit = _parse_junit(xml_out)

    # The JUnit report is the frozen proof, so a mismatch between it and the
    # stdout parse means our parsing is wrong - not that the tests passed.
    junit_outcomes = junit.get("outcomes") or {}
    counts_agree = isinstance(junit_outcomes, dict) and len(junit_outcomes) == len(assertions)

    failures = [a for a in assertions if not a["pass"]]
    reasons: list[str] = []
    if not test_file_exists:
        reasons.append(f"test target not found: {args.test_target}")
    if timed_out:
        reasons.append(f"pytest timed out after {args.timeout_seconds}s")
    if not assertions:
        reasons.append("pytest observed zero test outcomes (nothing was executed)")
    if not junit.get("parses"):
        reasons.append(f"JUnit report unusable: {junit.get('error')}")
    if junit.get("testcase_count", 0) != len(assertions):
        reasons.append(
            f"JUnit testcase count ({junit.get('testcase_count')}) disagrees with the "
            f"outcomes parsed from stdout ({len(assertions)})"
        )
    elif not counts_agree:
        reasons.append("JUnit outcomes disagree with the outcomes parsed from stdout")
    if failures:
        reasons.append(f"{len(failures)} assertion(s) did not pass")
    if exit_code != 0:
        reasons.append(f"pytest exit_code={exit_code}")

    verdict = "PASS" if not reasons else "FAIL"

    report = {
        "gate_id": "FG-10",
        "name": "Workflow Tests",
        "mandatory": True,
        "spec": (
            "section 113 - CreateOrder workflow on real MySQL, including the "
            "snapshot-immutability test; mandatory condition 4 of section 147 "
            "(order snapshot correctness)"
        ),
        "command": command,
        "cwd": str(BACKEND),
        "timestamp": started.isoformat(),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "summary": _summary_from_stdout(stdout),
        "verdict": verdict,
        "fail_reasons": reasons,
        "infrastructure": _infrastructure_note(),
        "junit_report": junit,
        "assertions": assertions,
        "stderr_tail": stderr[-2000:] if stderr else "",
    }
    JSON_OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {JSON_OUT.relative_to(ROOT)}")
    if xml_out.is_file():
        print(f"wrote {xml_out.relative_to(ROOT)}")
    print(
        f"exit_code={exit_code}  assertions={len(assertions)}  "
        f"junit_testcases={junit.get('testcase_count')}  verdict={verdict}"
    )
    for reason in reasons:
        print(f"  ! {reason}")
    for assertion in assertions:
        print(f"  [{'PASS' if assertion['pass'] else 'FAIL'}] {assertion['name']}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
