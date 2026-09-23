"""Emit the unit-test gate from a fresh pytest run."""

from __future__ import annotations

from gate_evidence import ROOT, Gate, emit

GATE = Gate(
    gate_id="FG-07",
    name="Unit Tests",
    mandatory=False,
    spec="Spec §146: backend unit tests pass from a clean source revision",
    test_target="tests/unit",
    # 541 of the 780 tests in tests/unit have no explicit unit marker. The
    # directory defines this gate; -m unit would silently omit most of it.
    marker="not integration",
    relevant_paths=(
        "backend/app",
        "backend/tests/unit",
        "backend/tests/conftest.py",
        "scripts/emit_fg07_evidence.py",
        "scripts/gate_evidence.py",
    ),
    json_out=ROOT / "artifacts/evidence/unit/fg07_pytest_unit.json",
    xml_out=ROOT / "artifacts/evidence/unit/fg07_pytest_unit.xml",
)


if __name__ == "__main__":
    raise SystemExit(emit(GATE))
