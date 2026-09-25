"""Emit FG-01 evidence: repository state audit.

Runs real git commands against this working tree and records what they actually
returned. Nothing here is asserted from a constant: the branch, the revision, the
subject, the existence of the key config files and the dirty-file counts all come
from a subprocess that was executed during this run.

The verdict is expected to be FAIL while the tree is mid-development. A dirty
trackable tree is a true statement about the repository, and this emitter's job is
to record it - not to launder it into a PASS. The Final Gate only accepts a PASS
when every recorded assertion passed at a clean revision.

Scope note (honest, not a loophole): the workspace check deliberately excludes
``artifacts/``. The evidence tree is written *by the gate run itself*, so including
it would make every gate unable to report a clean tree while running - a check that
can never pass is not a check. ``artifacts/`` being excluded is reported as its own
observation (``artifacts_tracked`` / ``artifacts_untracked``) so a reader can see
exactly what was left out instead of taking it on trust.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from gate_evidence import ROOT, git_state

OUT = ROOT / "artifacts" / "evidence" / "final" / "fg01_repo_state.json"

#: Paths this gate depends on: the audit itself, plus the frozen inventory it is
#: audited against and the scripts that produce the other gates' proof.
#:
#: ``FINAL_GATE.md`` is deliberately NOT in this list even though it is audited
#: for existence below. It is an OUTPUT of ``scripts/final_gate.py``: the gate
#: renders it after the emitters run and it is committed in the evidence commit.
#: Watching it would mark FG-01 STALE for a change that says nothing about the
#: repository state this gate audits - the same reasoning that excludes artifacts/.
RELEVANT_PATHS = (
    "scripts/emit_fg01_evidence.py",
    "scripts/gate_evidence.py",
    "PROJECT_BASELINE.yaml",
    "ops/docker-compose.yml",
    ".env.example",
    "backend/pyproject.toml",
    "backend/alembic.ini",
    "frontend/package.json",
)

#: The frozen key files the repository audit demands to exist.
KEY_FILES = (
    "PROJECT_BASELINE.yaml",
    "FINAL_GATE.md",
    "ops/docker-compose.yml",
    ".env.example",
    "backend/pyproject.toml",
    "backend/alembic.ini",
    "frontend/package.json",
)

#: Directories and files that make up the "trackable source tree" for the dirty
#: check. ``artifacts/`` is deliberately absent (see the module docstring).
TRACKABLE_SCOPE = (
    "backend",
    "frontend",
    "ops",
    "scripts",
    "docs",
    ".env.example",
    ".gitignore",
    "PROJECT_BASELINE.yaml",
    "FINAL_GATE.md",
    "README.md",
    "AGENTS.md",
)


def _git(*args: str) -> tuple[int, str]:
    """Run git and return (exit_code, stdout). A failure is recorded, not hidden."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"git {' '.join(args)} could not run: {exc}"
    return proc.returncode, proc.stdout or ""


def _porcelain(scoped: bool) -> tuple[list[str], list[str], list[str]]:
    """(modified, untracked, other) entries from ``git status --porcelain``."""
    args = ["status", "--porcelain=v1", "--untracked-files=all"]
    if scoped:
        args += ["--", *TRACKABLE_SCOPE]
    code, out = _git(*args)
    if code != 0:
        return [], [], [f"git status failed with exit_code={code}"]
    modified: list[str] = []
    untracked: list[str] = []
    other: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        status, _, path = line[:2], line[2:3], line[3:]
        if status == "??":
            untracked.append(path.strip())
        elif status.strip() and set(status) - {"?", "!"}:
            if set(status) & {"M", "D", "R", "C", "T", "A", "U"}:
                modified.append(path.strip())
            else:
                other.append(path.strip())
    return modified, untracked, other


def main() -> int:
    started = datetime.now(UTC)
    command = (
        f"{sys.executable} scripts/emit_fg01_evidence.py  "
        "[runs: git rev-parse --abbrev-ref HEAD; git rev-parse HEAD; "
        "git log -1 --format=%s; git status --porcelain=v1 --untracked-files=all]"
    )

    branch_code, branch_raw = _git("rev-parse", "--abbrev-ref", "HEAD")
    branch = branch_raw.strip()
    head_code, head_raw = _git("rev-parse", "HEAD")
    head = head_raw.strip()
    subject_code, subject_raw = _git("log", "-1", "--format=%s")
    subject = subject_raw.strip()

    missing = [path for path in KEY_FILES if not (ROOT / path).is_file()]

    modified, untracked, other = _porcelain(scoped=True)
    # Same query, unscoped, so the artifacts/ observation below describes the real
    # tree rather than the scoped subset (which never contains artifacts/ at all).
    _, all_out = _git("status", "--porcelain=v1", "--untracked-files=all")
    artifacts_entries = [
        line for line in all_out.splitlines() if line.strip() and line[3:].strip().startswith("artifacts/")
    ]
    artifacts_tracked = [line for line in artifacts_entries if not line.startswith("??")]
    artifacts_untracked = [line for line in artifacts_entries if line.startswith("??")]

    assertions: list[dict[str, object]] = []

    def check(name: str, expected: object, actual: object) -> None:
        assertions.append(
            {"name": name, "expected": expected, "actual": actual, "pass": actual == expected}
        )

    check("current branch is main", "main", branch)
    check("HEAD resolves to a commit object", True, bool(head) and head_code == 0)
    check("HEAD commit subject is readable", True, bool(subject) and subject_code == 0)
    check("key config files present", [], missing)
    check("no modified tracked files outside artifacts/", 0, len(modified))
    check("no untracked files outside artifacts/", 0, len(untracked))
    check("no unclassifiable git status entries", 0, len(other))
    check("artifacts/ tree is not version-controlled", 0, len(artifacts_tracked))

    reasons: list[str] = []
    if branch != "main":
        reasons.append(f"branch is {branch!r}, not 'main'")
    if head_code != 0 or not head:
        reasons.append(f"git rev-parse HEAD failed with exit_code={head_code}")
    if missing:
        reasons.append(f"missing key config files: {missing}")
    if modified:
        reasons.append(f"{len(modified)} tracked source file(s) modified: {modified[:10]}")
    if untracked:
        reasons.append(f"{len(untracked)} untracked source file(s): {untracked[:10]}")
    if other:
        reasons.append(f"{len(other)} unclassified status entries: {other[:10]}")

    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    exit_code = 0
    verdict = "PASS" if not reasons else "FAIL"

    report = {
        "gate_id": "FG-01",
        "name": "Repository State Audit",
        "command": command,
        "cwd": str(ROOT),
        "timestamp": started.isoformat(),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "git": git_state(RELEVANT_PATHS),
        "repository": {
            "branch": branch,
            "branch_command_exit_code": branch_code,
            "head": head,
            "head_command_exit_code": head_code,
            "head_subject": subject,
            "subject_command_exit_code": subject_code,
        },
        "workspace_check": {
            "scope": list(TRACKABLE_SCOPE),
            "artifacts_excluded": True,
            "artifacts_excluded_reason": (
                "artifacts/ is written by the gate run itself; counting it would make "
                "the clean-tree assertion unpassable during a run."
            ),
            "modified_file_count": len(modified),
            "untracked_file_count": len(untracked),
            "modified_files": modified,
            "untracked_files": untracked,
            "unclassified_files": other,
            "artifacts_untracked": len(artifacts_untracked),
            "artifacts_tracked": len(artifacts_tracked),
        },
        "key_files": {
            "required": list(KEY_FILES),
            "missing": missing,
            "present_count": len(KEY_FILES) - len(missing),
        },
        "assertions": assertions,
        "fail_reasons": reasons,
        "verdict": verdict,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"exit_code={exit_code}  assertions={len(assertions)}  verdict={verdict}")
    for item in assertions:
        print(f"  [{'PASS' if item['pass'] else 'FAIL'}] {item['name']}: {item['actual']!r}")
    for reason in reasons:
        print(f"  ! {reason}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())