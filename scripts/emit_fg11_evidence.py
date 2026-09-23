"""Emit the FG-11 evidence artifact: payment callback idempotency, on real MySQL.

FG-11 is mandatory condition 2 of section 147 ("Payment callback is idempotent") and
its frozen proof path is ``artifacts/evidence/concurrency/fg11_payment_idempotency.json``
(PROJECT_BASELINE.yaml). The test it runs is the concurrency gate: the same provider
event delivered N times at once must produce exactly one payment confirmation, one
inventory deduction, one fulfillment and no duplicate outbox effect
(REQ-PAY-004, workflow inventory ``PaymentSuccessWorkflow``).

Run it with the venv python from anywhere:

    .\.venv\Scripts\python.exe scripts\emit_fg11_evidence.py
"""

from __future__ import annotations

import pathlib

from gate_evidence import ROOT, Gate, emit

GATE = Gate(
    gate_id="FG-11",
    name="Payment Idempotency",
    mandatory=True,
    spec=(
        "REQ-PAY-004 / section 44 - a duplicate callback produces exactly one business "
        "effect: one payment confirm, one inventory effect, one fulfillment, no duplicate "
        "outbox side effect; mandatory condition 2 of section 147"
    ),
    test_target="tests/concurrency/test_payment_idempotency.py",
    marker="concurrency",
    json_out=ROOT / "artifacts" / "evidence" / "concurrency" / "fg11_payment_idempotency.json",
    infrastructure={
        "engine": "MySQL 8.4 (real container, not mocked)",
        "why_real": (
            "The property under test is a database behaviour: UNIQUE (provider, "
            "provider_event_id) is the serialisation point that makes the callback "
            "idempotent, and UNIQUE (idempotency_key) on inventory_movements is what "
            "makes a retried deduction impossible. Threads racing on a real InnoDB "
            "index behave differently from a single-threaded loop over a mock, which "
            "is why section 113 forbids proving a mandatory gate against one. A mock "
            "that agrees with the implementation proves only that the implementation "
            "agrees with itself."
        ),
        "concurrency": (
            "N worker threads open their own sessions and deliver the same provider "
            "event simultaneously; the assertion counts durable rows afterwards, so a "
            "second effect that commits late is still caught."
        ),
    },
)


if __name__ == "__main__":
    raise SystemExit(emit(GATE))
