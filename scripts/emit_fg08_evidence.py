"""Emit the MySQL-backed integration gate from a fresh pytest run."""

from __future__ import annotations

from gate_evidence import ROOT, Gate, emit

GATE = Gate(
    gate_id="FG-08",
    name="Integration Tests",
    mandatory=False,
    spec="Spec §146: backend integration tests pass against real infrastructure",
    test_target="tests/integration",
    marker="integration",
    relevant_paths=(
        "backend/app",
        "backend/migrations",
        "backend/tests/integration",
        "backend/tests/conftest.py",
        "scripts/emit_fg08_evidence.py",
        "scripts/gate_evidence.py",
    ),
    json_out=ROOT / "artifacts/evidence/integration/fg08_pytest_integration.json",
    xml_out=ROOT / "artifacts/evidence/integration/fg08_pytest_integration.xml",
)


if __name__ == "__main__":
    raise SystemExit(emit(GATE))
