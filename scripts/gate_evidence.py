"""Shared core for the "run a real pytest target and record what happened" gates.

Spec section 144 requires every gate to produce a machine-readable report with the
command, timestamp, exit code and per-assertion results; spec section 145 is blunter
still: ``FINAL_GATE.md`` is an echo and must never be treated as evidence. The
per-gate emitter scripts are the things that actually produce proof, by running a
real test target against real infrastructure and recording what happened - they never
write a verdict they did not observe.

## Why the verdict is derived, not asserted

A pytest run can fail *before* it executes a single test: a syntax error, an import
of a module that does not exist yet, or a wrong path. In every one of those cases
pytest still writes a JUnit file - it just contains zero ``<testcase>`` elements. An
emitter that only looked at the process exit code could therefore report whatever it
liked. So the verdict requires, all at once:

  * the test target exists on disk;
  * pytest reported per-test outcomes;
  * the JUnit XML parses and contains the same number of tests;
  * every observed outcome is PASSED;
  * the process exited 0.

Zero observations is a FAIL, never a vacuous PASS. That single rule is what makes
these artifacts worth committing, and it is shared here rather than re-implemented
per gate so the two Phase 5 gates cannot drift apart in how strict they are.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PYTHON = pathlib.Path(sys.executable)

#: pytest's own per-test verbose line, e.g.
#: ``tests/integration/order/test_x.py::test_y PASSED [ 50%]``.
_OUTCOME_RE = re.compile(r"^\S+::(.+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s|$)")
#: The trailing ``===== 8 passed in 1.23s =====`` banner.
_SUMMARY_RE = re.compile(r"^=+ (.*?) =+$")


@dataclass(frozen=True, slots=True)
class Gate:
    """Everything one gate emitter must decide, and nothing about how it runs."""

    gate_id: str
    name: str
    mandatory: bool
    spec: str
    #: pytest target(s), relative to ``backend/`` (files or directories).
    #:
    #: A tuple rather than a space-joined string because the plan is built as a fixed
    #: argv with no shell, so a space inside one element would be a single bogus path.
    test_target: str
    #: Additional targets. ``test_target`` is always included, so a gate with one
    #: target needs only that field.
    extra_targets: tuple[str, ...] = ()
    #: pytest marker expression, e.g. ``integration`` or ``concurrency``.
    marker: str = "integration"
    #: The paths whose dirty/clean state decides whether this run is reproducible.
    #:
    #: NOT the whole repo. The first version of this check scoped to
    #: ``backend/{app,tests,migrations}``, which meant a gate could never produce a
    #: PASS while any teammate had uncommitted work anywhere - and during a phase
    #: with four writers that is always. FG-11 was emitted green and recorded FAIL
    #: because another author's unfinished module was on disk, which is a true
    #: statement about the tree and a false one about the gate.
    #:
    #: The gate's RESULT depends on the application code, the migration state, and
    #: its own test file. It does not depend on another author's uncommitted tests,
    #: so those are reported as information and do not decide the verdict.
    relevant_paths: tuple[str, ...] = ()
    json_out: pathlib.Path = field(default=pathlib.Path())
    #: Set for gates whose frozen proof path is a JUnit XML file.
    xml_out: pathlib.Path | None = None
    timeout_seconds: int = 1800
    infrastructure: dict[str, str] = field(default_factory=dict)


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


def _parse_junit(xml_path: pathlib.Path | None) -> dict[str, object] | None:
    """Read back what pytest wrote, so the artifact is checked, not assumed."""
    if xml_path is None:
        return None
    result: dict[str, object] = {
        "path": str(xml_path.relative_to(ROOT)),
        "exists": xml_path.is_file(),
        "parses": False,
        "testcase_count": 0,
        "outcomes": [],
        "error": None,
    }
    if not xml_path.is_file():
        result["error"] = "pytest did not write a JUnit report"
        return result
    try:
        # Written by the pytest we just ran, in this process tree; not untrusted
        # input.
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        result["error"] = f"JUnit report could not be parsed: {exc}"
        return result
    result["parses"] = True
    outcomes: list[dict[str, str]] = []
    count = 0
    for case in tree.iter("testcase"):
        count += 1
        name = case.get("name") or "<unnamed>"
        outcome = "PASSED"
        for child in case:
            if child.tag in {"failure", "error", "skipped"}:
                outcome = {"failure": "FAILED", "error": "ERROR", "skipped": "SKIPPED"}[child.tag]
                break
        outcomes.append({"name": name, "actual": outcome})
    result["testcase_count"] = count
    result["outcomes"] = outcomes
    return result


def _summary_from_stdout(stdout: str) -> str:
    for line in reversed(stdout.splitlines()):
        match = _SUMMARY_RE.match(line.strip())
        if match:
            return match.group(1).strip()
    return ""


def git_state(relevant_paths: tuple[str, ...] = ()) -> dict[str, object]:
    """The revision this artifact was produced against, and whether it was dirty.

    An evidence file that says "PASS" without saying *what* passed is only half a
    proof. Concretely: FG-11 was emitted once from a tree that was later found to
    be flaky three runs in ten, and the artifact gave no way to tell which revision
    it described - so it could not be retired, only suspected. Recording the commit
    makes a stale artifact identifiable instead of ambiguous.

    ``dirty`` is recorded per-path rather than repo-wide because four authors share
    this working tree during a phase: a repo-wide dirty flag is always true and
    therefore says nothing, while "the tested paths were modified relative to the
    recorded commit" is the fact a reader actually needs.
    """
    revision = _run_git(["rev-parse", "HEAD"])
    subject = _run_git(["log", "-1", "--format=%s", "HEAD"])
    paths = list(relevant_paths) or ["backend/app", "backend/migrations"]
    dirty = _run_git(["status", "--porcelain", "--", *paths])
    # Information, not a verdict input: what else is dirty in the shared tree. During
    # a phase with four writers this is usually non-empty, which is exactly why it
    # must not decide whether this gate is reproducible.
    others = _run_git(["status", "--porcelain", "--", "backend", "scripts", "docs"])
    return {
        "revision": revision,
        "subject": subject,
        "relevant_paths": paths,
        "relevant_paths_dirty": bool(dirty.strip()),
        "relevant_paths_dirty_files": [line for line in dirty.splitlines() if line.strip()],
        "other_paths_dirty": bool(others.strip()),
        "other_paths_dirty_files": [line for line in others.splitlines() if line.strip()],
    }


def _run_git(args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout or ""


def _build_plan(gate: Gate) -> tuple[list[str], str]:
    """The real pytest invocation, and the readable command recorded in the artifact."""
    xml_out = gate.xml_out
    targets = (gate.test_target, *gate.extra_targets)
    plan = [
        str(PYTHON),
        "-m",
        "pytest",
        *targets,
        "-v",
        "--no-header",
        "-p",
        "no:cacheprovider",
        "-m",
        gate.marker,
    ]
    if xml_out is not None:
        # ABSOLUTE path on purpose: pytest resolves a relative ``--junit-xml``
        # against its own working directory, and we run it with ``cwd=BACKEND`` -
        # so a relative path would quietly write the report under ``backend/``
        # instead of the frozen proof path named in PROJECT_BASELINE.yaml.
        plan.append(f"--junit-xml={xml_out}")
        command = " ".join(plan).replace(str(xml_out), str(xml_out.relative_to(ROOT)))
    else:
        command = " ".join(plan)
    return plan, command


def emit(gate: Gate, argv: list[str] | None = None) -> int:
    """Run the gate and write its artifact. Returns a process exit code."""
    import argparse

    parser = argparse.ArgumentParser(description=f"Emit the {gate.gate_id} evidence artifact.")
    parser.add_argument("--test-target", default=gate.test_target)
    parser.add_argument("--timeout-seconds", type=int, default=gate.timeout_seconds)
    args = parser.parse_args(argv)
    gate = Gate(
        gate_id=gate.gate_id,
        name=gate.name,
        mandatory=gate.mandatory,
        spec=gate.spec,
        test_target=args.test_target,
        extra_targets=gate.extra_targets,
        marker=gate.marker,
        # Carried explicitly: this reconstruction is what applies --test-target, and
        # omitting a field here silently reverts it to its default. relevant_paths
        # was omitted on the first pass, so the scoped dirty check fell back to
        # backend/app and reported another author's module as the gate's problem.
        relevant_paths=gate.relevant_paths,
        json_out=gate.json_out,
        xml_out=gate.xml_out,
        timeout_seconds=args.timeout_seconds,
        infrastructure=gate.infrastructure,
    )

    gate.json_out.parent.mkdir(parents=True, exist_ok=True)
    if gate.xml_out is not None:
        gate.xml_out.parent.mkdir(parents=True, exist_ok=True)

    # A stale artifact from a previous run must not be mistakable for this run's.
    for stale in (gate.json_out, gate.xml_out):
        if stale is not None and stale.is_file():
            stale.unlink()

    plan, command = _build_plan(gate)
    targets = (gate.test_target, *gate.extra_targets)
    target_exists = all((BACKEND / target).exists() for target in targets)

    started = datetime.now(UTC)
    timed_out = False
    if not target_exists:
        stdout = ""
        stderr = f"test target not found: {BACKEND / gate.test_target}\n"
        exit_code = 4
    else:
        try:
            # Fixed argv, no shell=True, no shell interpolation.
            proc = subprocess.run(
                plan,
                cwd=BACKEND,
                capture_output=True,
                text=True,
                timeout=gate.timeout_seconds,
                check=False,
            )
            stdout, stderr, exit_code = proc.stdout or "", proc.stderr or "", proc.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = f"pytest exceeded the {gate.timeout_seconds}s cap\n"
            exit_code = 124
    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)

    assertions = _parse_stdout_outcomes(stdout)
    junit = _parse_junit(gate.xml_out)

    reasons: list[str] = []
    if not target_exists:
        missing = [t for t in targets if not (BACKEND / t).exists()]
        reasons.append(f"test target(s) not found: {missing}")
    if timed_out:
        reasons.append(f"pytest timed out after {gate.timeout_seconds}s")
    if not assertions:
        reasons.append("pytest observed zero test outcomes (nothing was executed)")
    if junit is not None:
        junit_outcomes = junit.get("outcomes") or []
        if not junit.get("parses"):
            reasons.append(f"JUnit report unusable: {junit.get('error')}")
        if junit.get("testcase_count", 0) != len(assertions):
            reasons.append(
                f"JUnit testcase count ({junit.get('testcase_count')}) disagrees with the "
                f"outcomes parsed from stdout ({len(assertions)})"
            )
        elif not isinstance(junit_outcomes, list) or Counter(
            (item["name"], item["actual"]) for item in junit_outcomes
        ) != Counter((item["name"], item["actual"]) for item in assertions):
            reasons.append("JUnit outcomes disagree with the outcomes parsed from stdout")
    failures = [a for a in assertions if not a["pass"]]
    if failures:
        reasons.append(f"{len(failures)} assertion(s) did not pass")
    if exit_code != 0:
        reasons.append(f"pytest exit_code={exit_code}")

    source_state = git_state(gate.relevant_paths)
    if source_state["relevant_paths_dirty"]:
        # Not a gate failure - the code may be perfectly correct - but it must not
        # be reported as PASS, because a reader cannot reproduce the run from the
        # recorded revision. Phase 5's own rule: a verdict that was not observed
        # against an identifiable tree is not evidence.
        reasons.append(
            "the paths this gate depends on were modified relative to the recorded "
            f"commit ({source_state['revision']}): "
            + ", ".join(source_state["relevant_paths_dirty_files"])
        )

    verdict = "PASS" if not reasons else "FAIL"

    report = {
        "gate_id": gate.gate_id,
        "name": gate.name,
        "mandatory": gate.mandatory,
        "spec": gate.spec,
        "command": command,
        "cwd": str(BACKEND),
        "timestamp": started.isoformat(),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "summary": _summary_from_stdout(stdout),
        "verdict": verdict,
        "fail_reasons": reasons,
        "git": source_state,
        "infrastructure": gate.infrastructure,
        "junit_report": junit,
        "assertions": assertions,
        "stderr_tail": stderr[-2000:] if stderr else "",
    }
    gate.json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {gate.json_out.relative_to(ROOT)}")
    if gate.xml_out is not None and gate.xml_out.is_file():
        print(f"wrote {gate.xml_out.relative_to(ROOT)}")
    testcase_count = junit.get("testcase_count") if junit else "n/a"
    print(
        f"exit_code={exit_code}  assertions={len(assertions)}  "
        f"junit_testcases={testcase_count}  verdict={verdict}"
    )
    for reason in reasons:
        print(f"  ! {reason}")
    for assertion in failures:
        print(f"  [FAIL] {assertion['name']}")
    return 0 if verdict == "PASS" else 1


def main_for(gate_factory) -> None:  # pragma: no cover - thin script entry point
    sys.exit(emit(gate_factory()))
