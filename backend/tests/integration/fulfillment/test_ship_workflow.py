"""The ship path on real MySQL: multi-package, the cumulative cap, and the axes.

These are the Phase 5 properties that a mock cannot prove (spec section 113 forbids
claiming them from one):

* a package is not a shipment - the payment path's shell must leave the order's
  ``fulfillment_status`` at ``UNFULFILLED``;
* a partial shipment must **create** the residual package rather than lose it
  (REQ-FUL-001, multi-package per order);
* the cumulative per-line cap is summed across *all* packages of the order, which is
  the rule a per-package check passes while over-shipping (70 001);
* **shipping never changes ``order_status``** (REQ-ORD-003/005, spec section 31);
* the "how much has shipped" total must count shipped packages only. Counting
  ``UNFULFILLED`` shells would report the order's *plan* as shipped, and with a
  full-width shell the guard then refuses the order's first legitimate shipment
  outright. That defect is why the test below ships an order at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, ErrorCode
from app.modules.fulfillment.models import Fulfillment
from app.modules.fulfillment.schemas import ShipFulfillmentRequest
from app.modules.fulfillment.service import FulfillmentService
from app.modules.order.enums import FulfillmentStatus, OrderStatus, PaymentStatus
from app.modules.order.models import OrderItem
from tests.integration.fulfillment.conftest import Commerce, order_of, packages_of

pytestmark = pytest.mark.integration


def _create_shell(session: Session, commerce: Commerce) -> Fulfillment:
    """Drive the real shell write - the same call ``PaymentSuccessWorkflow`` makes."""
    order = order_of(session, commerce)
    items = list(session.scalars(select(OrderItem).where(OrderItem.order_id == commerce.order_id)))
    shell = FulfillmentService(session).create_shell(
        order=order, items=items, warehouse_id=commerce.warehouse_id
    )
    session.commit()
    return shell


def _ship(session: Session, commerce: Commerce, fulfillment_id: int, quantity: int, **kwargs) -> Fulfillment:
    payload = ShipFulfillmentRequest(
        carrier=kwargs.pop("carrier", "SF"),
        tracking_no=kwargs.pop("tracking_no", f"SF-{commerce.marker}"),
        item_quantities=[{"order_item_id": commerce.order_item_id, "quantity": quantity}],
        **kwargs,
    )
    fulfillment = FulfillmentService(session).ship(
        principal=commerce.staff, fulfillment_id=fulfillment_id, payload=payload
    )
    session.commit()
    return fulfillment


# ---------------------------------------------------------------------------
# The shell
# ---------------------------------------------------------------------------
def test_create_shell_is_one_unfulfilled_package_per_order_line(session: Session, commerce: Commerce) -> None:
    shell = _create_shell(session, commerce)

    assert shell.fulfillment_status == FulfillmentStatus.UNFULFILLED.value
    assert shell.order_no == commerce.order_no
    assert shell.warehouse_id == commerce.warehouse_id
    assert shell.fulfillment_no.startswith("NVF"), shell.fulfillment_no
    assert len(shell.items) == 1
    assert shell.items[0].order_item_id == commerce.order_item_id
    # The **full** line quantity: this is the record of what is owed.
    assert shell.items[0].quantity == 3


def test_create_shell_leaves_the_order_axis_alone(session: Session, commerce: Commerce) -> None:
    """Design 6.1 step 9: creating a package is not shipping it.

    A shell that moved the axis would tell the customer their order is on its way the
    instant payment cleared.
    """
    _create_shell(session, commerce)

    order = order_of(session, commerce)
    assert order.fulfillment_status == FulfillmentStatus.UNFULFILLED.value


def test_create_shell_is_idempotent_per_order(session: Session, commerce: Commerce) -> None:
    """A retried payment workflow must not create a second package.

    The retry happens after a crash between the deduction and the commit, so the
    second call sees an existing shell and must return it rather than append another -
    otherwise the operator is told twice as much stock is owed.
    """
    first = _create_shell(session, commerce)
    second = _create_shell(session, commerce)

    assert first.id == second.id
    assert len(packages_of(session, commerce)) == 1


# ---------------------------------------------------------------------------
# Multi-package shipping
# ---------------------------------------------------------------------------
def test_a_partial_shipment_creates_the_residual_package(session: Session, commerce: Commerce) -> None:
    """A 3-unit line shipped as 2 then 1: the remainder must not be dropped.

    This is the multi-package rule (REQ-FUL-001) and the reason ``ship`` is keyed by
    fulfillment id rather than order number.
    """
    shell = _create_shell(session, commerce)

    shipped = _ship(session, commerce, shell.id, quantity=2)

    assert shipped.fulfillment_status == FulfillmentStatus.SHIPPED.value
    assert shipped.carrier == "SF"
    assert shipped.tracking_no == f"SF-{commerce.marker}"
    assert shipped.shipped_at is not None
    # The shipped package carries exactly what left, not the plan.
    assert [(i.order_item_id, i.quantity) for i in shipped.items] == [(commerce.order_item_id, 2)]

    packages = packages_of(session, commerce)
    assert len(packages) == 2, "the remainder must exist as a new package"
    residual = packages[1]
    assert residual.fulfillment_status == FulfillmentStatus.UNFULFILLED.value
    assert residual.fulfillment_no != shipped.fulfillment_no
    assert [(i.order_item_id, i.quantity) for i in residual.items] == [(commerce.order_item_id, 1)]
    # The residual is still shippable from the same warehouse the stock came from.
    assert residual.warehouse_id == commerce.warehouse_id

    order = order_of(session, commerce)
    assert order.fulfillment_status == FulfillmentStatus.PARTIAL_SHIPPED.value


def test_shipping_the_residual_reaches_shipped(session: Session, commerce: Commerce) -> None:
    shell = _create_shell(session, commerce)
    _ship(session, commerce, shell.id, quantity=2)
    residual = packages_of(session, commerce)[1]

    _ship(session, commerce, residual.id, quantity=1, carrier="YTO", tracking_no="YT-1")

    order = order_of(session, commerce)
    assert order.fulfillment_status == FulfillmentStatus.SHIPPED.value


def test_shipping_never_changes_order_status(session: Session, commerce: Commerce) -> None:
    """Spec section 31 / REQ-ORD-003: the axes are independent.

    Asserted around *every* write the ship path makes, because the tempting "and mark
    the order shipped now" is a one-line mistake that no other test would catch.
    """
    order = order_of(session, commerce)
    assert order.order_status == OrderStatus.PROCESSING.value
    original_status = order.order_status

    shell = _create_shell(session, commerce)
    assert order_of(session, commerce).order_status == original_status

    _ship(session, commerce, shell.id, quantity=1)
    assert order_of(session, commerce).order_status == original_status

    residual = packages_of(session, commerce)[1]
    _ship(session, commerce, residual.id, quantity=2)
    order = order_of(session, commerce)

    assert order.fulfillment_status == FulfillmentStatus.SHIPPED.value
    assert order.order_status == original_status


def test_the_order_axis_is_never_regressed_from_delivered(session: Session, commerce: Commerce) -> None:
    """``DELIVERED`` is terminal: a later package must not take the order backwards.

    Reached by driving the order to ``DELIVERED`` (a Phase 5+ concern - the return flow
    or a carrier callback) and then shipping a second package. A reship is a real
    scenario, and telling the customer their delivered order is out for delivery again
    is worse than a stale status.
    """
    shell = _create_shell(session, commerce)
    _ship(session, commerce, shell.id, quantity=2)

    order = order_of(session, commerce)
    order.fulfillment_status = FulfillmentStatus.DELIVERED.value
    session.commit()

    residual = packages_of(session, commerce)[1]
    _ship(session, commerce, residual.id, quantity=1)

    assert order_of(session, commerce).fulfillment_status == FulfillmentStatus.DELIVERED.value


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------
def test_shipping_an_already_shipped_package_is_refused(session: Session, commerce: Commerce) -> None:
    shell = _create_shell(session, commerce)
    _ship(session, commerce, shell.id, quantity=1)

    with pytest.raises(AppError) as caught:
        _ship(session, commerce, shell.id, quantity=1)

    assert int(caught.value.code) == int(ErrorCode.FULFILLMENT_ALREADY_SHIPPED)
    session.rollback()


def test_the_cumulative_cap_counts_only_shipped_packages(session: Session, commerce: Commerce) -> None:
    """The line is 3 units. Ship 3 in total, then any further unit is refused.

    **This is the test that catches a guard reading planned units.** A guard that sums
    ``fulfillment_items`` over *every* package counts the ``UNFULFILLED`` shell's plan
    as shipped, so on a fresh order it believes the order is already fully shipped and
    refuses the first legal request. If that regressed, this test fails on the first
    ``_ship`` call with ``FULFILLMENT_QUANTITY_EXCEEDS_ORDER``.
    """
    shell = _create_shell(session, commerce)
    _ship(session, commerce, shell.id, quantity=2)

    residual = packages_of(session, commerce)[1]
    # 2 already shipped + 1 == the ordered 3: exactly at the cap, so allowed.
    _ship(session, commerce, residual.id, quantity=1)
    assert order_of(session, commerce).fulfillment_status == FulfillmentStatus.SHIPPED.value

    # A further package (a reship) offering one more unit must be refused.
    order = order_of(session, commerce)
    items = list(session.scalars(select(OrderItem).where(OrderItem.order_id == commerce.order_id)))
    extra = FulfillmentService(session).create_shell(
        order=order, items=items, warehouse_id=commerce.warehouse_id
    )
    session.commit()

    with pytest.raises(AppError) as caught:
        _ship(session, commerce, extra.id, quantity=1)

    assert int(caught.value.code) == int(ErrorCode.FULFILLMENT_QUANTITY_EXCEEDS_ORDER)
    assert "3" in caught.value.public_message
    session.rollback()


def test_shipping_more_than_the_package_holds_is_refused(session: Session, commerce: Commerce) -> None:
    """The per-package half of 70 001: a 1-unit residual cannot ship 2."""
    shell = _create_shell(session, commerce)
    _ship(session, commerce, shell.id, quantity=2)
    residual = packages_of(session, commerce)[1]

    with pytest.raises(AppError) as caught:
        _ship(session, commerce, residual.id, quantity=2)

    assert int(caught.value.code) == int(ErrorCode.FULFILLMENT_QUANTITY_EXCEEDS_ORDER)
    session.rollback()


def test_a_consumer_cannot_ship(session: Session, commerce: Commerce) -> None:
    """Shipping is a staff action; a buyer who could ship could fake their own parcel."""
    shell = _create_shell(session, commerce)

    payload = ShipFulfillmentRequest(
        carrier="SF",
        tracking_no="NOPE",
        item_quantities=[{"order_item_id": commerce.order_item_id, "quantity": 1}],
    )
    with pytest.raises(AppError) as caught:
        FulfillmentService(session).ship(
            principal=commerce.consumer, fulfillment_id=shell.id, payload=payload
        )

    assert int(caught.value.code) == int(ErrorCode.INSUFFICIENT_PERMISSION)
    session.rollback()


def test_another_tenants_package_is_invisible(session: Session, commerce: Commerce) -> None:
    """The merchant scope is applied in the query, so the row is never fetched.

    A permission error here would confirm the package exists; ``FULFILLMENT_NOT_FOUND``
    is the only answer that discloses nothing.
    """
    shell = _create_shell(session, commerce)

    payload = ShipFulfillmentRequest(
        carrier="SF",
        tracking_no="X",
        item_quantities=[{"order_item_id": commerce.order_item_id, "quantity": 1}],
    )
    with pytest.raises(AppError) as caught:
        FulfillmentService(session).ship(
            principal=commerce.other_merchant_staff,
            fulfillment_id=shell.id,
            payload=payload,
        )

    assert int(caught.value.code) == int(ErrorCode.FULFILLMENT_NOT_FOUND)
    # And nothing changed.
    session.rollback()
    assert packages_of(session, commerce)[0].fulfillment_status == "UNFULFILLED"


def test_an_unknown_package_is_not_found(session: Session, commerce: Commerce) -> None:
    payload = ShipFulfillmentRequest(
        carrier="SF",
        tracking_no="X",
        item_quantities=[{"order_item_id": commerce.order_item_id, "quantity": 1}],
    )
    with pytest.raises(AppError) as caught:
        FulfillmentService(session).ship(principal=commerce.staff, fulfillment_id=2**40, payload=payload)

    assert int(caught.value.code) == int(ErrorCode.FULFILLMENT_NOT_FOUND)
    session.rollback()


def test_a_failed_shipment_leaves_no_trace(session: Session, commerce: Commerce) -> None:
    """A refused ship must be a no-op, not a half-write.

    The guards run before the stamp and the status write, and the caller rolls back, so
    the package must still be an unshipped shell with its full plan and no carrier.
    """
    shell = _create_shell(session, commerce)

    with pytest.raises(AppError):
        _ship(session, commerce, shell.id, quantity=4)
    session.rollback()

    reloaded = session.get(Fulfillment, shell.id)
    assert reloaded is not None
    assert reloaded.fulfillment_status == FulfillmentStatus.UNFULFILLED.value
    assert reloaded.carrier is None
    assert reloaded.tracking_no is None
    assert reloaded.shipped_at is None
    assert [(i.order_item_id, i.quantity) for i in reloaded.items] == [(commerce.order_item_id, 3)]
    assert len(packages_of(session, commerce)) == 1


# ---------------------------------------------------------------------------
# The payment path's own invariant, asserted here because the shell is created here
# ---------------------------------------------------------------------------
def test_the_shell_records_the_orders_paid_state(session: Session, commerce: Commerce) -> None:
    """A sanity check on the fixture as much as the code: the order is PAID.

    Without this, a later assertion that shipping leaves ``payment_status`` alone would
    pass vacuously on an unpaid order.
    """
    order = order_of(session, commerce)
    assert order.payment_status == PaymentStatus.PAID.value
    assert order.paid_amount == order.payable_amount

    shell = _create_shell(session, commerce)
    _ship(session, commerce, shell.id, quantity=3)

    order = order_of(session, commerce)
    assert order.payment_status == PaymentStatus.PAID.value
    assert order.fulfillment_status == FulfillmentStatus.SHIPPED.value
