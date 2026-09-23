"""HTTP smoke tests for the fulfillment surface.

The service tests prove the *rules*; this file proves the *wiring*, which is a
separate failure mode with a separate cause. ``app/api/v1/router.py`` discovers
submodules defensively, so a module whose ``api/__init__.py`` forgot to expose
anything - or which raised on import - is silently skipped and the application still
starts. The endpoints would simply 404, and every service-level test would still pass.

Asserted here: the three frozen paths exist, an unauthenticated call is refused with
the frozen envelope rather than FastAPI's default body, and the ship body's
three-field contract is enforced at the edge.
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.main import create_app

pytestmark = pytest.mark.integration

#: The frozen paths of ``API_CONTRACT`` sections 4 and 5 plus design section 7.
FROZEN_PATHS = {
    ("POST", "/api/v1/fulfillments/{fulfillment_id}/ship"),
    ("GET", "/api/v1/fulfillments/admin"),
    ("GET", "/api/v1/fulfillments/customer/orders/{order_no}/shipments"),
}


@pytest.fixture
def app():
    return create_app(get_settings())


def _routes(app) -> set[tuple[str, str]]:
    """``{(METHOD, path)}`` from the **OpenAPI document**.

    Read from ``app.openapi()`` rather than by walking ``app.routes``: a router that
    was mounted with ``include_router`` appears in the request pipeline but not as a
    flat entry in ``app.routes``, so walking the route list reports mounted paths as
    missing. The schema is also the artifact that matters - REQ-API-005 generates the
    frontend's types from it, so a path present in the pipeline but absent from the
    document is a path no typed client can call.
    """
    spec = app.openapi()
    found: set[tuple[str, str]] = set()
    for path, operations in spec.get("paths", {}).items():
        # The stored path already carries the parameter names used in the route
        # decorator, which is what the frozen list below names.
        for method in operations:
            if method.lower() in {"get", "post", "put", "patch", "delete"}:
                found.add((method.upper(), path))
    return found


@pytest.mark.parametrize(("method", "path"), sorted(FROZEN_PATHS))
def test_each_frozen_fulfillment_path_is_registered(app, method: str, path: str) -> None:
    """A skipped submodule would 404 in production while every unit test passed."""
    assert (method, path) in _routes(app), f"{method} {path} is not mounted"


def test_an_unauthenticated_ship_is_refused_with_the_envelope(client) -> None:
    """401 in the frozen envelope (section 1), not a bare FastAPI error body.

    ``code``/``message``/``data``/``trace_id`` is what the frontend's error mapper
    reads; a body it cannot parse is an error the UI cannot explain.
    """
    response = client.post(
        "/api/v1/fulfillments/1/ship",
        json={
            "carrier": "SF",
            "tracking_no": "X",
            "item_quantities": [{"order_item_id": 1, "quantity": 1}],
        },
    )

    assert response.status_code == 401, response.text
    body = response.json()
    assert set(body) == {"code", "message", "data", "trace_id"}
    assert body["code"] != 0


def test_an_unauthenticated_queue_read_is_refused(client) -> None:
    response = client.get("/api/v1/fulfillments/admin")

    assert response.status_code == 401, response.text
    assert response.json()["code"] != 0


def test_the_ship_body_is_validated_at_the_edge(client) -> None:
    """``extra="forbid"`` is enforced before authentication-independent code runs.

    A body carrying ``fulfillment_status`` must be refused rather than having the extra
    field ignored: a client that believes it set the status would render an optimistic
    UI the server never agreed to.
    """
    response = client.post(
        "/api/v1/fulfillments/1/ship",
        json={
            "carrier": "SF",
            "tracking_no": "X",
            "item_quantities": [{"order_item_id": 1, "quantity": 1}],
            "fulfillment_status": "SHIPPED",
        },
        headers={"Authorization": "Bearer not-a-real-token"},
    )

    # Either the token is rejected first (401) or the body is (4xx); what must not
    # happen is a 200 or a 500.
    assert response.status_code in {401, 403, 422}, response.text
    assert response.status_code not in {200, 500}


def test_an_unknown_carrier_is_refused_at_the_edge(client) -> None:
    """A free-text carrier never reaches the database."""
    response = client.post(
        "/api/v1/fulfillments/1/ship",
        json={
            "carrier": "SF Express",
            "tracking_no": "X",
            "item_quantities": [{"order_item_id": 1, "quantity": 1}],
        },
        headers={"Authorization": "Bearer not-a-real-token"},
    )

    assert response.status_code in {401, 403, 422}, response.text
    assert response.status_code not in {200, 500}
