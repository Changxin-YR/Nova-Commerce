"""FG-10 (3/4) - idempotency: two guards, one order, no second stock movement.

Spec §48/§96, §112 INV-015, API_CONTRACT §14.3, PHASE4_DESIGN §7.

The five frozen situations of §14.3, each asserted against the **database** rather
than against the response:

| Situation | Expected |
|---|---|
| Missing ``Idempotency-Key`` | ``IDEMPOTENCY_KEY_REQUIRED (10010)`` |
| Same key, same body | the original order; one order row, one movement |
| Same key, different body | ``IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD (10011)`` |
| Different key, same ``client_request_id``, same body | the original order |
| Different key, same ``client_request_id``, different body | ``10011`` |

"One order row, one movement" is the part that matters. A replay that returned the
right JSON while quietly creating a second order and locking a second unit of stock
would pass any assertion made on the response alone - which is why every case here
counts rows and inventory movements.

## And one case the contract does not name

Two **different customers** using the same key. ``idempotency_records`` is unique on
``(scope, idempotency_key)`` and is *not* per customer, so without care the second
customer would be handed the first one's order - a cross-customer read produced by the
"safe retry" path, which is the last place anyone looks for an authorisation bug. The
canonical request hash therefore binds the buyer, and
:func:`test_a_different_customer_with_the_same_key_gets_a_conflict` pins it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import (
    IdempotencyInProgressError,
    IdempotencyKeyRequiredError,
    IdempotencyPayloadMismatchError,
)
from app.modules.identity.service import Principal
from app.modules.inventory.enums import MovementType
from app.modules.order.service import OrderService
from app.modules.order.workflow import OrderLineInput
from app.shared.db.models.idempotency import IdempotencyRecord, IdempotencyStatus
from app.shared.db.session import get_session_factory

from .conftest import (
    OPENING_STOCK,
    Shop,
    movements_for,
    order_count_for,
    read_position,
)

pytestmark = [pytest.mark.integration]

LINES = None  # set per test from the shop fixture


def _run(shop: Shop, *, key: str | None, client_request_id: str, lines=None, **kwargs):
    """One create attempt, in its own session, exactly as a request would arrive."""
    session = get_session_factory()()
    try:
        return OrderService(session).create_order(
            principal=kwargs.pop("principal", shop.consumer),
            items=lines or [OrderLineInput(shop.sku_ids[0], 2)],
            address_id=kwargs.pop("address_id", shop.address_id),
            client_request_id=client_request_id,
            idempotency_key=key or "",
            **kwargs,
        )
    finally:
        session.close()


def _claims(shop: Shop) -> list[IdempotencyRecord]:
    session = get_session_factory()()
    try:
        return list(
            session.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.idempotency_key.like(f"{shop.marker}%")
                )
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def _lock_count(shop: Shop, sku_id: int) -> int:
    session = get_session_factory()()
    try:
        return len(
            [
                row
                for row in movements_for(session, sku_id=sku_id)
                if row.movement_type == MovementType.ORDER_LOCK.value
            ]
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Missing key
# ---------------------------------------------------------------------------
def test_a_missing_idempotency_key_is_refused(shop: Shop) -> None:
    """10010. The HTTP layer refuses it at the edge too; the workflow refuses it as
    well because it is also reachable from tests and (later) the tool gateway, and a
    create with no key is not idempotent by construction."""
    with pytest.raises(IdempotencyKeyRequiredError) as caught:
        _run(shop, key=None, client_request_id=shop.client_request_id("nokey"))
    assert int(caught.value.code) == 10_010
    assert order_count_for_user(shop) == 0


# ---------------------------------------------------------------------------
# Same key, same body
# ---------------------------------------------------------------------------
def test_same_key_and_body_replays_the_original_order(shop: Shop) -> None:
    key = shop.key("replay")
    first = _run(shop, key=key, client_request_id=shop.client_request_id("replay"))
    assert first.replayed is False

    second = _run(shop, key=key, client_request_id=shop.client_request_id("replay"))
    assert second.replayed is True
    assert second.order.order_no == first.order.order_no
    assert second.order.id == first.order.id

    # The response is not the evidence; the row counts are.
    assert order_count_for_user(shop) == 1
    assert _lock_count(shop, shop.sku_ids[0]) == 1

    session = get_session_factory()()
    try:
        available, locked, _version = read_position(
            session, sku_id=shop.sku_ids[0], warehouse_id=shop.warehouse_id
        )
        assert available == OPENING_STOCK - 2
        assert locked == 2
    finally:
        session.close()


def test_a_replay_with_a_reordered_basket_is_still_the_same_request(shop: Shop) -> None:
    """A client that retries with its lines in a different order is making the same
    request; answering 10011 to that would be a false conflict."""
    key = shop.key("reorder")
    lines = [OrderLineInput(shop.sku_ids[0], 1), OrderLineInput(shop.sku_ids[1], 1)]
    first = _run(shop, key=key, client_request_id=shop.client_request_id("reorder"), lines=lines)

    second = _run(
        shop,
        key=key,
        client_request_id=shop.client_request_id("reorder"),
        lines=list(reversed(lines)),
    )
    assert second.replayed is True
    assert second.order.order_no == first.order.order_no
    assert order_count_for_user(shop) == 1


# ---------------------------------------------------------------------------
# Same key, different body
# ---------------------------------------------------------------------------
def test_same_key_with_a_different_body_is_a_conflict(shop: Shop) -> None:
    key = shop.key("mismatch")
    _run(shop, key=key, client_request_id=shop.client_request_id("mismatch"))

    with pytest.raises(IdempotencyPayloadMismatchError) as caught:
        _run(
            shop,
            key=key,
            client_request_id=shop.client_request_id("mismatch"),
            lines=[OrderLineInput(shop.sku_ids[0], 5)],
        )
    assert int(caught.value.code) == 10_011
    assert caught.value.status_code == 409

    # A conflict must not create anything, and must not leak the first order either.
    assert order_count_for_user(shop) == 1
    assert _lock_count(shop, shop.sku_ids[0]) == 1
    assert "order_no" not in caught.value.context


def test_same_key_with_a_different_address_is_a_conflict(shop: Shop) -> None:
    key = shop.key("addr")
    _run(shop, key=key, client_request_id=shop.client_request_id("addr"))
    with pytest.raises(IdempotencyPayloadMismatchError):
        _run(
            shop,
            key=key,
            client_request_id=shop.client_request_id("addr"),
            address_id=shop.address_id,
            lines=[OrderLineInput(shop.sku_ids[0], 2)],
            remark="a different request",
        )


# ---------------------------------------------------------------------------
# The second guard: client_request_id
# ---------------------------------------------------------------------------
def test_a_lost_header_still_finds_the_original_order(shop: Shop) -> None:
    """Different key, same ``client_request_id``, same body: the body field is the
    guard for a client that lost its header."""
    body_id = shop.client_request_id("lost")
    first = _run(shop, key=shop.key("first"), client_request_id=body_id)

    second = _run(shop, key=shop.key("second"), client_request_id=body_id)
    assert second.replayed is True
    assert second.order.order_no == first.order.order_no
    assert order_count_for_user(shop) == 1
    assert _lock_count(shop, shop.sku_ids[0]) == 1


def test_a_reused_client_request_id_with_a_different_body_is_a_conflict(shop: Shop) -> None:
    """The two guards must not disagree about this case (§14.3)."""
    body_id = shop.client_request_id("reused")
    _run(shop, key=shop.key("a"), client_request_id=body_id)

    with pytest.raises(IdempotencyPayloadMismatchError) as caught:
        _run(
            shop,
            key=shop.key("b"),
            client_request_id=body_id,
            lines=[OrderLineInput(shop.sku_ids[1], 7)],
        )
    assert int(caught.value.code) == 10_011
    assert order_count_for_user(shop) == 1
    assert _lock_count(shop, shop.sku_ids[1]) == 0


# ---------------------------------------------------------------------------
# The claim record itself
# ---------------------------------------------------------------------------
def test_the_claim_is_completed_with_a_non_sensitive_snapshot(shop: Shop) -> None:
    """§48: ``response_snapshot`` sits outside the order's redaction path, so it must
    carry a summary and nothing else. The three permitted keys are asserted as an
    exact set - an added ``receiver_name`` would be a §94 leak that nothing else
    would catch.
    """
    key = shop.key("snapshot")
    result = _run(shop, key=key, client_request_id=shop.client_request_id("snapshot"))

    claims = [claim for claim in _claims(shop) if claim.idempotency_key == key]
    assert len(claims) == 1
    claim = claims[0]
    assert claim.scope == "order:create"
    assert claim.status == IdempotencyStatus.COMPLETED.value
    assert claim.is_completed
    assert claim.resource_type == "ORDER"
    assert claim.resource_id == result.order.id
    assert claim.response_code == 0
    assert claim.expires_at is not None
    assert len(claim.request_hash) == 64

    snapshot = claim.response_snapshot or {}
    assert set(snapshot) == {"order_no", "payable_amount", "created_at"}
    assert snapshot["order_no"] == result.order.order_no
    assert snapshot["payable_amount"] == result.order.payable_amount
    assert snapshot["created_at"].endswith("Z")
    # Nothing about the receiver, by any spelling.
    flattened = repr(snapshot)
    assert "张三丰" not in flattened
    assert "13800005678" not in flattened
    assert "1801室" not in flattened


def test_the_claim_and_the_order_commit_together(shop: Shop) -> None:
    """§48/§49: neither can be visible without the other. A claim with no order would
    poison the key forever; an order with no claim would allow a second one."""
    result = _run(shop, key=shop.key("together"), client_request_id=shop.client_request_id("t"))

    session = get_session_factory()()
    try:
        claim = (
            session.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.idempotency_key == shop.key("together")
                )
            )
            .scalars()
            .one()
        )
        assert claim.resource_id == result.order.id
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Cross-customer key collision
# ---------------------------------------------------------------------------
def test_a_different_customer_with_the_same_key_gets_a_conflict(shop: Shop) -> None:
    """The key space is global, so the buyer is part of the request hash.

    Without this, two customers sharing a key and a basket would share a hash, and the
    second would be handed the first one's order - along with that buyer's masked name
    and address.
    """
    key = shop.key("shared")
    first = _run(shop, key=key, client_request_id=shop.client_request_id("shared"))

    other_buyer = Principal(
        user_id=shop.consumer_id + 10_000,
        user_type="CONSUMER",
        merchant_id=None,
        roles=(),
        permissions=frozenset(),
        data_scope=shop.consumer.data_scope,
        session_id="other-buyer",
        is_staff=False,
    )

    with pytest.raises(IdempotencyPayloadMismatchError) as caught:
        _run(
            shop,
            key=key,
            client_request_id=shop.client_request_id("shared"),
            principal=other_buyer,
        )
    assert int(caught.value.code) == 10_011
    # Nothing about the first buyer's order is disclosed.
    assert "order_no" not in caught.value.context
    assert first.order.order_no not in repr(caught.value.context)


def test_every_claim_this_suite_made_is_attributable(shop: Shop) -> None:
    """A housekeeping assertion with teeth: ``idempotency_records`` has no owner
    column, so a key that is not marker-prefixed can never be cleaned up and the table
    grows a little on every run."""
    _run(shop, key=shop.key("attributable"), client_request_id=shop.client_request_id("a"))
    for claim in _claims(shop):
        assert claim.idempotency_key.startswith(shop.marker)


# ---------------------------------------------------------------------------
# In-progress
# ---------------------------------------------------------------------------
def test_an_in_progress_claim_that_is_visible_is_refused_rather_than_replayed(
    shop: Shop,
) -> None:
    """Unreachable on the normal path (claim and completion share one transaction), so
    it is driven directly: a claim some future writer leaves in flight must never be
    answered with a second order.

    ``IDEMPOTENCY_REQUEST_IN_PROGRESS (10012)`` is the frozen code for exactly this,
    and it tells the client to retry - which is true, and far better than either
    alternative (a duplicate order, or blocking a request thread on somebody else's
    transaction).
    """
    session = get_session_factory()()
    try:
        from app.modules.order.repository import IdempotencyRepository

        repository = IdempotencyRepository(session)
        key = shop.key("inflight")
        repository.insert_in_progress(
            scope="order:create",
            idempotency_key=key,
            request_hash="f" * 64,
            resource_type="ORDER",
        )
        session.commit()
    finally:
        session.close()

    with pytest.raises(IdempotencyInProgressError) as caught:
        _run(shop, key=shop.key("inflight"), client_request_id=shop.client_request_id("inflight"))
    assert int(caught.value.code) == 10_012
    assert caught.value.status_code == 409
    assert order_count_for_user(shop) == 0


def order_count_for_user(shop: Shop) -> int:
    session = get_session_factory()()
    try:
        return order_count_for(session, user_id=shop.consumer_id)
    finally:
        session.close()
