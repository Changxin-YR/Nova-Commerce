"""The two frozen reads plus the serializer, against real MySQL.

``API_CONTRACT`` section 5 freezes the fulfillment object and section 5.2 the console
queue. These tests assert the **wire shape a client actually receives**, not merely
that the ORM row is correct: a serializer that drops a field, renames
``fulfillment_status`` or forgets ``items[]`` is an integration defect that no service
test would catch.

The ``sku_id`` case is worth watching. ``fulfillment_items`` deliberately does not
store it (a package line whose ``sku_id`` disagreed with the order line it belongs to
would be an undetectable shipping error), while the frozen shape requires it - so the
serializer resolves it through the order line. The test below is what keeps that
resolution honest.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import AppError, ErrorCode
from app.modules.fulfillment.schemas import ShipFulfillmentRequest
from app.modules.fulfillment.serializers import to_fulfillment, to_page
from app.modules.fulfillment.service import FulfillmentService
from app.modules.order.enums import FulfillmentStatus
from app.modules.order.models import OrderItem
from tests.integration.fulfillment.conftest import (
    Commerce,
    order_of,
)

pytestmark = pytest.mark.integration


def _shell(session, commerce: Commerce):
    order = order_of(session, commerce)
    items = list(session.scalars(select(OrderItem).where(OrderItem.order_id == commerce.order_id)))
    shell = FulfillmentService(session).create_shell(
        order=order, items=items, warehouse_id=commerce.warehouse_id
    )
    session.commit()
    return shell


def _ship(session, commerce: Commerce, fulfillment_id: int, quantity: int):
    payload = ShipFulfillmentRequest(
        carrier="JD",
        tracking_no=f"JD-{commerce.marker}",
        item_quantities=[{"order_item_id": commerce.order_item_id, "quantity": quantity}],
    )
    fulfillment = FulfillmentService(session).ship(
        principal=commerce.staff, fulfillment_id=fulfillment_id, payload=payload
    )
    session.commit()
    return fulfillment


# ---------------------------------------------------------------------------
# The frozen shape
# ---------------------------------------------------------------------------
def test_the_unshipped_serialised_shape_is_the_frozen_one(session, commerce: Commerce) -> None:
    shell = _shell(session, commerce)
    service = FulfillmentService(session)
    wire = to_fulfillment(shell, service.sku_by_line_for_orders([commerce.order_id]))

    assert set(wire.model_dump()) == {
        "id",
        "order_id",
        "order_no",
        "fulfillment_no",
        "fulfillment_status",
        "carrier",
        "tracking_no",
        "shipped_at",
        "delivered_at",
        "created_at",
        "items",
    }
    assert wire.fulfillment_status == "UNFULFILLED"
    # Null, not "" - the console distinguishes "no tracking number" from a blank one.
    assert wire.carrier is None
    assert wire.tracking_no is None
    assert wire.shipped_at is None
    assert wire.delivered_at is None

    assert len(wire.items) == 1
    line = wire.items[0]
    assert set(line.model_dump()) == {
        "id",
        "order_item_id",
        "sku_id",
        "product_name",
        "sku_name",
        "quantity",
    }
    assert line.order_item_id == commerce.order_item_id
    # Resolved through the order line, because the table does not store it.
    assert line.sku_id == commerce.sku_id
    assert line.product_name == "Nova Phone 15 Pro"
    assert line.quantity == 3


def test_the_serialised_timestamp_is_the_frozen_encoding(session, commerce: Commerce) -> None:
    """``API_CONTRACT`` section 2: UTC, milliseconds, literal ``Z``."""
    shell = _shell(session, commerce)
    service = FulfillmentService(session)
    wire = to_fulfillment(shell, service.sku_by_line_for_orders([commerce.order_id]))
    created = wire.model_dump(mode="json")["created_at"]

    assert created.endswith("Z"), created
    assert created[19] == ".", created
    assert len(created) == 24, created  # 2026-09-22T23:31:07.507Z


def test_the_shipped_serialised_shape_carries_the_parcel_facts(session, commerce: Commerce) -> None:
    shell = _shell(session, commerce)
    shipped = _ship(session, commerce, shell.id, quantity=3)
    service = FulfillmentService(session)
    wire = to_fulfillment(shipped, service.sku_by_line_for_orders([commerce.order_id]))

    assert wire.carrier == "JD"
    assert wire.tracking_no == f"JD-{commerce.marker}"
    assert wire.shipped_at is not None
    assert wire.fulfillment_status == "SHIPPED"


# ---------------------------------------------------------------------------
# The consumer read
# ---------------------------------------------------------------------------
def test_a_consumer_sees_their_own_orders_shipments(session, commerce: Commerce) -> None:
    shell = _shell(session, commerce)
    _ship(session, commerce, shell.id, quantity=1)

    rows = FulfillmentService(session).get_for_order_no(
        principal=commerce.consumer, order_no=commerce.order_no
    )

    assert len(rows) == 2  # the shipped package and its residual
    assert all(row.order_no == commerce.order_no for row in rows)


def test_a_consumer_cannot_read_another_persons_order(session, commerce: Commerce) -> None:
    """Answers ``ORDER_NOT_FOUND``, so a guessed order number discloses nothing."""
    from app.modules.identity.enums import DataScope, UserType
    from app.modules.identity.service import Principal

    stranger = Principal(
        user_id=commerce.buyer_id + 10_000_000,
        user_type=UserType.CONSUMER.value,
        merchant_id=None,
        roles=(),
        permissions=frozenset(),
        data_scope=DataScope.SELF,
        session_id="stranger",
        is_staff=False,
    )

    with pytest.raises(AppError) as caught:
        FulfillmentService(session).get_for_order_no(principal=stranger, order_no=commerce.order_no)

    assert int(caught.value.code) == int(ErrorCode.ORDER_NOT_FOUND)
    session.rollback()


def test_an_order_with_no_packages_returns_an_empty_list(session, commerce: Commerce) -> None:
    """Section 6 explicitly allows ``shipments: []`` - an unpacked order has none."""
    rows = FulfillmentService(session).get_for_order_no(
        principal=commerce.consumer, order_no=commerce.order_no
    )
    assert rows == []


# ---------------------------------------------------------------------------
# The console queue
# ---------------------------------------------------------------------------
def test_the_admin_queue_is_paged_and_enveloped(session, commerce: Commerce) -> None:
    _shell(session, commerce)

    page = FulfillmentService(session).list_admin_fulfillments(principal=commerce.staff, page=1, page_size=20)

    assert page.total >= 1
    wire = to_page(
        page.rows,
        page=1,
        page_size=20,
        total=page.total,
        sku_by_line=FulfillmentService(session).sku_by_line_for_orders([row.order_id for row in page.rows]),
    )
    dumped = wire.model_dump(mode="json")
    assert set(dumped) == {"items", "meta"}
    assert set(dumped["meta"]) == {"page", "page_size", "total", "total_pages"}
    assert dumped["meta"]["page"] == 1
    assert dumped["meta"]["total"] == page.total
    assert any(row["order_no"] == commerce.order_no for row in dumped["items"])


def test_the_queue_filters_by_order_no(session, commerce: Commerce) -> None:
    _shell(session, commerce)

    page = FulfillmentService(session).list_admin_fulfillments(
        principal=commerce.staff, order_no=commerce.order_no
    )

    assert page.total == 1
    assert page.rows[0].order_no == commerce.order_no


def test_the_queue_filters_by_status(session, commerce: Commerce) -> None:
    shell = _shell(session, commerce)
    _ship(session, commerce, shell.id, quantity=3)

    service = FulfillmentService(session)
    shipped = service.list_admin_fulfillments(
        principal=commerce.staff,
        order_no=commerce.order_no,
        fulfillment_status=FulfillmentStatus.SHIPPED.value,
    )
    unfulfilled = service.list_admin_fulfillments(
        principal=commerce.staff,
        order_no=commerce.order_no,
        fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
    )

    assert shipped.total == 1
    assert shipped.rows[0].fulfillment_status == FulfillmentStatus.SHIPPED.value
    assert unfulfilled.total == 0, "a fully shipped order leaves no unshipped package"


def test_an_unknown_status_filter_is_a_validation_error(session, commerce: Commerce) -> None:
    """An unmeetable filter must not look like an empty queue.

    An operator who sees an empty list concludes there is nothing to ship and acts on
    that conclusion.
    """
    with pytest.raises(AppError) as caught:
        FulfillmentService(session).list_admin_fulfillments(
            principal=commerce.staff, fulfillment_status="NOT_A_STATUS"
        )

    assert int(caught.value.code) == int(ErrorCode.VALIDATION_ERROR)
    session.rollback()


def test_the_queue_is_scoped_to_the_operators_merchant(session, commerce: Commerce) -> None:
    _shell(session, commerce)

    page = FulfillmentService(session).list_admin_fulfillments(
        principal=commerce.other_merchant_staff, order_no=commerce.order_no
    )

    assert page.total == 0
    assert page.rows == []


def test_a_consumer_cannot_read_the_queue(session, commerce: Commerce) -> None:
    with pytest.raises(AppError) as caught:
        FulfillmentService(session).list_admin_fulfillments(principal=commerce.consumer)

    assert int(caught.value.code) == int(ErrorCode.INSUFFICIENT_PERMISSION)
    session.rollback()


# ---------------------------------------------------------------------------
# The repository total the guard used to trust
# ---------------------------------------------------------------------------
def test_the_guards_shipped_total_counts_only_shipped_packages(session, commerce: Commerce) -> None:
    """What the 70001 guard reads must count **shipped** units, never the shell's plan.

    This began as a characterisation test pinning a defect: the repository's
    ``shipped_quantities_for_order`` summed every package with no status filter, so on a
    fresh order it returned the ``UNFULFILLED`` shell's *planned* units as though they
    had gone out. The cumulative guard, reading that number, computed ``3 + 2 > 3`` and
    refused the order's **first legal shipment** with 70001 - so the ship endpoint could
    never be used, while every service-level test stayed green.

    ``FulfillmentService`` carries its own filtered total (``_shipped_quantities``) as
    the workaround, retired only once the repository's filter is committed. This test
    therefore asserts the invariant - the guard must see goods, not plans - and proves it
    through behaviour as well as through the number: the first legal shipment of a
    3-unit line must **succeed**, which is only possible if the shell's 3 planned units
    were not counted as shipped.

    It is the phase's most likely silent double-count (a shell and the shipments it later
    becomes coexist on one order), and the reason the workaround must not be retired
    without the repository filter in place.
    """
    shell = _shell(session, commerce)
    service = FulfillmentService(session)

    assert service._shipped_quantities(order_of(session, commerce)) == {}, (
        "nothing has actually shipped yet - counting the unshipped shell would make the "
        "70001 guard refuse the order's first legal shipment"
    )

    # The order's first legal shipment must succeed, not raise 70001.
    _ship(session, commerce, shell.id, quantity=2)
    session.expire_all()

    assert service._shipped_quantities(order_of(session, commerce)) == {commerce.order_item_id: 2}, (
        "the total follows the goods, not the plan"
    )
    assert order_of(session, commerce).fulfillment_status == (FulfillmentStatus.PARTIAL_SHIPPED.value)


def test_the_shipped_total_is_reused_by_the_axis_and_the_guard(session, commerce: Commerce) -> None:
    """One implementation of "how much went out", asserted where it is observable.

    The guard and the axis must agree, or an order can read ``SHIPPED`` while its last
    shipment was refused.
    """
    shell = _shell(session, commerce)
    _ship(session, commerce, shell.id, quantity=2)

    service = FulfillmentService(session)
    order = order_of(session, commerce)
    shipped = service._shipped_quantities(order)
    axis = service._recompute_order_axis(order)

    assert shipped == {commerce.order_item_id: 2}
    assert axis == FulfillmentStatus.PARTIAL_SHIPPED.value
