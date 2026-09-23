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
    # All four modules the verifier built for FG-12, not just the boundary controls:
    # the live-table caps, the workflow caps that have no database mirror, the
    # fulfillment quantity guard, and the concurrency probe. The frozen proof path
    # names one artifact, so it should carry the whole gate rather than one module of
    # it - a gate that runs a subset of its own tests is a gate with a silent hole.
    test_target="tests/integration/refund",
    extra_targets=("tests/concurrency/test_refund_concurrency.py",),
    # "integration or concurrency", not "integration". The refund gate's concurrency
    # probe is marked `concurrency` (the project's own vocabulary: integration =
    # needs real infrastructure, concurrency = real parallel load against real MySQL),
    # so `-m integration` DESELECTED it and the artifact reported PASS with 12
    # assertions while the race for the last refundable amount was never exercised.
    # Found by the verifier reading the artifact's own assertion list against the t6
    # brief, which named that probe explicitly. FG-09 is marked the same way, so this
    # is also the consistent choice.
    marker="integration or concurrency",
    relevant_paths=(
        "backend/app",
        "backend/migrations",
        "backend/tests/integration/refund",
        "backend/tests/concurrency/test_refund_concurrency.py",
        # The gate's tests build their worlds with the SHARED SEED (paid_order), so a
        # change to it changes what this gate exercised. It was missing from this set,
        # and data-layer's paid_order token fix landed after the previous emission -
        # which is exactly the case the clean-path assertion exists to catch.
        "backend/tests/integration/commerce",
    ),
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
