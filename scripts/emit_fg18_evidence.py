"""Emit the FG-18 evidence artifact: MCP authorization and compatibility, on real HTTP.

FG-18 is a **mandatory** final gate ("MCP Authorization / Compatibility", section 147's
external-capability condition) and its frozen proof path is
``artifacts/evidence/mcp/fg18_mcp.json`` (PROJECT_BASELINE.yaml). The suite this runs
answers three questions, and the artifact records only what actually happened:

1. **Is the token real?** Every authorization case presents a genuinely signed JWT to
   the production verifier (PyJWT, HS256 for tests, RS*/ES* for production). Wrong
   issuer, wrong audience, tampered signature, expired and not-yet-valid are all
   refused, and the positive control is in the same suite - a verifier that refused
   everything would satisfy the negatives and fail the control.
2. **Is the surface an intersection?** ``tools/list`` is filtered by
   token scope ∩ RBAC permission ∩ DataScope ∩ tool policy, and ``tools/call``
   re-authorises rather than trusting the listing.
3. **Is it the official SDK, natively?** ``mcp==2.2.0`` is used through ``MCPServer``
   and ``streamable_http_app``, with the SDK's own protocol negotiation, and the
   compatibility invariants of ADR-013 are asserted against the installed package so a
   dependency bump fails loudly instead of silently changing the baseline.

## Why the emitter delegates to ``gate_evidence.emit``

The shared harness already implements the property this gate needs most: a verdict
requires a non-empty set of *observed* outcomes, a JUnit report that agrees with them
test-for-test, a zero exit code, **and** a clean watched-path set. Writing a second
comparison here would be a second definition of "consistent", and the first thing that
drifts. The only judgement this file contributes is the test target, the marker
expression, and which paths the result actually depends on.

## Why ``-m "integration or not integration"``

Deliberately *every* test in the target: the authorization cases are pure (a token, a
verifier, a policy table) and must run even where MySQL is down, while the tool cases
read and write real rows and carry the ``integration`` marker. An emitter that ran only
one of the two would let the other rot silently.

Run it with the venv python from anywhere:

    ./.venv/Scripts/python.exe scripts/emit_fg18_evidence.py
"""

from __future__ import annotations

from gate_evidence import ROOT, Gate, emit

GATE = Gate(
    gate_id="FG-18",
    name="MCP Authorization / Compatibility",
    mandatory=True,
    spec=(
        "sections 86-93, 121, 138 (ADR-013), 147 - the official MCP SDK is used "
        "natively; every external tool call is authorised by the four-way "
        "intersection of OAuth scope, RBAC permission, DataScope and tool policy; "
        "token verification is real cryptography; no capability outside the frozen "
        "sixteen is reachable; the one write tool files an approval request instead of "
        "creating business state"
    ),
    test_target="tests/mcp",
    marker="integration or not integration",
    # Scoped to what this gate actually exercises, for the reason recorded on the
    # ``Gate.relevant_paths`` field: a wider set would make the verdict a statement
    # about the whole working tree, which during a phase with several writers is a
    # statement about other people's unfinished work rather than about this gate.
    #
    # ``backend/app/modules/*`` appears because the tools call those *services*: a
    # change to a service signature can break FG-18 without touching app/mcp, and the
    # gate's claim is about the surface it exposes.
    relevant_paths=(
        "backend/app/mcp",
        "backend/app/core/errors.py",
        "backend/app/core/redaction.py",
        "backend/app/modules/agent",
        "backend/app/modules/aftersales",
        "backend/app/modules/analytics",
        "backend/app/modules/catalog",
        "backend/app/modules/fulfillment",
        "backend/app/modules/governance",
        "backend/app/modules/identity",
        "backend/app/modules/inventory",
        "backend/app/modules/knowledge",
        "backend/app/modules/marketing",
        "backend/app/modules/order",
        "backend/app/shared/db",
        "backend/tests/mcp",
        "backend/pyproject.toml",
        "scripts/emit_fg18_evidence.py",
        "scripts/gate_evidence.py",
        ".env.example",
    ),
    json_out=ROOT / "artifacts" / "evidence" / "mcp" / "fg18_mcp.json",
    xml_out=ROOT / "artifacts" / "evidence" / "mcp" / "fg18_mcp.xml",
    infrastructure={
        "sdk": "mcp==2.2.0 (official Python SDK, ADR-013)",
        "transports": "in-memory (Client(MCPServer)) and real Streamable HTTP (uvicorn + mcp.Client over http://127.0.0.1:PORT/mcp)",
        "crypto": "PyJWT 2.14.0 - signature, issuer, audience/resource, exp and nbf all verified; HS256 in tests, RS*/ES* supported for production",
        "database": "real MySQL for the tool tests that read or write rows (shared instance; run serially)",
        "why_real": (
            "section 113 forbids a mocked authorization path. An OAuth check that is "
            "mocked cannot fail, so it proves nothing about the deployment - and the "
            "failure mode this gate exists to prevent is precisely an authorized-looking "
            "tool surface that is not actually authorized."
        ),
    },
)


if __name__ == "__main__":
    raise SystemExit(emit(GATE))
