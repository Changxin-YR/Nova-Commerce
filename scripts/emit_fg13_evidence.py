"""Emit FG-13 evidence for refresh-token rotation and HTTP cookie sessions."""

from __future__ import annotations

from gate_evidence import ROOT, Gate, emit

GATE = Gate(
    gate_id="FG-13",
    name="Auth Session / Refresh Rotation",
    mandatory=True,
    spec=(
        "REQ-AUTH-002 through REQ-AUTH-006 / sections 22-23: store only refresh "
        "hashes, rotate on refresh, revoke on logout, reject expired and reused tokens"
    ),
    test_target="tests/integration/identity/test_refresh_rotation.py",
    extra_targets=("tests/integration/order/test_identity_http.py",),
    marker="integration",
    relevant_paths=(
        "backend/app/core",
        "backend/app/api",
        "backend/app/main.py",
        "backend/app/modules/identity",
        "backend/app/shared",
        "backend/migrations",
        "backend/tests/integration/identity",
        "backend/tests/integration/order/test_identity_http.py",
        "backend/tests/integration/order/conftest.py",
        "scripts/emit_fg13_evidence.py",
        "scripts/gate_evidence.py",
    ),
    json_out=ROOT / "artifacts" / "evidence" / "auth" / "fg13_refresh_rotation.json",
    infrastructure={
        "engine": "MySQL 8.4 (real container, not mocked)",
        "why_real": (
            "Session rotation depends on committed rows, hash uniqueness, and revocation "
            "state. HTTP tests also verify the refresh cookie and owner-scoped routes."
        ),
    },
)


if __name__ == "__main__":
    raise SystemExit(emit(GATE))
