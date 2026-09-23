"""Emit the FG-12 evidence artifact: refund invariants, on real MySQL.

FG-12 is mandatory condition 3 of section 147 ("Refund never exceeds amount actually
paid") and its frozen proof path is
``artifacts/evidence/integration/fg12_refund_invariants.json`` (PROJECT_BASELINE.yaml).
The test it runs is the verifier's own gate test, written independently of the
refund implementation.

Run it with the venv python from anywhere:

    .\.venv\Scripts\python.exe scripts\emit_fg12_evidence.py
"""

from __future__ import annotations

from gate_evidence import ROOT, Gate, emit

GATE = Gate(
    gate_id="FG-12",
    name="Refund Invariants",
    mandatory=True,
    spec=(
        "REQ-AFS-002 / section 46 - total refunded <= paid amount; item refund <= item "
        "payable amount; mandatory condition 3 of section 147"
    ),
    test_target="tests/integration/refund/test_refund_invariants.py",
    marker="integration",
    json_out=ROOT / "artifacts" / "evidence" / "integration" / "fg12_refund_invariants.json",
    infrastructure={
        "engine": "MySQL 8.4 (real container, not mocked)",
        "why_real": (
            "Both caps are enforced at the database boundary as well as in the service, "
            "so the gate must show the engine refusing the bad write - a CHECK "
            "constraint cannot be proven against a mock, and an application-level check "
            "that agrees with the application proves nothing."
        ),
        "fresh_connection_rule": (
            "Every post-mutation read uses a fresh connection. MySQL REPEATABLE READ "
            "fixes a connection's snapshot at its first read, so a probe that reuses one "
            "connection compares the snapshot against itself and passes as a false "
            "green (HANDOFF section 6). Each negative control also asserts that the "
            "offending mutation actually failed before asserting the row is unchanged - "
            "the two-way check that exposed the trap during Phase 4."
        ),
    },
)


if __name__ == "__main__":
    raise SystemExit(emit(GATE))
