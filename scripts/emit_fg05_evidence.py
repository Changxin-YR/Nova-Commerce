"""Emit the FG-05 (Alembic migration) evidence artifact from real runs.

Spec section 129: Alembic is the sole production schema authority - no
``create_all``, no schema mutation at boot. The gate must therefore prove two
things against the real database, not assert them:

  1. ``alembic check`` reports **no** pending autogenerate operations, i.e. the
     models and the migrated schema have not drifted apart; and
  2. ``alembic upgrade head`` is clean and ``alembic current`` then reports the
     same revision as ``alembic heads`` - the database is at the head revision.

Why "no drift" is the assertion that matters: a green test suite against a schema
that differs from the migrations means the next deploy breaks. ``alembic check``
is the only statement that compares the two, so its own output is recorded
verbatim rather than summarised into a boolean.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime

from gate_evidence import ROOT, git_state

PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe")
BACKEND = ROOT / "backend"
TXT_OUT = ROOT / "artifacts" / "evidence" / "migration" / "fg05_alembic.txt"
JSON_OUT = TXT_OUT.with_suffix(".json")

_HEAD_RE = re.compile(r"([0-9a-f]{6,})")


def _alembic(*args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            [PYTHON, "-m", "alembic", *args],
            cwd=BACKEND,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"alembic {' '.join(args)} exceeded the 600s cap\n"
    except OSError as exc:
        return 127, f"could not execute {PYTHON}: {exc}\n"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    started = datetime.now(UTC)
    TXT_OUT.parent.mkdir(parents=True, exist_ok=True)

    assertions: list[dict[str, object]] = []

    def check(name: str, passed: bool, actual: str) -> None:
        assertions.append({"name": name, "expected": "PASSED", "actual": actual, "pass": passed})

    outputs: list[str] = []

    # --- 1. no model/schema drift ------------------------------------------
    check_code, check_out = _alembic("check")
    outputs.append(f"# $ alembic check  -> exit {check_code}\n{check_out}")
    check("alembic check exits 0 (no autogenerate drift)", check_code == 0, f"exit_code={check_code}")
    no_ops = "No new upgrade operations detected" in check_out
    check(
        "alembic check reports 'No new upgrade operations detected'",
        no_ops,
        "reported" if no_ops else "not reported",
    )

    # --- 2. the database is at the head revision ---------------------------
    upgrade_code, upgrade_out = _alembic("upgrade", "head")
    outputs.append(f"# $ alembic upgrade head  -> exit {upgrade_code}\n{upgrade_out}")
    check("alembic upgrade head exits 0", upgrade_code == 0, f"exit_code={upgrade_code}")

    head_code, head_out = _alembic("heads")
    outputs.append(f"# $ alembic heads  -> exit {head_code}\n{head_out}")
    heads = {match.group(1) for line in head_out.splitlines() for match in [_HEAD_RE.match(line.strip())] if match}
    check("alembic heads reports exactly one head", head_code == 0 and len(heads) == 1, f"heads={sorted(heads)}")

    current_code, current_out = _alembic("current")
    outputs.append(f"# $ alembic current  -> exit {current_code}\n{current_out}")
    current_revisions = {
        match.group(1) for line in current_out.splitlines() for match in [_HEAD_RE.match(line.strip())] if match
    }
    at_head = bool(heads) and heads.issubset(current_revisions)
    check(
        "database current revision equals the head revision",
        current_code == 0 and at_head,
        f"current={sorted(current_revisions)} head={sorted(heads)}",
    )

    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    source_state = git_state(
        (
            "backend/alembic.ini",
            "backend/migrations",
            "backend/app/shared/db",
            "backend/app/modules",
            "scripts/emit_fg05_evidence.py",
            "scripts/gate_evidence.py",
        )
    )

    verdict = (
        "PASS"
        if all(item["pass"] is True for item in assertions) and source_state["relevant_paths_dirty"] is False
        else "FAIL"
    )

    TXT_OUT.write_text(
        "\n".join(
            [
                "# FG-05 Alembic migration",
                f"# cwd: {BACKEND}",
                f"# started: {started.isoformat()}",
                f"# exit codes: check={check_code} upgrade={upgrade_code} heads={head_code} current={current_code}",
                "",
                "\n\n".join(outputs),
            ]
        ),
        encoding="utf-8",
    )

    report = {
        "gate_id": "FG-05",
        "name": "Alembic Migration",
        "mandatory": False,
        "spec": "section 129 - Alembic is the sole schema authority; models and migrations do not drift",
        "command": f"{PYTHON} -m alembic check && {PYTHON} -m alembic upgrade head && {PYTHON} -m alembic current",
        "cwd": str(BACKEND),
        "timestamp": started.isoformat(),
        "duration_ms": duration_ms,
        "exit_code": check_code if check_code != 0 else upgrade_code,
        "heads": sorted(heads),
        "current": sorted(current_revisions),
        "git": source_state,
        "assertions": assertions,
        "junit_report": None,
        "summary": "no autogenerate drift; database at head",
        "stderr_tail": (check_out + upgrade_out + current_out)[-2000:],
        "verdict": verdict,
    }
    JSON_OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"wrote {TXT_OUT.relative_to(ROOT)}")
    print(f"wrote {JSON_OUT.relative_to(ROOT)}")
    print(f"assertions={len(assertions)} verdict={verdict}")
    for item in assertions:
        print(f"  [{'PASS' if item['pass'] else 'FAIL'}] {item['name']} -> {item['actual']}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
