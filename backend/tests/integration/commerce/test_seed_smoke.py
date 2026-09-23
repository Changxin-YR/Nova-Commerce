"""Smoke test for the shared Phase 5 seed's `paid_order()` helper.

## Why this file exists, and why it was briefly absent

`PHASE5_DESIGN` section 12 promises that every Phase 5 test imports its fixtures
from `tests/integration/commerce/seed.py`. The captain pointed out the hole that
left: `paid_order()` drives the **real** payment path, and **nothing exercised it**
- grep showed it referenced only by its own definition and by the package
`conftest`, so a break in the helper would have silently given every test built on
it the wrong fixture, with the failure appearing in whatever test ran next.

That is the worst combination for a helper: non-trivial behaviour (three real
workflow calls, a signed callback, committed side effects) and no direct test.
This restores the coverage.

It is deliberately a **behavioural** test rather than a shape assertion: it checks
that the settlement really happened and really produced the side effects callers
rely on, because those are what a future change to the payment path would break.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.modules.fulfillment.models import Fulfillment, FulfillmentItem
from app.modules.order.enums import FulfillmentStatus, OrderStatus, PaymentStatus
from app.modules.order.workflow import OrderLineInput
from app.shared.db.session import get_session_factory

from .seed import OPENING_STOCK, Shop, items_of, load_order, paid_order, read_position, status_logs

pytestmark = [pytest.mark.integration]


def test_paid_order_reaches_paid_through_the_real_payment_path(shop: Shop) -> None:
    """The helper's whole contract: the order is genuinely settled, not hand-written."""
    order = paid_order(shop, lines=[OrderLineInput(shop.sku_ids[0], 3)], suffix="smoke")

    session = get_session_factory()()
    try:
        stored = load_order(session, order_no=order.order_no)

        # -- the three axes the settlement is responsible for -------------------
        assert stored.order_status == OrderStatus.PROCESSING.value
        assert stored.payment_status == PaymentStatus.PAID.value
        # Creating a package is NOT shipping it (design 6.1 step 9).
        assert stored.fulfillment_status == FulfillmentStatus.UNFULFILLED.value
        assert stored.paid_amount == stored.payable_amount > 0
        assert stored.refunded_amount == 0

        # -- the audit trail the settlement must leave -------------------------
        logs = status_logs(session, order_id=stored.id)
        assert [entry.to_status for entry in logs] == [
            OrderStatus.PENDING_PAYMENT.value,
            OrderStatus.PROCESSING.value,
        ]

        # -- exactly ONE fulfillment shell, carrying the order's lines ---------
        # (the single-shell assertion the previous version of this test made, and
        # the property a retried callback would break)
        shells = list(
            session.query(Fulfillment).filter(Fulfillment.order_id == stored.id).all()
        )
        assert len(shells) == 1, f"expected exactly one shell, found {len(shells)}"
        shell_items = (
            session.query(FulfillmentItem)
            .filter(FulfillmentItem.fulfillment_id == shells[0].id)
            .all()
        )
        order_items = items_of(session, order_id=stored.id)
        assert len(shell_items) == len(order_items)
        assert {i.order_item_id: i.quantity for i in shell_items} == {
            i.id: i.quantity for i in order_items
        }

        # -- the reservation really happened, and the settlement consumed it ----
        # available fell at RESERVE time (CreateOrderWorkflow) and is untouched by
        # the deduction; `deduct` only lowers `locked_qty` because the unit already
        # left availability when it was reserved. So after a full settle:
        available, locked, _ = read_position(
            session, sku_id=shop.sku_ids[0], warehouse_id=shop.warehouse_id
        )
        assert available == OPENING_STOCK - 3, "the reservation took 3 out of available"
        assert locked == 0, "the deduction consumed the reservation"

        # ...and the ledger explains both moves, which is INV-007.
        movements = session.execute(
            text(
                "SELECT movement_type, COUNT(*) FROM inventory_movements "
                "WHERE sku_id = :s AND idempotency_key LIKE :p GROUP BY movement_type"
            ),
            {"s": shop.sku_ids[0], "p": f"%{order.order_no}%"},
        ).all()
        by_type = {row[0]: int(row[1]) for row in movements}
        assert by_type.get("ORDER_DEDUCT") == len(order_items), (
            f"one ORDER_DEDUCT per line expected, got {by_type}"
        )
    finally:
        session.close()


def test_paid_order_is_idempotent_about_the_shell_not_the_order(shop: Shop) -> None:
    """Two calls produce two orders, each with its own single shell.

    Guards the obvious misuse: a caller that calls `paid_order` twice for one shop
    must get two independent settled orders, not one order with two shells - which
    is the state a duplicate callback would create, and the thing FG-11 exists to
    rule out.
    """
    first = paid_order(shop, suffix="twice-a")
    second = paid_order(shop, suffix="twice-b")
    assert first.order_no != second.order_no

    session = get_session_factory()()
    try:
        for order in (first, second):
            count = (
                session.query(Fulfillment)
                .filter(Fulfillment.order_id == order.id)
                .count()
            )
            assert count == 1, f"order {order.order_no} has {count} shells, expected 1"
    finally:
        session.close()


def test_the_seed_states_are_what_design_12_promises(shop: Shop) -> None:
    """The fixture's own facts, so a drift in the seed is reported as a seed failure."""
    assert shop.sku_prices == (1999, 2999, 999), "non-round prices are load-bearing"
    session = get_session_factory()()
    try:
        # no order has been created by the fixture itself
        assert (
            session.query(Fulfillment).filter(Fulfillment.order_id.is_(None)).count() == 0
        )
        available, locked, _ = read_position(
            session, sku_id=shop.sku_ids[2], warehouse_id=shop.warehouse_id
        )
        assert available == OPENING_STOCK
        assert locked == 0
        # the marker prefix is what teardown attributes rows by
        assert session.execute(
            text("SELECT COUNT(*) FROM merchants WHERE code LIKE :p"),
            {"p": f"M{shop.marker}%"},
        ).scalar_one() == 1
    finally:
        session.close()
