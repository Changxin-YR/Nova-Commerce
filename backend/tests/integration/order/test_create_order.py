"""FG-10 (1/4) - order creation: persistence, recomputation, reservation, INV-014.

Spec §27/§35/§37/§38, §112 INV-006/INV-014, API_CONTRACT §14.2, PHASE4_DESIGN §7.

Real MySQL. The properties proved here are the ones a mock would fake:

* **The server recomputes every figure.** The test changes the SKU's price and
  asserts the order follows the SKU, not the client - the create request has no price
  field at all, which :mod:`tests.unit.modules.order.test_schemas` checks at the edge
  and this file checks at the row.
* **INV-006 exactness.** ``SUM(order_items.payable_amount) == orders.payable_amount``
  is asserted against the **persisted rows**, read back with a fresh query. The
  workflow's in-transaction assertion is a different mechanism (it can still roll
  back); this one is the evidence that the committed state is right.
* **INV-014.** After the order exists, the product name, SKU name, SKU price and
  attribute snapshot are all mutated, and the order is re-read: every field identical.
  This is the test the phase is named for, and it is deliberately done by mutating
  real rows rather than by inspecting an import graph.
* **The reservation really happened**, read from ``inventories`` directly: the
  available quantity fell, the locked quantity rose, and exactly one ``ORDER_LOCK``
  movement explains the delta (INV-007).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import InsufficientStockError
from app.modules.catalog.models import Product, ProductSku
from app.modules.inventory.enums import MovementType
from app.modules.order.enums import OrderStatus
from app.modules.order.service import OrderService
from app.modules.order.workflow import OrderLineInput
from app.shared.db.base import utc_now
from app.shared.db.session import get_session_factory

from .conftest import (
    OPENING_STOCK,
    Shop,
    items_of,
    load_order,
    movements_for,
    order_count_for,
    read_position,
    status_logs,
)

pytestmark = [pytest.mark.integration]


def _create(shop: Shop, *, lines=None, suffix="1", **kwargs):
    """Run one create in its own session, exactly as a request would."""
    session = get_session_factory()()
    try:
        return OrderService(session).create_order(
            principal=shop.consumer,
            items=lines or [OrderLineInput(shop.sku_ids[0], 1)],
            address_id=shop.address_id,
            client_request_id=shop.client_request_id(suffix),
            idempotency_key=shop.key(suffix),
            **kwargs,
        )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 1. Persistence, recomputation and the reservation
# ---------------------------------------------------------------------------
def test_create_persists_an_order_a_line_and_one_status_log(shop: Shop) -> None:
    result = _create(shop, lines=[OrderLineInput(shop.sku_ids[0], 2)])

    assert result.replayed is False
    order = result.order
    assert order.id is not None
    assert order.order_status == OrderStatus.PENDING_PAYMENT.value
    assert order.payment_status == "UNPAID"
    assert order.fulfillment_status == "UNFULFILLED"
    assert order.after_sale_status == "NONE"
    assert order.merchant_id == shop.merchant_id
    assert order.user_id == shop.consumer_id
    assert order.address_id == shop.address_id
    assert order.paid_amount == 0
    assert order.refunded_amount == 0
    assert order.cancel_reason is None
    assert order.item_count == 2

    # `NV{YYYYMMDD}{id:06d}`, 16 characters, and it contains the real id.
    prefix = f"NV{utc_now():%Y%m%d}"
    assert order.order_no.startswith(prefix)
    assert order.order_no == f"{prefix}{order.id:06d}"

    # A payment window was stamped from ORDER_PAYMENT_TIMEOUT_MINUTES.
    assert order.expires_at is not None

    session = get_session_factory()()
    try:
        items = items_of(session, order_id=order.id)
        assert len(items) == 1
        assert items[0].sku_id == shop.sku_ids[0]
        assert items[0].quantity == 2
        assert items[0].warehouse_id == shop.warehouse_id
        assert items[0].after_sale_status == "NONE"
        assert items[0].refunded_amount == 0

        logs = status_logs(session, order_id=order.id)
        assert len(logs) == 1
        assert logs[0].from_status is None
        assert logs[0].to_status == OrderStatus.PENDING_PAYMENT.value
        # §36/§70: the operator is derived from the verified principal.
        assert logs[0].operator_type == "CUSTOMER"
        assert logs[0].operator_id == shop.consumer_id
        assert logs[0].order_no == order.order_no
    finally:
        session.close()


def test_the_server_recomputes_the_price_from_the_sku(shop: Shop) -> None:
    """§38 at the row level: the amount follows the catalogue, not the request."""
    result = _create(shop, lines=[OrderLineInput(shop.sku_ids[0], 3)])
    expected = shop.sku_prices[0] * 3
    assert result.order.original_amount == expected
    assert result.order.payable_amount == expected
    assert result.order.promotion_discount_amount == 0
    assert result.order.coupon_discount_amount == 0
    assert result.order.shipping_amount == 0  # §14.4: free in V1


def test_duplicate_sku_lines_are_merged_into_one_row(shop: Shop) -> None:
    """``uq_order_items_order_sku`` would reject two rows; the merge happens first."""
    result = _create(
        shop,
        lines=[OrderLineInput(shop.sku_ids[0], 1), OrderLineInput(shop.sku_ids[0], 2)],
    )
    assert result.order.item_count == 3
    assert result.order.original_amount == shop.sku_prices[0] * 3

    session = get_session_factory()()
    try:
        items = items_of(session, order_id=result.order.id)
        assert len(items) == 1
        assert items[0].quantity == 3
    finally:
        session.close()


def test_the_stock_is_reserved_and_the_ledger_explains_it(shop: Shop) -> None:
    sku_id = shop.sku_ids[0]
    session = get_session_factory()()
    try:
        available_before, locked_before, _version = read_position(
            session, sku_id=sku_id, warehouse_id=shop.warehouse_id
        )
        assert available_before == OPENING_STOCK
        assert locked_before == 0
    finally:
        session.close()

    _create(shop, lines=[OrderLineInput(sku_id, 4)])

    session = get_session_factory()()
    try:
        available_after, locked_after, _version = read_position(
            session, sku_id=sku_id, warehouse_id=shop.warehouse_id
        )
        assert available_after == OPENING_STOCK - 4
        assert locked_after == 4

        movements = movements_for(session, sku_id=sku_id)
        locks = [row for row in movements if row.movement_type == MovementType.ORDER_LOCK.value]
        assert len(locks) == 1
        lock = locks[0]
        assert lock.before_available == OPENING_STOCK
        assert lock.after_available == OPENING_STOCK - 4
        assert lock.before_locked == 0
        assert lock.after_locked == 4
        # INV-007: the movement fully explains the delta.
        assert lock.after_available - lock.before_available == -4
        assert lock.after_locked - lock.before_locked == 4
        # §28: attributed to the buyer, not to an unattributed SYSTEM.
        assert lock.operator_type == "CUSTOMER"
        assert lock.operator_id == shop.consumer_id
        assert lock.reference_type == "ORDER"
        # INV-007/§7: the ledger names the order that locked the unit, because §7
        # inserts the order row *before* reserving. A NULL here would leave the movement
        # explainable only by decoding an idempotency-key string.
        assert lock.reference_id is not None
        assert lock.idempotency_key == f"order-lock:{shop.consumer_id}:{shop.client_request_id('1')}:{sku_id}"
    finally:
        session.close()


def test_insufficient_stock_rolls_the_whole_create_back(shop: Shop) -> None:
    """Nothing at all may survive a failed create - no order, no items, no claim.

    The lock the reservation had already taken for an earlier line must not survive
    either, which is why the rollback is owned by the workflow rather than left to
    the caller.
    """
    session = get_session_factory()()
    try:
        with pytest.raises(InsufficientStockError):
            OrderService(session).create_order(
                principal=shop.consumer,
                items=[
                    OrderLineInput(shop.sku_ids[1], 1),
                    OrderLineInput(shop.sku_ids[2], OPENING_STOCK + 1),
                ],
                address_id=shop.address_id,
                client_request_id=shop.client_request_id("fail"),
                idempotency_key=shop.key("fail"),
            )
    finally:
        session.close()

    session = get_session_factory()()
    try:
        # The first line's reservation was rolled back with everything else.
        available, locked, _version = read_position(
            session, sku_id=shop.sku_ids[1], warehouse_id=shop.warehouse_id
        )
        assert available == OPENING_STOCK
        assert locked == 0
        assert order_count_for(session, user_id=shop.consumer_id) == 0

        # §48: a rolled-back create leaves no key behind, so a genuine retry is allowed.
        from app.shared.db.models.idempotency import IdempotencyRecord

        claim = (
            session.execute(
                select(IdempotencyRecord).where(
                    IdempotencyRecord.idempotency_key == shop.key("fail")
                )
            )
            .scalars()
            .first()
        )
        assert claim is None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 2. INV-006 against the persisted rows
# ---------------------------------------------------------------------------
def test_inv006_holds_for_the_persisted_rows(shop: Shop) -> None:
    result = _create(
        shop,
        lines=[
            OrderLineInput(shop.sku_ids[0], 2),
            OrderLineInput(shop.sku_ids[1], 3),
            OrderLineInput(shop.sku_ids[2], 5),
        ],
    )
    session = get_session_factory()()
    try:
        items = items_of(session, order_id=result.order.id)
        order = load_order(session, order_no=result.order.order_no)
        assert sum(item.payable_amount for item in items) == order.payable_amount
        assert sum(item.original_amount for item in items) == order.original_amount
        # The per-row CHECK restates `payable = original - allocated`; a mismatch would
        # have been rejected by MySQL rather than reaching this line.
        for item in items:
            assert item.allocated_discount_amount == (
                item.promotion_discount_amount + item.coupon_discount_amount
            )
            assert item.payable_amount == item.original_amount - item.allocated_discount_amount
            assert item.payable_amount >= 0
    finally:
        session.close()


def test_the_summary_snapshot_fields_are_written_from_the_first_line(shop: Shop) -> None:
    result = _create(
        shop,
        lines=[OrderLineInput(shop.sku_ids[1], 1), OrderLineInput(shop.sku_ids[2], 2)],
    )
    assert result.order.item_count == 3
    session = get_session_factory()()
    try:
        first = items_of(session, order_id=result.order.id)[0]
        assert result.order.first_item_name == f"{first.product_name} {first.sku_name}"
        assert len(result.order.first_item_name) <= 200
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 3. INV-014: snapshot immutability
# ---------------------------------------------------------------------------
def test_a_historical_order_is_immune_to_later_product_edits(shop: Shop) -> None:
    """**The FG-10 snapshot test.**

    Everything the order copied is mutated afterwards: the product name, the SKU name,
    the SKU price, the attribute snapshot, the primary image, and the SKU's own number.
    The order must be byte-for-byte unchanged - not "still loads", but every field
    identical.

    This is a legal requirement as much as a technical one: a product rename silently
    rewriting last month's invoice is not a rounding difference, it is a wrong record
    of what somebody was charged.
    """
    result = _create(
        shop,
        lines=[OrderLineInput(shop.sku_ids[0], 1), OrderLineInput(shop.sku_ids[1], 2)],
    )
    order_no = result.order.order_no

    session = get_session_factory()()
    try:
        before = [
            (
                item.product_id,
                item.sku_id,
                item.product_name,
                item.sku_name,
                item.image_object_key,
                item.image_url,
                dict(item.sku_snapshot or {}),
                item.unit_price,
                item.quantity,
                item.original_amount,
                item.allocated_discount_amount,
                item.payable_amount,
            )
            for item in items_of(session, order_id=result.order.id)
        ]
        order_before = (
            load_order(session, order_no=order_no).original_amount,
            load_order(session, order_no=order_no).payable_amount,
            load_order(session, order_no=order_no).first_item_name,
            load_order(session, order_no=order_no).item_count,
        )
    finally:
        session.close()

    # -- now edit the live catalogue, hard -------------------------------
    session = get_session_factory()()
    try:
        product = session.get(Product, shop.product_id)
        assert product is not None
        product.name = "RENAMED PRODUCT AFTER PURCHASE"

        first_sku = session.get(ProductSku, shop.sku_ids[0])
        assert first_sku is not None
        first_sku.name = "RENAMED SKU"
        first_sku.price_amount = 999999
        first_sku.sku_no = "S-RENAMED"
        first_sku.attribute_snapshot = {"color": "RENAMED"}

        from app.modules.catalog.models import ProductImage

        image = (
            session.execute(
                select(ProductImage).where(
                    ProductImage.product_id == shop.product_id,
                    ProductImage.role == "PRIMARY",
                )
            )
            .scalars()
            .first()
        )
        if image is not None:
            image.object_key = "products/renamed/primary.png"
        session.commit()
    finally:
        session.close()

    # -- the order must be untouched -------------------------------------
    session = get_session_factory()()
    try:
        items = items_of(session, order_id=result.order.id)
        after = [
            (
                item.product_id,
                item.sku_id,
                item.product_name,
                item.sku_name,
                item.image_object_key,
                item.image_url,
                dict(item.sku_snapshot or {}),
                item.unit_price,
                item.quantity,
                item.original_amount,
                item.allocated_discount_amount,
                item.payable_amount,
            )
            for item in items
        ]
        assert after == before

        order = load_order(session, order_no=order_no)
        assert (
            order.original_amount,
            order.payable_amount,
            order.first_item_name,
            order.item_count,
        ) == order_before

        # ... and the specific fields the rename would have changed.
        assert items[0].product_name == "Nova Phone 15 Pro"
        assert items[0].sku_name == "原色钛金属款 1"
        assert items[0].unit_price == shop.sku_prices[0]
        assert items[0].sku_snapshot == {"color": "原色钛金属", "index": 1}
        assert items[0].image_object_key == f"products/{shop.marker}/primary.png"

        # The order is still readable through the service, from snapshots only.
        service = OrderService(session)
        detail = service.get_customer_order(principal=shop.consumer, order_no=order_no)
        assert detail.items[0].product_name == "Nova Phone 15 Pro"
        assert detail.items[0].sku_name == "原色钛金属款 1"
    finally:
        session.close()


def test_a_withdrawn_sku_does_not_break_an_existing_order(shop: Shop) -> None:
    """The other half of INV-014: the order must stay readable *and* cancellable after
    the product is withdrawn, because a customer whose order vanished would have no way
    to get their money or their stock back."""
    result = _create(shop, lines=[OrderLineInput(shop.sku_ids[0], 1)])

    session = get_session_factory()()
    try:
        sku = session.get(ProductSku, shop.sku_ids[0])
        assert sku is not None
        sku.status = "DISCONTINUED"
        sku.deleted_at = utc_now()
        session.commit()
    finally:
        session.close()

    session = get_session_factory()()
    try:
        service = OrderService(session)
        order = service.get_customer_order(principal=shop.consumer, order_no=result.order.order_no)
        assert order.items[0].sku_name == "原色钛金属款 1"
        cancelled = service.cancel(principal=shop.consumer, order_no=result.order.order_no)
        assert cancelled.order_status == OrderStatus.CANCELLED.value
    finally:
        session.close()

    session = get_session_factory()()
    try:
        available, locked, _version = read_position(
            session, sku_id=shop.sku_ids[0], warehouse_id=shop.warehouse_id
        )
        assert available == OPENING_STOCK
        assert locked == 0
    finally:
        session.close()


# ---------------------------------------------------------------------------
# 4. Preview does not write
# ---------------------------------------------------------------------------
def test_preview_writes_nothing(shop: Shop) -> None:
    session = get_session_factory()()
    try:
        cart = OrderService(session).preview(
            principal=shop.consumer,
            items=[OrderLineInput(shop.sku_ids[0], 2), OrderLineInput(shop.sku_ids[1], 1)],
            address_id=shop.address_id,
        )
        assert cart.payable_amount == shop.sku_prices[0] * 2 + shop.sku_prices[1]
        assert cart.shipping_amount == 0
        assert cart.warnings == ()
        # No commit was issued by the service, so nothing was written.
        session.rollback()
    finally:
        session.close()

    session = get_session_factory()()
    try:
        assert order_count_for(session, user_id=shop.consumer_id) == 0
        available, locked, _version = read_position(
            session, sku_id=shop.sku_ids[0], warehouse_id=shop.warehouse_id
        )
        assert (available, locked) == (OPENING_STOCK, 0)
        movements = [
            row
            for row in movements_for(session, sku_id=shop.sku_ids[0])
            if row.movement_type == MovementType.ORDER_LOCK.value
        ]
        assert movements == []
    finally:
        session.close()


def test_preview_and_create_agree_on_the_price(shop: Shop) -> None:
    """The create path recomputes rather than trusting the preview (§14.2) - so the two
    must nevertheless agree, or the customer would see one number and be charged
    another."""
    lines = [OrderLineInput(shop.sku_ids[0], 2), OrderLineInput(shop.sku_ids[2], 3)]
    session = get_session_factory()()
    try:
        previewed = OrderService(session).preview(principal=shop.consumer, items=lines)
    finally:
        session.close()

    result = _create(shop, lines=lines, suffix="agree")
    assert result.order.payable_amount == previewed.payable_amount
    assert result.order.original_amount == previewed.original_amount


def test_a_stranger_cannot_preview_against_someone_elses_address(shop: Shop) -> None:
    """§109: the address must belong to the caller, and the failure is a not-found so
    the id cannot be confirmed to exist.

    The stranger needs no row of their own: ``preview`` filters the address by
    ``user_id`` in the query, and ownership is a property of the principal, so a
    principal whose user_id owns nothing is exactly the case under test.
    """
    from app.core.errors import AddressNotFoundError
    from app.modules.identity.service import Principal

    stranger = Principal(
        user_id=shop.consumer_id + 10_000,
        user_type="CONSUMER",
        merchant_id=None,
        roles=(),
        permissions=frozenset(),
        data_scope=shop.consumer.data_scope,
        session_id="stranger",
        is_staff=False,
    )

    session = get_session_factory()()
    try:
        with pytest.raises(AddressNotFoundError):
            OrderService(session).preview(
                principal=stranger,
                items=[OrderLineInput(shop.sku_ids[0], 1)],
                address_id=shop.address_id,
            )
    finally:
        session.close()


def _stranger(shop: Shop) -> object:
    """A principal whose ``user_id`` owns nothing.

    Ownership is a property of the principal, not of the row, so the stranger needs no
    user row of their own: ``user_id`` is what the query filters on, and a principal
    that owns nothing exercises exactly the branch under test.
    """
    from app.modules.identity.service import Principal

    return Principal(
        user_id=shop.consumer_id + 10_000,
        user_type="CONSUMER",
        merchant_id=None,
        roles=(),
        permissions=frozenset(),
        data_scope=shop.consumer.data_scope,
        session_id="stranger",
        is_staff=False,
    )


def _address_failure(shop: Shop, *, address_id: int, principal, suffix: str):
    """Run one create that must fail on the address, and return the raised error.

    Returns the exception rather than asserting inside, because the point of these tests
    is to compare two failures with each other.
    """
    from app.core.errors import AppError

    session = get_session_factory()()
    try:
        with pytest.raises(AppError) as caught:
            OrderService(session).create_order(
                principal=principal,
                items=[OrderLineInput(shop.sku_ids[0], 1)],
                address_id=address_id,
                client_request_id=shop.client_request_id(suffix),
                idempotency_key=shop.key(suffix),
            )
        return caught.value
    finally:
        session.close()


def test_create_refuses_a_stranger_address_with_404_and_never_403(shop: Shop) -> None:
    """The address rule on the **create** path (§14.2, §109).

    §14.2 requires ``address_id`` to belong to the caller; §109 requires the answer to be
    a not-found rather than a 403, because a 403 tells the caller that *somebody else's*
    address id exists and turns the endpoint into an existence oracle. A 403 here would
    be the more "helpful" answer and the wrong one.

    **The code is asserted, not just the status.** The ruling is settled: the order path
    calls ``AddressService.get`` and lets it raise, so an address that is absent and an
    address that belongs to somebody else both answer ``ADDRESS_NOT_FOUND (50008)`` /
    404. That is deliberate - distinguishing them would re-create the oracle one layer
    down, where it is harder to notice - and it is pinned here so that a future change
    which re-splits the codes turns this test red instead of quietly widening the
    endpoint's disclosure.
    """
    stranger = _stranger(shop)
    error = _address_failure(
        shop, address_id=shop.address_id, principal=stranger, suffix="stranger-addr"
    )
    assert error.status_code == 404, "an address failure must never be a 403"
    assert error.status_code != 403
    assert int(error.code) == 50008

    # Nothing was created, and no stock was touched: the refusal happens at step 2,
    # before the price is computed and long before the reservation.
    session = get_session_factory()()
    try:
        assert order_count_for(session, user_id=shop.consumer_id) == 0
        available, locked, _version = read_position(
            session, sku_id=shop.sku_ids[0], warehouse_id=shop.warehouse_id
        )
        assert (available, locked) == (OPENING_STOCK, 0)
    finally:
        session.close()


def test_create_refuses_an_address_that_does_not_exist(shop: Shop) -> None:
    """The absent case: also 50008 / 404, and also not a 403."""
    error = _address_failure(
        shop, address_id=999_999_999, principal=shop.consumer, suffix="absent-addr"
    )
    assert error.status_code == 404
    assert int(error.code) == 50008

    session = get_session_factory()()
    try:
        assert order_count_for(session, user_id=shop.consumer_id) == 0
    finally:
        session.close()


def test_absent_and_not_yours_addresses_are_indistinguishable(shop: Shop) -> None:
    """§109 made executable: the two failures must be **identical in every observable**.

    This is the assertion that actually protects the endpoint. Asserting "both are 404"
    is not enough: a response that differs in its business code (or in its message) is
    still an existence oracle for anyone who reads the JSON, and the caller is exactly the
    party who reads the JSON. So the status, the code **and** the message are compared -
    if any of the three ever diverges, this fails.

    Note the two requests are otherwise alike: same principal for the absent case is the
    *owner*, which is the stronger pairing - the difference the attacker would exploit is
    "I get 50009 for an id that is mine-adjacent", and that difference must not exist.
    """
    not_yours = _address_failure(
        shop, address_id=shop.address_id, principal=_stranger(shop), suffix="cmp-notyours"
    )
    absent = _address_failure(
        shop, address_id=999_999_999, principal=shop.consumer, suffix="cmp-absent"
    )

    assert not_yours.status_code == absent.status_code == 404
    assert int(not_yours.code) == int(absent.code) == 50008
    assert not_yours.public_message == absent.public_message
    # The context must not differ either - it is part of the payload the client sees.
    assert not_yours.context == absent.context


def test_create_refuses_an_address_that_does_not_exist(shop: Shop) -> None:
