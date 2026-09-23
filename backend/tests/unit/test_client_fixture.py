"""Shape of the shared ASGI test client fixture (`tests/conftest.py::client`).

The fixture existed from Phase 1 and was **broken** for four phases: it built
``httpx.Client(transport=httpx.ASGITransport(app=app))``, and with httpx 0.28 that
raises ``AttributeError: 'ASGITransport' object has no attribute 'handle_request'``
on the first call, because the transport implements only the async interface. No
test requested the fixture, so the defect was latent; Phase 4 worked around it with
a module-local fixture and reported it. Phase 5 fixes it at source and pins the
behaviour here (HANDOFF section 17.5, obligation 4).

Why a test at all, rather than trusting the new implementation: a fixture that
silently fails on first use is exactly the class of defect this project keeps
finding cheaply *because* something exercises it. A fix with no caller is a claim.
"""

from __future__ import annotations


def test_the_shared_client_drives_the_real_asgi_app(client) -> None:
    """One real request through the real middleware stack, and the frozen envelope."""
    response = client.get("/health/live")

    assert response.status_code == 200, response.text
    body = response.json()
    # Section 95: every response carries the envelope, health included.
    assert set(body) >= {"code", "message", "data", "trace_id"}
    assert body["code"] == 0
    assert body["data"]["status"] in {"ok", "degraded"}


def test_the_shared_client_answers_on_the_versioned_prefix(client) -> None:
    """Liveness is enough to prove the fixture; this proves it mounted the real app.

    A fixture that returned a stub would still pass the test above, so this one
    asserts a path that only the composed application can serve. It is an
    authentication-required endpoint precisely because 401 is the *successful*
    outcome here: it proves the route exists and the auth layer ran, without
    needing a database or a token.
    """
    response = client.get("/api/v1/orders")

    assert response.status_code == 401
    assert response.json()["code"] == 20000
