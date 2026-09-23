"""The Phase 4 CHECK constraints are enforcement, not decoration.

Spec §112, `docs/architecture/ARCHITECTURE_INVARIANTS.md` ("*An invariant is only
real if something fails when you break it. An invariant with no executable
enforcement is a comment, not an invariant*"), PHASE4_DESIGN §4.1/§4.2.

## Why this file exists

The rest of the order suite asserts that the amounts, allocations and statuses it
writes are **correct**. None of it asserts that the database would **refuse** an
incorrect one. Those are different claims, and only the second one is what makes
INV-006 real at rest rather than merely true of the code path that ran.

So every probe here deliberately writes a value that violates exactly one named
constraint and asserts three things:

1. the write is **rejected**,
2. it is rejected as a **CHECK** violation (MySQL errno 3819), not as something
   else that happens to fail - a test that accepts any exception passes for the
   wrong reason,
3. the **stored row is unchanged** afterwards.

## The trap this avoids (learned from the data-layer author, and re-verified here)

MySQL reports a violated CHECK as **errno 3819 → ``sqlalchemy.exc.OperationalError``**,
while a unique-key violation is **1062 → ``IntegrityError``**. So
``pytest.raises(IntegrityError)`` does **not** catch a CHECK violation, and a test
written that way would appear to pass while proving nothing about the constraint.
Verified in this environment before this file was written:

    CHECK  -> OperationalError | (3819, "Check constraint '...' is violated.")
    UNIQUE -> IntegrityError   | (1062, "Duplicate entry ...")

Each probe runs inside ``begin_nested()``. Rolling back to a SAVEPOINT leaves the
session usable, so one session can carry every probe and the assertions can still
read the row back afterwards. Calling ``session.rollback()`` instead would discard
the surrounding transaction and the *next* statement would fail with "transaction
already deassociated from connection" - testing the teardown rather than the
constraint.

## Why a per-row CHECK is not a substitute for INV-006

``order_items``' two leg constraints and ``orders``' identity constraint cannot
express INV-006 itself, which spans rows: they are necessary but not sufficient.
The cross-row sum is asserted in the creating transaction (``workflow._assert_invariants``)
and again from the committed rows in ``test_inv006_allocation.py``. This file covers
the other half: the legs the database will not let you bend.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from app.modules.order.service import OrderService
from app.modules.order.workflow import OrderLineInput
from app.shared.db.session import get_session_factory

from .conftest import Shop

pytestmark = [pytest.mark.integration]

#: MySQL's errno for a violated CHECK constraint.
ERRNO_CHECK_VIOLATION = 3819

#: (label, which row the probe targets, statement, the constraint that must fire)
#:
#: Every statement is built to violate **exactly one** constraint, so the assertion
#: can name it. Several of these constraints are coupled - changing
#: ``allocated_discount_amount`` would break both the "allocated = promotion + coupon"
#: leg *and* the "payable = original - allocated" leg - which is why the probes touch
#: the fields that appear in only one of them. ``paid_amount`` and ``refunded_amount``,
#: for instance, appear in the non-negativity list but in neither identity, so setting
#: one to -1 isolates that constraint.
PROBES: tuple[tuple[str, str, str, str], ...] = (
    (
        "orders.payable_consistent",
        "order",
        "UPDATE orders SET payable_amount = payable_amount + 1 WHERE id = :row_id",
        "ck_orders_payable_consistent",
    ),
    (
        "orders.amounts_non_negative",
        "order",
        # paid_amount is in the non-negativity list but in no identity, so this
        # cannot accidentally trip the payable identity as well.
        "UPDATE orders SET paid_amount = -1 WHERE id = :row_id",
        "ck_orders_amounts_non_negative",
    ),
    (
        "orders.status_valid",
        "order",
        # 'SHIPPED' is a *fulfillment* status (§31). Proving the database refuses it
        # in the order_status column is what makes the four-axis separation a
        # constraint rather than a convention.
        "UPDATE orders SET order_status = 'SHIPPED' WHERE id = :row_id",
        "ck_orders_status_valid",
    ),
    (
        "order_items.payable_consistent",
        "item",
        "UPDATE order_items SET payable_amount = payable_amount + 1 WHERE order_id = :row_id",
        "ck_order_items_payable_consistent",
    ),
    (
        "order_items.allocated_consistent",
        "item",
        # Moves promotion_discount_amount while leaving `allocated` and `payable`
        # alone: breaks "allocated = promotion + coupon" only.
        "UPDATE order_items "
        "SET promotion_discount_amount = promotion_discount_amount + 1 "
        "WHERE order_id = :row_id",
        "ck_order_items_allocated_consistent",
    ),
    (
        "order_items.amounts_non_negative",
        "item",
        "UPDATE order_items SET refunded_amount = -1 WHERE order_id = :row_id",
        "ck_order_items_amounts_non_negative",
    ),
    (
        "order_items.quantity_positive",
        "item",
        "UPDATE order_items SET quantity = 0 WHERE order_id = :row_id",
        "ck_order_items_quantity_positive",
    ),
)


@pytest.fixture
def order_with_item(shop: Shop):
    """One committed order with one line, plus a session from which to attack it."""
    session = get_session_factory()()
    result = OrderService(session).create_order(
        principal=shop.consumer,
        items=[OrderLineInput(shop.sku_ids[0], 2)],
        address_id=shop.address_id,
        client_request_id=shop.client_request_id("constraints"),
        idempotency_key=shop.key("constraints"),
    )
    order_id = result.order.id
    session.close()
    return order_id


def _read_row(order_id: int, target: str) -> tuple:
    """Snapshot the columns a probe could touch, for a before/after comparison."""
    session = get_session_factory()()
    try:
        if target == "order":
            row = session.execute(
                text(
                    "SELECT order_status, payable_amount, paid_amount, refunded_amount "
                    "FROM orders WHERE id = :row_id"
                ),
                {"row_id": order_id},
            ).one()
        else:
            row = session.execute(
                text(
                    "SELECT allocated_discount_amount, promotion_discount_amount, "
                    "payable_amount, refunded_amount, quantity "
                    "FROM order_items WHERE order_id = :row_id"
                ),
                {"row_id": order_id},
            ).one()
        return tuple(row)
    finally:
        session.close()


@pytest.mark.parametrize(
    ("label", "target", "statement", "constraint"),
    PROBES,
    ids=[probe[0] for probe in PROBES],
)
def test_a_violating_write_is_refused_by_the_named_check(
    shop: Shop, order_with_item: int, label: str, target: str, statement: str, constraint: str
) -> None:
    """The constraint rejects the write, names itself, and changes nothing."""
    before = _read_row(order_with_item, target)

    session = get_session_factory()()
    try:
        # SAVEPOINT: a failed statement is confined here, so the session stays
        # usable and the before/after comparison below is meaningful. `begin_nested()`
        # re-raises after rolling back, so `pytest.raises` still sees the error.
        with pytest.raises(DBAPIError) as caught, session.begin_nested():
            session.execute(text(statement), {"row_id": order_with_item})

        error = caught.value
        assert isinstance(error, OperationalError), (
            f"{label}: expected a CHECK violation (OperationalError/3819), got "
            f"{type(error).__name__}"
        )
        assert not isinstance(error, IntegrityError), (
            f"{label}: a CHECK violation is not an IntegrityError - catching the wrong "
            "class is how a constraint test passes for the wrong reason"
        )
        assert error.orig.args[0] == ERRNO_CHECK_VIOLATION, error.orig.args

        # The constraint that fired must be the one under test: asserting only "some
        # CHECK failed" would pass when the probe tripped a neighbouring constraint.
        assert constraint in str(error.orig.args[1]), (
            f"{label}: expected {constraint}, server said: {error.orig.args[1]}"
        )
    finally:
        session.rollback()
        session.close()

    # ... and the row is untouched, which is the whole point of a rejected write.
    assert _read_row(order_with_item, target) == before, f"{label}: the refused write still landed"


def test_order_items_never_permits_a_second_line_for_the_same_sku(shop: Shop) -> None:
    """``uq_order_items_order_sku`` is what forces the pre-pricing merge in §14.2.

    A *unique* constraint, so this one really is an ``IntegrityError`` (1062) - the
    contrast that makes the file's other assertion meaningful.
    """
    session = get_session_factory()()
    try:
        result = OrderService(session).create_order(
            principal=shop.consumer,
            items=[OrderLineInput(shop.sku_ids[1], 1)],
            address_id=shop.address_id,
            client_request_id=shop.client_request_id("dup-sku"),
            idempotency_key=shop.key("dup-sku"),
        )
        order_id = result.order.id
    finally:
        session.close()

    session = get_session_factory()()
    try:
        with pytest.raises(IntegrityError) as caught, session.begin_nested():
            session.execute(
                text(
                    "INSERT INTO order_items "
                    "(order_id, warehouse_id, product_id, sku_id, product_name, sku_name, "
                    " unit_price, quantity, original_amount, promotion_discount_amount, "
                    " coupon_discount_amount, allocated_discount_amount, payable_amount, "
                    " refunded_amount, after_sale_status, created_at, updated_at) "
                    "SELECT order_id, warehouse_id, product_id, sku_id, product_name, "
                    " sku_name, unit_price, quantity, original_amount, "
                    " promotion_discount_amount, coupon_discount_amount, "
                    " allocated_discount_amount, payable_amount, refunded_amount, "
                    " after_sale_status, NOW(3), NOW(3) "
                    "FROM order_items WHERE order_id = :row_id LIMIT 1"
                ),
                {"row_id": order_id},
            )
        assert caught.value.orig.args[0] == 1062, caught.value.orig.args
    finally:
        session.rollback()
        session.close()

    session = get_session_factory()()
    try:
        count = session.execute(
            text("SELECT COUNT(*) FROM order_items WHERE order_id = :row_id"),
            {"row_id": order_id},
        ).scalar_one()
        assert count == 1
    finally:
        session.close()


def test_the_session_survives_a_rejected_write(shop: Shop, order_with_item: int) -> None:
    """The mechanics the rest of this file depends on, asserted rather than assumed.

    A rejected statement inside ``begin_nested()`` must leave the session able to
    keep working. If that ever stopped being true, every probe above would start
    failing for a reason that had nothing to do with the constraint - and the failure
    would look like a constraint defect.
    """
    session = get_session_factory()()
    try:
        with pytest.raises(OperationalError), session.begin_nested():
            session.execute(
                text("UPDATE orders SET paid_amount = -1 WHERE id = :row_id"),
                {"row_id": order_with_item},
            )
        # Both a read and a write still work, in the same session, after the refusal.
        assert session.execute(text("SELECT 1")).scalar() == 1
        with session.begin_nested():
            session.execute(
                text("UPDATE orders SET cancel_reason = 'probe' WHERE id = :row_id"),
                {"row_id": order_with_item},
            )
        assert (
            session.execute(
                text("SELECT cancel_reason FROM orders WHERE id = :row_id"),
                {"row_id": order_with_item},
            ).scalar()
            == "probe"
        )
        session.rollback()
    finally:
        session.close()
