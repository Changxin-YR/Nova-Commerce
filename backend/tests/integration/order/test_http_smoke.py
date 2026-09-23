"""FG-10 - the HTTP surface: the eight frozen paths, the envelope, and one round trip.

Spec §95 (envelope), §96 (paths), §110 (mass assignment), API_CONTRACT §1/§2/§3/§14,
PHASE4_DESIGN §9.

The paths come from ``PROJECT_BASELINE.yaml`` §96 / API_CONTRACT §14 and are **not**
the ``/orders/orders/*`` shape the frontend currently guesses; that migrates in Phase 7.

## The routing trap this file pins

Both sub-routers mount under ``/orders``, and the consumer detail route is
``GET /orders/{order_no}``. If it were registered first, ``GET /orders/admin`` would
be captured by it and the console list would 404. That is invisible to a
"are the routes registered" check - both routes exist either way - so
:func:`test_admin_is_not_shadowed_by_the_detail_route` distinguishes them **by their
answer**: the admin handler refuses a consumer principal with 403
(``INSUFFICIENT_PERMISSION``), while the consumer detail handler would answer 50003
``ORDER_NOT_FOUND`` for ``order_no="admin"``.

## What a real HTTP round trip adds over the service tests

Two things that no service-level test can see: the ``§95`` envelope (``code``/
``message``/``data``/``trace_id``), and the ``§2`` scalar encodings as they actually
appear on the wire - money as integers, enums as SCREAMING_SNAKE, and timestamps as
ISO-8601 UTC with **three** fractional digits.
"""

from __future__ import annotations

import re

import pytest

from app.core.config import get_settings
from app.modules.identity.service import AuthService
from app.shared.db.session import get_session_factory

from .conftest import PASSWORD, Shop

pytestmark = [pytest.mark.integration]

BASE = "/api/v1/orders"

#: The eight frozen paths, with their methods (PHASE4_DESIGN §9).
FROZEN_PATHS: tuple[tuple[str, str], ...] = (
    ("post", f"{BASE}/preview"),
    ("post", BASE),
    ("get", BASE),
    ("get", f"{BASE}/{{order_no}}"),
    ("post", f"{BASE}/{{order_no}}/cancel"),
    ("post", f"{BASE}/{{order_no}}/confirm-receipt"),
    ("get", f"{BASE}/admin"),
    ("get", f"{BASE}/admin/{{order_no}}"),
)


def _token(shop: Shop, *, staff: bool = False) -> str:
    """Mint a real access token through ``AuthService`` (there is no ``/auth/login``
    route yet, and the design explicitly says to mint the token this way)."""
    session = get_session_factory()()
    try:
        issued = AuthService(session, get_settings()).login(
            identifier=shop.staff_username if staff else shop.consumer_username,
            password=PASSWORD,
            client_ip="127.0.0.1",
        )
        # Committed, because the API's own session must be able to see the auth
        # session `get_current_principal` re-checks on every request.
        session.commit()
        return issued.access_token
    finally:
        session.close()


def _auth(token: str, **extra: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    headers.update(extra)
    return headers


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def test_the_eight_frozen_paths_are_registered(client) -> None:
    schema = client.get("/api/openapi.json").json()
    paths = schema["paths"]
    missing = [
        f"{method.upper()} {path}"
        for method, path in FROZEN_PATHS
        if path not in paths or method not in paths[path]
    ]
    assert missing == [], f"the frozen paths are not registered: {missing}"


def test_admin_is_not_shadowed_by_the_detail_route(client, shop: Shop) -> None:
    """The registration-order trap, distinguished by behaviour.

    A consumer principal on ``GET /orders/admin`` must be refused **as a console
    endpoint** (403, ``INSUFFICIENT_PERMISSION``). If the detail route had captured the
    path, the same request would answer 50003 ``ORDER_NOT_FOUND`` for an order numbered
    "admin" - a 404 dressed up as a business error.
    """
    response = client.get(f"{BASE}/admin", headers=_auth(_token(shop)))
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == 20_009, body
    assert body["code"] != 50_003


def test_the_detail_route_is_still_reachable_for_a_consumer(client, shop: Shop) -> None:
    """... and the customer route was not lost to the admin registration."""
    response = client.get(f"{BASE}/NV19990101000001", headers=_auth(_token(shop)))
    assert response.status_code == 404
    assert response.json()["code"] == 50_003


# ---------------------------------------------------------------------------
# Authentication and the envelope
# ---------------------------------------------------------------------------
def test_creating_without_a_token_is_401_in_the_frozen_envelope(client, shop: Shop) -> None:
    response = client.post(
        BASE,
        json={
            "items": [{"sku_id": shop.sku_ids[0], "quantity": 1}],
            "address_id": shop.address_id,
            "client_request_id": shop.client_request_id("noauth"),
        },
        headers={"Idempotency-Key": shop.key("noauth")},
    )
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == 20_000
    assert set(body) == {"code", "message", "data", "trace_id"}
    assert body["trace_id"]
    assert response.headers.get("X-Trace-Id") == body["trace_id"]


def test_creating_without_an_idempotency_key_is_10010(client, shop: Shop) -> None:
    """§14.3. Declared optional in the signature so this code is produced by our own
    check rather than by FastAPI's validator."""
    response = client.post(
        BASE,
        json={
            "items": [{"sku_id": shop.sku_ids[0], "quantity": 1}],
            "address_id": shop.address_id,
            "client_request_id": shop.client_request_id("nokey"),
        },
        headers=_auth(_token(shop)),
    )
    assert response.status_code == 400
    assert response.json()["code"] == 10_010


@pytest.mark.parametrize(
    "field",
    ["unit_price", "payable_amount", "discount_amount", "original_amount", "user_id", "merchant_id"],
)
def test_a_body_naming_its_own_price_or_identity_is_rejected(client, shop: Shop, field: str) -> None:
    """§38/§110 enforced at the wire: **422**, not a silently ignored key."""
    body = {
        "items": [{"sku_id": shop.sku_ids[0], "quantity": 1}],
        "address_id": shop.address_id,
        "client_request_id": shop.client_request_id(f"forbid-{field}"),
        field: 1,
    }
    response = client.post(
        BASE,
        json=body,
        headers=_auth(_token(shop), **{"Idempotency-Key": shop.key(f"forbid-{field}")}),
    )
    assert response.status_code == 422, response.text
    assert field in response.text


# ---------------------------------------------------------------------------
# The round trip
# ---------------------------------------------------------------------------
def test_an_authenticated_create_get_cancel_round_trip(client, shop: Shop) -> None:
    token = _token(shop)
    key = shop.key("http")

    created = client.post(
        BASE,
        json={
            "items": [
                {"sku_id": shop.sku_ids[0], "quantity": 1},
                {"sku_id": shop.sku_ids[2], "quantity": 2},
            ],
            "address_id": shop.address_id,
            "coupon_id": None,
            "remark": "leave at the door",
            "client_request_id": shop.client_request_id("http"),
        },
        headers=_auth(token, **{"Idempotency-Key": key}),
    )
    assert created.status_code == 200, created.text
    envelope = created.json()
    assert envelope["code"] == 0
    assert envelope["message"] == "OK"
    order = envelope["data"]

    # -- §2 scalar encodings, on the real wire --------------------------
    assert isinstance(order["id"], int)
    assert isinstance(order["payable_amount"], int)
    assert isinstance(order["original_amount"], int)
    assert order["order_status"] == "PENDING_PAYMENT"
    assert order["payment_status"] == "UNPAID"
    assert order["fulfillment_status"] == "UNFULFILLED"
    assert order["after_sale_status"] == "NONE"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", order["created_at"]), (
        order["created_at"]
    )
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", order["expires_at"]), (
        order["expires_at"]
    )
    assert order["paid_at"] is None  # §2: explicit null, never absent

    # -- §38: the server's number, not the client's ---------------------
    expected = shop.sku_prices[0] * 1 + shop.sku_prices[2] * 2
    assert order["payable_amount"] == expected
    assert sum(item["payable_amount"] for item in order["items"]) == order["payable_amount"]

    # -- §94: masked, and §6: the server-owned figures ------------------
    assert order["receiver_name"] == "张**"
    assert order["receiver_phone"] == "138****5678"
    assert order["refundable_amount"] == 0
    assert order["shipments"] == []
    assert order["item_count"] == 3
    assert order["remark"] == "leave at the door"
    assert order["cancel_reason"] is None
    assert order["full_address"].endswith("****")
    assert "1801室" not in created.text

    order_no = order["order_no"]

    # -- GET the detail -------------------------------------------------
    fetched = client.get(f"{BASE}/{order_no}", headers=_auth(token))
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["order_no"] == order_no
    assert fetched.json()["data"]["items"][0]["product_name"] == "Nova Phone 15 Pro"

    # -- GET the list: the §3 paged envelope ----------------------------
    listed = client.get(BASE, headers=_auth(token), params={"page": 1, "page_size": 20})
    assert listed.status_code == 200
    page = listed.json()["data"]
    assert set(page) == {"items", "meta"}
    assert set(page["meta"]) == {"page", "page_size", "total", "total_pages"}
    assert page["meta"]["page"] == 1
    assert page["meta"]["total_pages"] == 1
    assert order_no in {row["order_no"] for row in page["items"]}
    # §6: the list row genuinely has no line items.
    assert "items" not in page["items"][0]
    assert page["items"][0]["first_item_name"]

    # -- POST the cancel ------------------------------------------------
    cancelled = client.post(
        f"{BASE}/{order_no}/cancel",
        json={"reason": "用户主动取消"},
        headers=_auth(token),
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["order_status"] == "CANCELLED"
    assert cancelled.json()["data"]["cancel_reason"] == "用户主动取消"

    # -- and the frozen refusal code for a second attempt ---------------
    again = client.post(f"{BASE}/{order_no}/cancel", json={}, headers=_auth(token))
    assert again.status_code == 409
    assert again.json()["code"] == 50_010

    # -- confirm-receipt on a cancelled order is 50011 ------------------
    confirm = client.post(f"{BASE}/{order_no}/confirm-receipt", headers=_auth(token))
    assert confirm.status_code == 409
    assert confirm.json()["code"] == 50_011


def test_a_replayed_create_returns_the_same_order_with_http_200(client, shop: Shop) -> None:
    """200 rather than 201, because a replay returns the *same* order: a client that
    could not tell "created" from "replayed" would have to guess whether its retry was
    safe (§14.2, INV-015)."""
    token = _token(shop)
    key = shop.key("http-replay")
    body = {
        "items": [{"sku_id": shop.sku_ids[1], "quantity": 1}],
        "address_id": shop.address_id,
        "client_request_id": shop.client_request_id("http-replay"),
    }

    first = client.post(BASE, json=body, headers=_auth(token, **{"Idempotency-Key": key}))
    second = client.post(BASE, json=body, headers=_auth(token, **{"Idempotency-Key": key}))
    assert first.status_code == second.status_code == 200
    assert first.json()["data"]["order_no"] == second.json()["data"]["order_no"]
    assert first.json()["data"]["id"] == second.json()["data"]["id"]

    # A different body with the same key is 10011.
    third = client.post(
        BASE,
        json={**body, "remark": "different"},
        headers=_auth(token, **{"Idempotency-Key": key}),
    )
    assert third.status_code == 409
    assert third.json()["code"] == 10_011


def test_the_preview_endpoint_creates_nothing(client, shop: Shop) -> None:
    token = _token(shop)
    preview = client.post(
        f"{BASE}/preview",
        json={
            "items": [{"sku_id": shop.sku_ids[0], "quantity": 2}],
            "address_id": shop.address_id,
        },
        headers=_auth(token),
    )
    assert preview.status_code == 200, preview.text
    data = preview.json()["data"]
    assert data["payable_amount"] == shop.sku_prices[0] * 2
    assert data["shipping_amount"] == 0
    assert data["warnings"] == []
    assert data["items"][0]["sku_id"] == shop.sku_ids[0]
    assert data["items"][0]["image_url"] is None  # §14.2's own example
    # A preview line is not a persisted line: no id, no after-sale status.
    assert "id" not in data["items"][0]
    assert "refunded_amount" not in data["items"][0]

    listed = client.get(BASE, headers=_auth(token))
    assert listed.json()["data"]["meta"]["total"] == 0


def test_the_preview_refuses_a_coupon_it_cannot_resolve(client, shop: Shop) -> None:
    """§14.2: never silently dropped - the customer selected it, so they are told."""
    preview = client.post(
        f"{BASE}/preview",
        json={
            "items": [{"sku_id": shop.sku_ids[0], "quantity": 1}],
            "address_id": shop.address_id,
            "coupon_id": 424242,
        },
        headers=_auth(_token(shop)),
    )
    assert preview.status_code == 404
    assert preview.json()["code"] == 90_004


def test_the_console_surface_serves_a_staff_token(client, shop: Shop) -> None:
    """The two console reads, with an authenticated staff principal."""
    consumer_token = _token(shop)
    created = client.post(
        BASE,
        json={
            "items": [{"sku_id": shop.sku_ids[0], "quantity": 1}],
            "address_id": shop.address_id,
            "client_request_id": shop.client_request_id("console"),
        },
        headers=_auth(consumer_token, **{"Idempotency-Key": shop.key("console")}),
    )
    assert created.status_code == 200, created.text
    order_no = created.json()["data"]["order_no"]

    staff_token = _token(shop, staff=True)
    listed = client.get(
        f"{BASE}/admin",
        headers=_auth(staff_token),
        params={"order_status": "PENDING_PAYMENT", "order_no": order_no},
    )
    assert listed.status_code == 200, listed.text
    page = listed.json()["data"]
    assert page["meta"]["total"] == 1
    assert page["items"][0]["order_no"] == order_no
    # §94 on the console surface too.
    assert page["items"][0]["receiver_name"] == "张**"

    detail = client.get(f"{BASE}/admin/{order_no}", headers=_auth(staff_token))
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["order_no"] == order_no
    assert detail.json()["data"]["refundable_amount"] == 0

    # An unusable filter is a validation error - business code 10001, HTTP 422 by the
    # frozen envelope mapping - not an empty page and not a silent ignore.
    bad = client.get(
        f"{BASE}/admin", headers=_auth(staff_token), params={"order_status": "SHIPPED"}
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == 10_001


def test_pagination_beyond_the_last_page_is_an_empty_page(client, shop: Shop) -> None:
    """§3: never a 404, and the meta is still present."""
    token = _token(shop)
    response = client.get(BASE, headers=_auth(token), params={"page": 5, "page_size": 20})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["items"] == []
    assert data["meta"] == {"page": 5, "page_size": 20, "total": 0, "total_pages": 0}


def test_an_out_of_range_page_size_is_refused(client, shop: Shop) -> None:
    """§3 caps ``page_size`` at 100, on every list endpoint."""
    response = client.get(BASE, headers=_auth(_token(shop)), params={"page_size": 101})
    assert response.status_code == 422
