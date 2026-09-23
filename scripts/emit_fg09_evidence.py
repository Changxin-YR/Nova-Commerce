"""Emit the FG-09 evidence artifact from a real run.

Spec section 144 requires each gate to produce a machine-readable report with the
command, timestamp, exit code and per-assertion results. This runs the real test
file against real MySQL and records what actually happened - it never writes a
verdict it did not observe.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
from datetime import UTC, datetime

from gate_evidence import git_state

ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PYTHON = pathlib.Path(sys.executable)
OUT = ROOT / "artifacts" / "evidence" / "concurrency" / "fg09_inventory_over_sell.json"

CMD = [
    str(PYTHON), "-m", "pytest",
    "tests/concurrency/test_inventory_oversell.py",
    "-v", "--no-header", "-p", "no:cacheprovider",
    "-m", "concurrency or integration",
]

started = datetime.now(UTC)
proc = subprocess.run(CMD, cwd=BACKEND, capture_output=True, text=True, timeout=900, check=False)
duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
stdout, stderr = proc.stdout or "", proc.stderr or ""

# Parse the real per-test outcomes out of pytest's own report.
assertions = []
for line in stdout.splitlines():
    match = re.match(r"^\S+::(\S+)\s+(PASSED|FAILED|ERROR|SKIPPED)", line.strip())
    if match:
        name, outcome = match.group(1), match.group(2)
        assertions.append({
            "name": name,
            "expected": "PASSED",
            "actual": outcome,
            "pass": outcome == "PASSED",
        })

summary = re.search(r"=+ (.*?) =+", stdout.strip().splitlines()[-1] if stdout.strip() else "")
source_state = git_state((
    "backend/app",
    "backend/migrations",
    "backend/tests/concurrency/test_inventory_oversell.py",
    "scripts/emit_fg09_evidence.py",
    "scripts/gate_evidence.py",
))
verdict = (
    "PASS"
    if proc.returncode == 0
    and assertions
    and all(a["pass"] for a in assertions)
    and not source_state["relevant_paths_dirty"]
    else "FAIL"
)

OUT.parent.mkdir(parents=True, exist_ok=True)
report = {
    "gate_id": "FG-09",
    "name": "Inventory Concurrency",
    "mandatory": True,
    "spec": "section 113 - 1 unit of stock, 20 concurrent CreateOrder, exactly 1 succeeds",
    "command": " ".join(CMD),
    "cwd": str(BACKEND),
    "timestamp": started.isoformat(),
    "duration_ms": duration_ms,
    "exit_code": proc.returncode,
    "git": source_state,
    "summary": summary.group(1).strip() if summary else "",
    "infrastructure": {
        "engine": "MySQL 8.4 (real container, not mocked)",
        "why_real": (
            "The property under test is a database behaviour - SELECT ... FOR UPDATE "
            "taking an exclusive row lock, and CHECK (available_qty >= 0) refusing a "
            "bad write. Neither exists in a mock, so spec section 113 forbids proving "
            "this gate against one."
        ),
    },
    "assertions": assertions,
    "negative_control": {
        "name": "test_naive_read_then_write_DOES_oversell",
        "purpose": (
            "Proves the lock is doing the work. It deliberately implements the wrong "
            "algorithm (read, decide, write, no lock) and forces the interleaving with "
            "a barrier; it must oversell. If it did not, the positive test would be "
            "passing because the load was too gentle, not because the code is correct."
        ),
        "result": next(
            (a["actual"] for a in assertions if a["name"] == "test_naive_read_then_write_DOES_oversell"),
            "MISSING",
        ),
    },
    "stderr_tail": stderr[-2000:] if stderr else "",
    "verdict": verdict,
}
OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"wrote {OUT.relative_to(ROOT)}")
print(f"exit_code={proc.returncode}  assertions={len(assertions)}  verdict={verdict}")
for a in assertions:
    print(f"  [{'PASS' if a['pass'] else 'FAIL'}] {a['name']}")
sys.exit(0 if verdict == "PASS" else 1)
