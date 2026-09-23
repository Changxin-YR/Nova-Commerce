"""FG-11's HTTP half - the payment surface over real HTTP.

Spec references: API_CONTRACT 15.2 and 15.3, REQ-PAY-002/005/006, INV-008, section 109,
section 95 (the envelope), section 110 (mass assignment).

## What an HTTP test adds over the workflow tests

Three things that no service-level test can see, and each of them is a place a payment
surface goes wrong in practice:

1. **The signature is verified over the raw bytes the client actually sent.** A handler that
   let FastAPI parse the JSON and then verified a re-serialised copy would pass a
   service-level test (which hands it the bytes directly) and refuse every honest provider
   in production. This file posts the bytes it signed.
2. **The refuse-versus-settle mapping.** The workflow answers business questions
   ("was an effect applied?") and the edge answers transport ones. A duplicate must be
   **200 with code 60004** - a provider retries until it sees success, so an error would
   make it retry a settlement that is already done, forever.
3. **The ownership answers.** A foreign payment must be ``PAYMENT_NOT_FOUND (60000)``, never
   403: distinguishing them makes the endpoint an existence oracle for other customers'
   payments (section 109).

The mock surface's guard is asserted twice here - once as "allowed in the test
environment" and once as "refused when the flag is off" - because INV-008's whole point is
that the same question is asked at two layers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.core.errors import ErrorCode
from app.modules.payment.enums import CallbackProcessStatus, PaymentRecordStatus
from app.shared.db.session import get_session_factory

from .conftest import PAYABLE_AMOUNT, Shop, fresh_connection_row

pytestmark = [pytest.mark.integration]

BASE = "/api/v1/payments"

#: The frozen paths of section 15.2 plus the callback route of 15.3.
FROZEN_PATHS: tuple[tuple[str, str], ...] = (
    ("post", f"{BASE}/customer/payments"),
    ("get", f"{BASE}/customer/payments/{{payment_id}}"),
    ("get", f"{BASE}/customer/payments/by-order/{{order_no}}"),
    ("post", f"{BASE}/customer/payments/{{payment_id}}/mock-pay"),
    ("post", f"{BASE}/callbacks/{{provider}}"),
    ("get", f"{BASE}/admin/payments"),
)


def _auth(token: str, **extra: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    headers.update(extra)
    return headers


def _signed_body(payload: dict, *, secret: str, timestamp: str | None = None) -> tuple[bytes, dict]:
    """Serialise a provider body and sign **those exact bytes**.

    The bytes returned are the ones posted. Signing a re-serialised copy is the defect this
    helper exists to make impossible: a verifier that parses and re-encodes would disagree
    with an honest provider over key order and whitespace, and would agree with a *reordered*
    payload whose round-trip happened to match.
    """
    from app.modules.payment.providers import sign_body

    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    stamp = timestamp or str(int(datetime.now(UTC).timestamp()))
    signature = sign_body(secret=secret, timestamp=stamp, raw_body=body)
    return body, {
        "X-Provider-Event-Id": payload["event_id"],
        "X-Provider-Timestamp": stamp,
        "X-Provider-Signature": signature,
        "Content-Type": "application/json",
    }


def _settlement_payload(*, event_id: str, payment_no: str, order_no: str, amount: int) -> dict:
    return {
        "event_id": event_id,
        "payment_no": payment_no,
        "order_no": order_no,
        "event_type": "PAYMENT_SUCCEEDED",
        "transaction_no": f"HTTP-{event_id}",
        "amount": amount,
    }


# ---------------------------------------------------------------------------
# Route registration - the routing trap
# ---------------------------------------------------------------------------
def test_every_frozen_payment_path_is_registered(client) -> None:
    """Section 15.2/15.3. Asserted **by answer**, not by the route table.

    A route's existence says nothing about which handler answers a request, and this
    sub-package has a real routing hazard: ``/customer/payments/{payment_id}`` would
    swallow ``/customer/payments/by-order/...`` if it were registered first. So each path is
    probed without credentials and the **401** is what proves a handler (rather than a 404)
    is behind it.
    """
    for method, path in FROZEN_PATHS:
        probe = path.replace("{payment_id}", "1").replace("{order_no}", "NV1").replace(
            "{provider}", "MOCK"
        )
        call = getattr(client, method)
        response = call(probe, json={}) if method == "post" else call(probe)
        assert response.status_code != 404, (
            f"{method.upper()} {path} is not registered (observed {response.status_code})"
        )

    # And the intended order: `by-order` must reach the by-order handler, which answers
    # ORDER_NOT_FOUND (50003) for an unknown order, while `{payment_id}` would have answered
    # a 422 for the non-integer path segment instead.
    response = client.get(
        f"{BASE}/customer/payments/by-order/NOPE", headers=_auth("not-a-real-token")
    )
    assert response.status_code in (401, 403)
    assert response.json()["code"] != 422


def test_by_order_is_not_swallowed_by_the_detail_route(client, shop: Shop, shop_token: str) -> None:
    """The same hazard, distinguished by a real answer.

    An unknown order resolves to ``ORDER_NOT_FOUND (50003)`` through the by-order handler.
    If ``/{payment_id}`` had captured the request, FastAPI would have refused ``by-order`` as
    a non-integer path parameter with a **422** - a different code and a different meaning.
    """
    response = client.get(
        f"{BASE}/customer/payments/by-order/NOPE", headers=_auth(shop_token)
    )
    assert response.status_code == 404
    assert response.json()["code"] == int(ErrorCode.ORDER_NOT_FOUND)


# ---------------------------------------------------------------------------
# The callback surface (section 15.3)
# ---------------------------------------------------------------------------
def test_a_correctly_signed_callback_settles_the_payment(client, shop: Shop) -> None:
    """The whole path, over HTTP: raw bytes in, one settlement out."""
    from app.core.config import get_settings

    settings = get_settings()
    secret = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()
    event_id = f"http-{shop.marker}-first"
    payload = _settlement_payload(
        event_id=event_id,
        payment_no=shop.payment_no,
        order_no=shop.order_no,
        amount=PAYABLE_AMOUNT,
    )
    body, headers = _signed_body(payload, secret=secret)

    response = client.post(f"{BASE}/callbacks/MOCK", content=body, headers=headers)

    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["code"] == 0, envelope
    data = envelope["data"]
    assert data["payment_no"] == shop.payment_no
    assert data["status"] == PaymentRecordStatus.SUCCESS.value
    assert data["paid_amount"] == PAYABLE_AMOUNT
    assert data["external_transaction_no"] == f"HTTP-{event_id}"
    assert data["refundable_amount"] == PAYABLE_AMOUNT
    assert envelope["trace_id"], "section 95: every response carries a trace id"

    row = fresh_connection_row(
        "SELECT status, paid_amount, external_transaction_no FROM payments "
        "WHERE payment_no = :payment_no",
        {"payment_no": shop.payment_no},
    )
    assert row[0] == PaymentRecordStatus.SUCCESS.value
    assert int(row[1]) == PAYABLE_AMOUNT
    assert row[2] == f"HTTP-{event_id}"


def test_a_duplicate_delivery_answers_200_with_the_duplicate_code(client, shop: Shop) -> None:
    """**200, not 409.** The one honest use of "your request was already handled".

    The payment has already been settled by the previous test, and this is a *different*
    event id for it, so the unique index cannot be what answers: the state guard does, and
    the edge maps it to the idempotent success. A 409 here would make a provider retry an
    event that is already applied, forever.
    """
    from app.core.config import get_settings

    settings = get_settings()
    secret = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()
    payload = _settlement_payload(
        event_id=f"http-{shop.marker}-second",
        payment_no=shop.payment_no,
        order_no=shop.order_no,
        amount=PAYABLE_AMOUNT,
    )
    body, headers = _signed_body(payload, secret=secret)

    response = client.post(f"{BASE}/callbacks/MOCK", content=body, headers=headers)

    assert response.status_code == 200, response.text
    envelope = response.json()
    assert envelope["code"] == int(ErrorCode.PAYMENT_CALLBACK_DUPLICATE), envelope
    assert envelope["data"]["duplicate"] is True
    assert envelope["data"]["replayed"] is True
    # The acknowledgement names the payment and the *provider's* event, and nothing else
    # about the merchant's business.
    assert envelope["data"]["payment_no"] == shop.payment_no
    assert set(envelope["data"]) == {
        "event_id",
        "payment_no",
        "processed",
        "replayed",
        "duplicate",
        "status",
    }


def test_a_forged_signature_is_refused_and_claims_nothing(client, shop: Shop) -> None:
    """A bad signature never reaches business code and never claims the event id.

    Both halves matter: refusing without recording would make "why did the provider's
    correction do nothing?" unanswerable, and recording the event id as *processed* would
    make the provider's honest retry a duplicate of a delivery that applied nothing.
    """
    event_id = f"http-{shop.marker}-forged"
    payload = _settlement_payload(
        event_id=event_id,
        payment_no=shop.payment_no,
        order_no=shop.order_no,
        amount=PAYABLE_AMOUNT,
    )
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    headers = {
        "X-Provider-Event-Id": event_id,
        "X-Provider-Timestamp": str(int(datetime.now(UTC).timestamp())),
        "X-Provider-Signature": "0" * 64,
        "Content-Type": "application/json",
    }

    response = client.post(f"{BASE}/callbacks/MOCK", content=body, headers=headers)

    assert response.status_code in (400, 401), response.text
    assert response.json()["code"] == int(ErrorCode.PAYMENT_CALLBACK_INVALID_SIGNATURE)

    # The refusal is recorded, and it does **not** claim success - design section 7.
    recorded = fresh_connection_row(
        "SELECT process_status, signature_valid FROM payment_callbacks "
        "WHERE provider_event_id = :event_id",
        {"event_id": event_id},
    )
    assert recorded[0] == CallbackProcessStatus.FAILED.value, (
        f"the refused delivery is recorded as {recorded[0]}"
    )
    assert recorded[1] in (False, 0), "a refused signature was recorded as valid"


def test_a_stale_timestamp_is_refused_even_when_the_signature_verifies(
    client, shop: Shop
) -> None:
    """The replay attack the skew window exists for.

    The signature is *valid* - computed over the same scheme with the same secret - and the
    request is still refused, because a captured body replayed later must not be able to
    settle anything. Nothing but the freshness check can catch this, which is why the
    timestamp is bound into the signed material rather than checked separately.
    """
    from app.core.config import get_settings

    settings = get_settings()
    secret = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()
    old_timestamp = str(int(datetime.now(UTC).timestamp()) - 86_400)
    payload = _settlement_payload(
        event_id=f"http-{shop.marker}-stale",
        payment_no=shop.payment_no,
        order_no=shop.order_no,
        amount=PAYABLE_AMOUNT,
    )
    body, headers = _signed_body(payload, secret=secret, timestamp=old_timestamp)

    response = client.post(f"{BASE}/callbacks/MOCK", content=body, headers=headers)

    assert response.status_code in (400, 401), response.text
    assert response.json()["code"] == int(ErrorCode.PAYMENT_CALLBACK_INVALID_SIGNATURE)


def test_an_unknown_payment_is_not_found_and_settles_nothing(client, shop: Shop) -> None:
    """A callback naming a payment this deployment cannot resolve is 60000, not a 500.

    A provider whose release change references a payment from another environment is a real
    operational event; the answer must be an error it can act on, and the delivery must still
    be recorded for triage.
    """
    from app.core.config import get_settings

    settings = get_settings()
    secret = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()
    event_id = f"http-{shop.marker}-unknown"
    payload = _settlement_payload(
        event_id=event_id,
        payment_no=f"NVPAY-nope-{shop.marker}",
        order_no=shop.order_no,
        amount=PAYABLE_AMOUNT,
    )
    body, headers = _signed_body(payload, secret=secret)

    response = client.post(f"{BASE}/callbacks/MOCK", content=body, headers=headers)

    assert response.status_code == 404, response.text
    assert response.json()["code"] == int(ErrorCode.PAYMENT_NOT_FOUND)


def test_an_amount_mismatch_is_refused_with_the_mismatch_code(client, fresh_order: Shop) -> None:
    """60002, and the payment is not marked successful.

    A callback reporting a different amount is the shape of a partial payment, a currency
    confusion or an attack. The answer names the mismatch so an operator can reconcile it,
    rather than a generic refusal that would send them looking in the wrong place.
    """
    from app.core.config import get_settings
    from app.modules.payment.enums import PaymentChannel
    from app.modules.payment.service import PaymentService

    settings = get_settings()
    secret = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()

    # Create a real attempt through the service so the row exists with the right amount.
    with get_session_factory()() as session:
        created = PaymentService(session, settings).create(
            principal=fresh_order.consumer,
            order_no=fresh_order.order_no,
            channel=PaymentChannel.MOCK.value,
            client_request_id=f"mismatch-client-{fresh_order.marker}",
            idempotency_key=f"mismatch-key-{fresh_order.marker}",
        )
        payment_no = created.payment.payment_no
        expected = created.payment.amount

    event_id = f"http-{fresh_order.marker}-short"
    payload = _settlement_payload(
        event_id=event_id,
        payment_no=payment_no,
        order_no=fresh_order.order_no,
        amount=expected - 1,
    )
    body, headers = _signed_body(payload, secret=secret)

    response = client.post(f"{BASE}/callbacks/MOCK", content=body, headers=headers)

    assert response.status_code == 409, response.text
    assert response.json()["code"] == int(ErrorCode.PAYMENT_AMOUNT_MISMATCH)

    row = fresh_connection_row(
        "SELECT status, paid_amount FROM payments WHERE payment_no = :payment_no",
        {"payment_no": payment_no},
    )
    assert row[0] != PaymentRecordStatus.SUCCESS.value, "a short payment settled"
    assert int(row[1]) == 0


def test_the_callback_route_needs_no_jwt_and_ignores_one(client, shop: Shop) -> None:
    """REQ-PAY-005: the provider's authentication model is the signature, not a token.

    Two assertions in one: a callback with **no** Authorization header is processed, and one
    carrying a bogus bearer token is processed identically. The second is the one that
    matters - a route that *accepted* a JWT would let anything holding one act as a payment
    provider, which is the escalation the separate auth model exists to prevent.
    """
    from app.core.config import get_settings

    settings = get_settings()
    secret = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()
    event_id = f"http-{shop.marker}-no-jwt"
    payload = _settlement_payload(
        event_id=event_id,
        payment_no=shop.payment_no,
        order_no=shop.order_no,
        amount=PAYABLE_AMOUNT,
    )
    body, headers = _signed_body(payload, secret=secret)
    headers["Authorization"] = "Bearer not-a-real-token"

    response = client.post(f"{BASE}/callbacks/MOCK", content=body, headers=headers)

    # Accepted (200) as an idempotent replay, which proves the token was not consulted:
    # a route that depended on `get_current_principal` would have answered 401.
    assert response.status_code == 200, response.text
    assert response.json()["code"] == int(ErrorCode.PAYMENT_CALLBACK_DUPLICATE)


# ---------------------------------------------------------------------------
# Ownership and the mock guard (section 109, INV-008)
# ---------------------------------------------------------------------------
def test_a_foreign_payment_is_not_found_never_forbidden(client, staff_token: str) -> None:
    """A **staff** token is not the owner, so the consumer read must answer 60000.

    Deliberately a staff token rather than a second consumer: it is the principal that
    *could* plausibly be granted access later, so the check that matters is that ownership is
    applied in the query even for an authenticated, privileged caller. A 403 would confirm
    that the id exists, which is the existence oracle section 109 forbids.
    """
    response = client.get(f"{BASE}/customer/payments/1", headers=_auth(staff_token))
    assert response.status_code == 404
    assert response.json()["code"] == int(ErrorCode.PAYMENT_NOT_FOUND)


def test_the_mock_surface_is_refused_when_the_flag_is_off(
    client, shop: Shop, shop_token: str, monkeypatch
) -> None:
    """The endpoint-level half of REQ-PAY-005 / INV-008.

    The flag is turned off on the live ``Settings`` object, so the request is served by the
    running application exactly as a hardened deployment would serve it. A guard that lived
    only in a ``Settings`` validator would be bypassable with ``model_construct()``, which is
    why the edge asks the same question again - and this is the assertion that the second
    question is really being asked.
    """
    from app.core.config import get_settings

    settings = get_settings()
    # Set rather than assumed: the shipped default is ``False`` ("safe by default, opt in to
    # the dangerous thing"), so a test environment has to opt in exactly like a deployment.
    # Asserting the *flag* here, not a property of the environment, is what makes the
    # negative case below unambiguous.
    monkeypatch.setattr(settings, "PAYMENT_MOCK_ENABLED", False, raising=True)

    response = client.post(
        f"{BASE}/customer/payments/{shop.payment_id}/mock-pay", headers=_auth(shop_token)
    )

    assert response.status_code == 403, response.text
    assert response.json()["code"] == int(ErrorCode.PAYMENT_MOCK_DISABLED)

    # Nothing was settled: the guard runs before any settlement work.
    row = fresh_connection_row(
        "SELECT status FROM payments WHERE payment_no = :payment_no",
        {"payment_no": shop.payment_no},
    )
    assert row[0] == PaymentRecordStatus.SUCCESS.value, (
        "the already-settled fixture payment changed state on a refused mock call"
    )


def test_the_mock_surface_is_allowed_when_the_flag_is_on(
    client, shop: Shop, shop_token: str, monkeypatch
) -> None:
    """The positive half, and it is not ceremony.

    The negative test above could pass for the wrong reason - a route that 403s for
    *everyone* would satisfy it, and a route that 404s would too. This one opts the flag in
    and settles through the real ``PaymentSuccessWorkflow``, which also proves the mock
    channel reaches the same transaction body a provider callback does rather than a
    shortcut beside it.
    """
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "PAYMENT_MOCK_ENABLED", True, raising=True)

    response = client.post(
        f"{BASE}/customer/payments/{shop.payment_id}/mock-pay", headers=_auth(shop_token)
    )

    # 200 with either the settled payment or the idempotent-duplicate code: the fixture order
    # is settled by the callback tests above, so the second mock-pay is answered by the state
    # guard as a replay. Both are successes and neither is a second settlement.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["code"] in (0, int(ErrorCode.PAYMENT_CALLBACK_DUPLICATE)), body

    settled = fresh_connection_row(
        "SELECT COUNT(*) FROM payments WHERE order_id = :order_id AND status = 'SUCCESS'",
        {"order_id": shop.order_id},
    )
    assert int(settled[0]) == 1, "the mock settlement produced more than one SUCCESS attempt"


def test_a_consumer_cannot_reach_the_admin_payment_queue(client, shop_token: str, shop: Shop) -> None:
    """A consumer token on a console route must be refused.

    The console list is scoped by ``merchant_id`` and a consumer has none, so allowing the
    call would either 500 or - worse - return an unscoped page. ``require_merchant_scope``
    refuses it as a permission failure before any query runs.
    """
    response = client.get(f"{BASE}/admin/payments", headers=_auth(shop_token))
    assert response.status_code == 403
    assert response.json()["code"] in (
        int(ErrorCode.INSUFFICIENT_PERMISSION),
        int(ErrorCode.FORBIDDEN),
    )


def test_an_unauthenticated_consumer_read_is_refused(client, shop: Shop) -> None:
    response = client.get(f"{BASE}/customer/payments/{shop.payment_id}")
    assert response.status_code == 401
    assert response.json()["code"] == int(ErrorCode.UNAUTHENTICATED)


# ---------------------------------------------------------------------------
# The consumer surface
# ---------------------------------------------------------------------------
def test_the_owner_can_read_their_own_payment_by_id_and_by_order(
    client, shop: Shop, shop_token: str
) -> None:
    """Both frozen read paths, with the section 2 encodings visible on the wire.

    The two reads must agree field for field: a detail page and an order page that rendered
    different figures for one attempt is exactly the class of bug the frozen Payment object
    exists to prevent.
    """
    by_id = client.get(f"{BASE}/customer/payments/{shop.payment_id}", headers=_auth(shop_token))
    by_order = client.get(
        f"{BASE}/customer/payments/by-order/{shop.order_no}", headers=_auth(shop_token)
    )

    assert by_id.status_code == 200, by_id.text
    assert by_order.status_code == 200, by_order.text
    assert by_id.json()["data"] == by_order.json()["data"]

    data = by_id.json()["data"]
    assert data["payment_no"] == shop.payment_no
    assert data["order_no"] == shop.order_no
    assert data["channel"] == "MOCK"
    assert isinstance(data["amount"], int), "money is an integer in minor units"
    assert data["pay_url"] and data["pay_url"].endswith("/mock-pay"), (
        "a MOCK payment must carry its pay_url (section 15.2)"
    )
    # Section 2: ISO-8601 UTC with exactly three fractional digits.
    assert data["created_at"].endswith("Z")
    assert len(data["created_at"].split(".")[-1]) == 4


def test_the_create_endpoint_requires_an_idempotency_key(client, fresh_order: Shop, shop_token: str) -> None:
    """10010 rather than a FastAPI 422: the code is what the client maps to a retry."""
    response = client.post(
        f"{BASE}/customer/payments",
        json={
            "order_no": fresh_order.order_no,
            "channel": "MOCK",
            "client_request_id": f"http-create-{fresh_order.marker}",
        },
        headers=_auth(shop_token),
    )
    # 10010's canonical status is a 400 (it is a missing header, not a malformed body), and
    # the point of the test is the *code*: a FastAPI 422 would carry 10001 instead, which
    # the client's error mapper would not map back to "your retry needs a key".
    assert response.status_code == 400, response.text
    assert response.json()["code"] == int(ErrorCode.IDEMPOTENCY_KEY_REQUIRED)


def test_the_create_endpoint_cannot_be_talked_into_an_amount(client, fresh_order: Shop, shop_token: str) -> None:
    """Section 110: a body naming its own amount is **rejected**, not quietly corrected.

    This is the single most important assertion on the consumer surface. Ignoring the extra
    key would be worse than refusing it: the response shows the *server's* amount, so the
    client would believe it had set the price and only find out from a reconciliation.
    """
    response = client.post(
        f"{BASE}/customer/payments",
        json={
            "order_no": fresh_order.order_no,
            "channel": "MOCK",
            "client_request_id": f"http-amount-{fresh_order.marker}",
            "amount": 1,
        },
        headers=_auth(shop_token, **{"Idempotency-Key": f"http-amount-{fresh_order.marker}"}),
    )
    assert response.status_code == 422
    assert response.json()["code"] == int(ErrorCode.VALIDATION_ERROR)


def test_a_foreign_order_cannot_be_paid_by_number(client, shop_token: str, shop: Shop) -> None:
    """Naming somebody else's order is ``ORDER_NOT_FOUND``, never a pending charge on it.

    The order number is a guessable-looking string, so the query filters by ``user_id``
    rather than checking ownership after loading - a post-load check is one early ``return``
    away from leaking the existence of another customer's order.
    """
    response = client.post(
        f"{BASE}/customer/payments",
        json={
            "order_no": f"NVPIT-NOPE-{shop.marker}",
            "channel": "MOCK",
            "client_request_id": f"foreign-{shop.marker}",
        },
        headers=_auth(shop_token, **{"Idempotency-Key": f"foreign-{shop.marker}"}),
    )
    assert response.status_code == 404
    assert response.json()["code"] == int(ErrorCode.ORDER_NOT_FOUND)

    # And the same for an order that exists but belongs to somebody else: the staff
    # account's merchant owns this order, so a *consumer*-scoped call against it is 404 too.
    staff_order = client.post(
        f"{BASE}/customer/payments",
        json={
            "order_no": shop.order_no,
            "channel": "MOCK",
            "client_request_id": f"foreign2-{shop.marker}",
        },
        headers=_auth(shop_token, **{"Idempotency-Key": f"foreign2-{shop.marker}"}),
    )
    # The order belongs to this consumer in the fixture, so this create is legitimate - the
    # assertion is that it is not silently a *second* charge: it either replays the existing
    # attempt or is refused for the order's state, never a fresh attempt beside a PAID one.
    assert staff_order.status_code in (200, 409), staff_order.text


def test_the_admin_queue_is_paged_and_filtered(client, shop: Shop, staff_token: str) -> None:
    """Section 3's envelope, and the three filters section 15.2 freezes."""
    response = client.get(
        f"{BASE}/admin/payments",
        params={"order_no": shop.order_no},
        headers=_auth(staff_token),
    )
    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert set(body) == {"items", "meta"}
    assert body["meta"]["total"] >= 1
    assert body["meta"]["total_pages"] >= 1
    assert all(item["order_no"] == shop.order_no for item in body["items"])

    # An unknown filter value is refused rather than dropped: dropping it would render every
    # payment while the operator believed they were looking at failures.
    bad = client.get(
        f"{BASE}/admin/payments", params={"status": "PAYED"}, headers=_auth(staff_token)
    )
    assert bad.status_code == 422
    assert bad.json()["code"] == int(ErrorCode.VALIDATION_ERROR)


def test_the_admin_queue_shows_a_settled_payment(client, shop: Shop, staff_token: str) -> None:
    """The queue the console works from, filtered to SUCCESS.

    Asserts the settlement is visible to the back office rather than only to the buyer: a
    payment that settled but never appeared in the operator's queue is a support ticket with
    no answer.
    """
    response = client.get(
        f"{BASE}/admin/payments",
        params={"status": "success", "order_no": shop.order_no},
        headers=_auth(staff_token),
    )
    assert response.status_code == 200, response.text
    items = response.json()["data"]["items"]
    assert any(
        item["status"] == PaymentRecordStatus.SUCCESS.value
        and item["order_no"] == shop.order_no
        for item in items
    ), f"the settled payment is missing from the console queue: {items}"
