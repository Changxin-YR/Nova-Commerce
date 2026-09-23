"""FG-10 (4/4) - the status machine against real rows: cancel and confirm-receipt.

Spec §27/§31/§36, §112 INV-003, API_CONTRACT §14.5, PHASE4_DESIGN §8.

Three things are checked that a response-only assertion would miss:

* **The stock really came back**, read from ``inventories`` and explained by an
  ``ORDER_RELEASE`` movement (INV-007). A cancel that flipped the status and forgot
  the release would leave the unit locked for ever, and the customer would never know.
* **The ledger is not double-credited.** A second cancel is refused with 50010 *and*
  appends no movement, because ``order-release:{order_no}:{sku_id}`` is UNIQUE
  (INV-003).
* **``confirm-receipt`` does not move ``fulfillment_status``.** §31 keeps the four
  axes independent; a receipt that also marked the order DELIVERED would be the
  state-machine collapse the spec forbids, and the assertion is on the stored column
  rather than on the response.

``PENDING_PAYMENT -> PROCESSING`` has no HTTP writer in Phase 4 (Phase 5's
``PaymentSuccessWorkflow`` writes it), so the tests that need a ``PROCESSING`` order
set the column directly. That is not a shortcut around the code under test - the
transition *guard* is what these tests exercise, and it reads the status from the row
exactly as it would in production.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, update

from app.core.errors import (
    OrderNotCancellableError,
    OrderNotConfirmableError,
    OrderNotFoundError,
)
from app.modules.identity.service import Principal
from app.modules.inventory.enums import MovementType
from app.modules.order.enums import OrderStatus
from app.modules.order.models import Order
from app.modules.order.service import DEFAULT_CANCEL_REASON, OrderService
from app.modules.order.workflow import OrderLineInput
from app.shared.db.base import utc_now
from app.shared.db.session import get_session_factory

from .conftest import (
    OPENING_STOCK,
    Shop,
    load_order,
    movements_for,
    order_count_for,
    read_position,
    status_logs,
)

pytestmark = [pytest.mark.integration]


def _create(shop: Shop, *, suffix: str, lines=None, quantity: int = 2):
    session = get_session_factory()()
    try:
        return OrderService(session).create_order(
            principal=shop.consumer,
            items=lines or [OrderLineInput(shop.sku_ids[0], quantity)],
            address_id=shop.address_id,
            client_request_id=shop.client_request_id(suffix),
            idempotency_key=shop.key(suffix),
        )
    finally:
        session.close()


def _cancel(shop: Shop, *, order_no: str, principal=None, reason=None):
    session = get_session_factory()()
    try:
        return OrderService(session).cancel(
            principal=principal or shop.consumer, order_no=order_no, reason=reason
        )
    finally:
        session.close()


def _confirm(shop: Shop, *, order_no: str, principal=None):
    session = get_session_factory()()
    try:
        return OrderService(session).confirm_receipt(
            principal=principal or shop.consumer, order_no=order_no
        )
    finally:
        session.close()


def _force_status(order_no: str, status: str) -> None:
    """Simulate the Phase 5 payment workflow, which is the only writer of PROCESSING."""
    session = get_session_factory()()
    try:
        session.execute(
            update(Order)
            .where(Order.order_no == order_no)
            .values(order_status=status, payment_status="PAID", paid_at=utc_now())
        )
        session.commit()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------
def test_cancel_releases_the_stock_and_appends_the_ledger_entry(shop: Shop) -> None:
    created = _create(shop, suffix="cancel")
    sku_id = shop.sku_ids[0]

    cancelled = _cancel(shop, order_no=created.order.order_no, reason="用户主动取消")
    assert cancelled.order_status == OrderStatus.CANCELLED.value
    assert cancelled.cancel_reason == "用户主动取消"
    assert cancelled.cancelled_at is not None
    assert cancelled.payment_status == "UNPAID"  # untouched: payment is a different axis
    assert cancelled.fulfillment_status == "UNFULFILLED"

    session = get_session_factory()()
    try:
        available, locked, _version = read_position(
            session, sku_id=sku_id, warehouse_id=shop.warehouse_id
        )
        assert available == OPENING_STOCK
        assert locked == 0

        releases = [
            row
            for row in movements_for(session, sku_id=sku_id)
            if row.movement_type == MovementType.ORDER_RELEASE.value
        ]
        assert len(releases) == 1
        release = releases[0]
        assert release.before_locked == 2
        assert release.after_locked == 0
        assert release.before_available == OPENING_STOCK - 2
        assert release.after_available == OPENING_STOCK
        assert release.idempotency_key == f"order-release:{created.order.order_no}:{sku_id}"
        assert release.reference_type == "ORDER"
        assert release.reference_id == created.order.id
        assert release.operator_type == "CUSTOMER"
        assert release.reason == "order cancelled"

        # §36: exactly one more log row, and it describes the real transition.
        logs = status_logs(session, order_id=created.order.id)
        assert [log.to_status for log in logs] == ["PENDING_PAYMENT", "CANCELLED"]
        assert logs[1].from_status == "PENDING_PAYMENT"
        assert logs[1].reason == "用户主动取消"
        assert logs[1].operator_type == "CUSTOMER"
        assert logs[1].order_no == created.order.order_no
    finally:
        session.close()


def test_cancel_releases_every_line_of_a_multi_line_order(shop: Shop) -> None:
    """Each line's release targets the warehouse stored **on the line** - "the row that
    was locked" (§4.2) - so a multi-line cancel must not resolve a default warehouse and
    release the wrong row."""
    created = _create(
        shop,
        suffix="cancel-multi",
        lines=[
            OrderLineInput(shop.sku_ids[0], 2),
            OrderLineInput(shop.sku_ids[1], 3),
            OrderLineInput(shop.sku_ids[2], 5),
        ],
    )
    _cancel(shop, order_no=created.order.order_no)

    session = get_session_factory()()
    try:
        for index, quantity in enumerate((2, 3, 5)):
            available, locked, _version = read_position(
                session, sku_id=shop.sku_ids[index], warehouse_id=shop.warehouse_id
            )
            assert (available, locked) == (OPENING_STOCK, 0)
            releases = [
                row
                for row in movements_for(session, sku_id=shop.sku_ids[index])
                if row.movement_type == MovementType.ORDER_RELEASE.value
            ]
            assert len(releases) == 1
            assert releases[0].before_locked == quantity
    finally:
        session.close()


def test_a_second_cancel_is_refused_and_releases_nothing_more(shop: Shop) -> None:
    """50010, and the ledger is not credited twice (INV-003)."""
    created = _create(shop, suffix="double-cancel")
    sku_id = shop.sku_ids[0]
    _cancel(shop, order_no=created.order.order_no)

    with pytest.raises(OrderNotCancellableError) as caught:
        _cancel(shop, order_no=created.order.order_no)
    assert int(caught.value.code) == 50_010
    assert caught.value.status_code == 409
    assert caught.value.context["from_status"] == "CANCELLED"
    assert caught.value.context["to_status"] == "CANCELLED"

    session = get_session_factory()()
    try:
        available, locked, _version = read_position(
            session, sku_id=sku_id, warehouse_id=shop.warehouse_id
        )
        # Still released exactly once - not twice, which would invent stock.
        assert (available, locked) == (OPENING_STOCK, 0)
        releases = [
            row
            for row in movements_for(session, sku_id=sku_id)
            if row.movement_type == MovementType.ORDER_RELEASE.value
        ]
        assert len(releases) == 1
        assert len(status_logs(session, order_id=created.order.id)) == 2
    finally:
        session.close()


def test_cancelling_a_processing_order_is_refused(shop: Shop) -> None:
    """50010 - a paid order is cancelled by a refund (Phase 5), not by this endpoint."""
    created = _create(shop, suffix="cancel-processing")
    _force_status(created.order.order_no, OrderStatus.PROCESSING.value)

    with pytest.raises(OrderNotCancellableError) as caught:
        _cancel(shop, order_no=created.order.order_no)
    assert int(caught.value.code) == 50_010

    session = get_session_factory()()
    try:
        # The reservation is still held, because the cancel did not happen.
        available, locked, _version = read_position(
            session, sku_id=shop.sku_ids[0], warehouse_id=shop.warehouse_id
        )
        assert locked == 2
        assert available == OPENING_STOCK - 2
        releases = [
            row
            for row in movements_for(session, sku_id=shop.sku_ids[0])
            if row.movement_type == MovementType.ORDER_RELEASE.value
        ]
        assert releases == []
        assert len(status_logs(session, order_id=created.order.id)) == 1
    finally:
        session.close()


def test_a_blank_reason_falls_back_to_the_server_wording(shop: Shop) -> None:
    """§6: ``cancel_reason`` is server-owned - the client cannot infer it - so an
    omitted reason must still be recorded as something."""
    created = _create(shop, suffix="cancel-default")
    cancelled = _cancel(shop, order_no=created.order.order_no, reason=None)
    assert cancelled.cancel_reason == DEFAULT_CANCEL_REASON


def test_a_stranger_cannot_cancel_someone_elses_order(shop: Shop) -> None:
    """50003, never 403 (§109) - and the order is untouched."""
    created = _create(shop, suffix="cancel-stranger")
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

    with pytest.raises(OrderNotFoundError) as caught:
        _cancel(shop, order_no=created.order.order_no, principal=stranger)
    assert int(caught.value.code) == 50_003
    assert caught.value.status_code == 404

    session = get_session_factory()()
    try:
        order = load_order(session, order_no=created.order.order_no)
        assert order.order_status == OrderStatus.PENDING_PAYMENT.value
        _available, locked, _version = read_position(
            session, sku_id=shop.sku_ids[0], warehouse_id=shop.warehouse_id
        )
        assert locked == 2
    finally:
        session.close()


def test_an_unknown_order_number_is_50003(shop: Shop) -> None:
    with pytest.raises(OrderNotFoundError):
        _cancel(shop, order_no="NV19990101000001")


# ---------------------------------------------------------------------------
# Confirm receipt
# ---------------------------------------------------------------------------
def test_confirm_receipt_completes_the_order_without_touching_fulfillment(shop: Shop) -> None:
    """§31: the four status axes are independent, and this is the assertion that keeps
    them so."""
    created = _create(shop, suffix="confirm")
    _force_status(created.order.order_no, OrderStatus.PROCESSING.value)

    completed = _confirm(shop, order_no=created.order.order_no)
    assert completed.order_status == OrderStatus.COMPLETED.value
    assert completed.completed_at is not None
    # Untouched: the carrier delivered the goods; this is a different fact.
    assert completed.fulfillment_status == "UNFULFILLED"
    assert completed.payment_status == "PAID"  # written by Phase 5, not by us
    assert completed.cancel_reason is None

    session = get_session_factory()()
    try:
        logs = status_logs(session, order_id=created.order.id)
        assert [log.to_status for log in logs] == ["PENDING_PAYMENT", "COMPLETED"]
        assert logs[1].from_status == "PROCESSING"
        assert logs[1].operator_type == "CUSTOMER"
        # The stock stays locked: a completed order has been sold, not returned.
        _available, locked, _version = read_position(
            session, sku_id=shop.sku_ids[0], warehouse_id=shop.warehouse_id
        )
        assert locked == 2
    finally:
        session.close()


def test_confirm_receipt_from_pending_payment_is_refused(shop: Shop) -> None:
    """50011. An unpaid order cannot have been received."""
    created = _create(shop, suffix="confirm-early")

    with pytest.raises(OrderNotConfirmableError) as caught:
        _confirm(shop, order_no=created.order.order_no)
    assert int(caught.value.code) == 50_011
    assert caught.value.status_code == 409
    assert caught.value.context["from_status"] == "PENDING_PAYMENT"
    assert caught.value.context["allowed"] == ["CANCELLED", "CLOSED", "PROCESSING"]

    session = get_session_factory()()
    try:
        order = load_order(session, order_no=created.order.order_no)
        assert order.order_status == OrderStatus.PENDING_PAYMENT.value
        assert order.completed_at is None
        assert len(status_logs(session, order_id=created.order.id)) == 1
    finally:
        session.close()


def test_confirm_receipt_is_refused_after_a_cancel(shop: Shop) -> None:
    created = _create(shop, suffix="confirm-cancelled")
    _cancel(shop, order_no=created.order.order_no)
    with pytest.raises(OrderNotConfirmableError):
        _confirm(shop, order_no=created.order.order_no)


def test_confirm_receipt_is_refused_twice(shop: Shop) -> None:
    created = _create(shop, suffix="confirm-twice")
    _force_status(created.order.order_no, OrderStatus.PROCESSING.value)
    _confirm(shop, order_no=created.order.order_no)

    with pytest.raises(OrderNotConfirmableError) as caught:
        _confirm(shop, order_no=created.order.order_no)
    assert int(caught.value.code) == 50_011
    assert caught.value.context["from_status"] == "COMPLETED"


def test_the_version_counter_advances_on_every_transition(shop: Shop) -> None:
    """§26: the optimistic-locking counter is bumped by the paths that mutate the row."""
    created = _create(shop, suffix="version")
    session = get_session_factory()()
    try:
        before = load_order(session, order_no=created.order.order_no).version
    finally:
        session.close()

    _cancel(shop, order_no=created.order.order_no)

    session = get_session_factory()()
    try:
        after = load_order(session, order_no=created.order.order_no).version
        assert after == before + 1
    finally:
        session.close()


def test_a_cancelled_order_can_still_be_read_by_its_owner(shop: Shop) -> None:
    """The order must not disappear from history when it is cancelled."""
    created = _create(shop, suffix="read-after-cancel")
    _cancel(shop, order_no=created.order.order_no)

    session = get_session_factory()()
    try:
        order = OrderService(session).get_customer_order(
            principal=shop.consumer, order_no=created.order.order_no
        )
        assert order.order_status == OrderStatus.CANCELLED.value
        page = OrderService(session).list_customer_orders(
            principal=shop.consumer, order_status="CANCELLED"
        )
        assert created.order.order_no in {row.order_no for row in page.rows}
        # ... and it is absent from the "still waiting" filter.
        pending = OrderService(session).list_customer_orders(
            principal=shop.consumer, order_status="PENDING_PAYMENT"
        )
        assert created.order.order_no not in {row.order_no for row in pending.rows}
    finally:
        session.close()


def test_the_order_count_is_the_number_of_create_calls(shop: Shop) -> None:
    """One create, one row - and the transitions above add none."""
    _create(shop, suffix="count")
    session = get_session_factory()()
    try:
        assert order_count_for(session, user_id=shop.consumer_id) == 1
        row = session.execute(
            select(Order.order_status).where(Order.user_id == shop.consumer_id)
        ).scalars().one()
        assert row == OrderStatus.PENDING_PAYMENT.value
    finally:
        session.close()
