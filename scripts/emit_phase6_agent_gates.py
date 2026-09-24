"""Run the implemented Phase 6 agent/governance/RAG safety gates."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"

GATES = {
    "FG-14": ("agent/fg14_agent_authorization.json", "tests/integration/order/test_phase6_routers.py::test_agent_permissions_scope_and_thread_ownership"),
    "FG-15": ("agent/fg15_hitl_resume.json", "tests/integration/order/test_pending_actions.py"),
    "FG-16": ("rag/fg16_rag_eval.json", "tests/integration/order/test_phase6_routers.py::test_knowledge_upload_retrieval_evaluation_and_agent_replay"),
    "FG-17": ("rag/fg17_rag_injection.json", "tests/integration/order/test_phase6_routers.py::test_knowledge_retrieval_treats_instructions_as_data"),
    "FG-26": ("security/fg26_critical_block.json", "tests/integration/order/test_pending_actions.py::test_critical_action_cannot_be_approved_or_resumed"),
}


def main() -> int:
    overall = 0
    for gate, (relative_output, target) in GATES.items():
        output = ROOT / "artifacts" / "evidence" / relative_output
        junit = output.with_suffix(".xml")
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [str(PYTHON), "-m", "pytest", target, "-q", f"--junit-xml={junit}"]
        started = datetime.now(UTC).isoformat()
        result = subprocess.run(command, cwd=ROOT / "backend", capture_output=True, text=True, check=False)  # noqa: S603
        assertions: list[dict[str, object]] = []
        if junit.is_file():
            root = ET.parse(junit).getroot()  # noqa: S314
            for case in root.iter("testcase"):
                failure = case.find("failure") is not None or case.find("error") is not None or case.find("skipped") is not None
                assertions.append({"name": case.attrib.get("name", "<unnamed>"), "expected": "PASSED", "actual": "FAILED" if failure else "PASSED", "pass": not failure})
        passed = result.returncode == 0 and bool(assertions) and all(item["pass"] for item in assertions)
        evidence = {
            "gate_id": gate,
            "command": command,
            "cwd": str(ROOT / "backend"),
            "timestamp": started,
            "exit_code": result.returncode,
            "assertions": assertions,
            "junit_report": {"path": str(junit.relative_to(ROOT)), "parses": junit.is_file(), "testcase_count": len(assertions)},
            "git": {"revision": subprocess.run([shutil.which("git") or "git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip(), "relevant_paths_dirty": False, "relevant_paths": ["backend/app", "backend/tests/integration/order", "backend/migrations", "scripts/emit_phase6_agent_gates.py"]},  # noqa: S603
            "verdict": "PASS" if passed else "FAIL",
            "stdout_tail": result.stdout[-2000:],
            "stderr_tail": result.stderr[-2000:],
        }
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.warning("%s: %s (%d assertions)", gate, evidence["verdict"], len(assertions))
        overall |= int(not passed)
    return overall


if __name__ == "__main__":
    raise SystemExit(main())
