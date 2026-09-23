"""Adversarial probes of the fulfillment quantity guard (FG-12 defence in depth).

`PHASE5_DESIGN` section 5.4 and REQ-FUL-002: *the sum of shipped quantities per
``order_item_id`` across **all** fulfillments of an order must never exceed that line's
``quantity``* (``FULFILLMENT_QUANTITY_EXCEEDS_ORDER``, 70001). Cumulative across packages -
a per-package check is not enough, and that is the defect this rule exists for.

## Why this file exists separately from the other fulfillment tests

The committed tests cover the *repository* aggregate (`shipped_quantities_for_order` versus
`planned_quantities_for_order`) and one partial shipment through the service. Neither covers
the two cases that actually broke, both of which are **service-level** claims:

1. **The first shipment at full width.** The original defect summed every
   ``fulfillment_items.quantity`` with no status filter, so a fresh shell's *planned* units
   were reported as shipped and the guard computed ``planned + requested > ordered``. The
   committed partial test ships 2 of 3 - which is not the case that broke. Shipping the
   **full** 3 as the order's first shipment is the request that was refused, and it is the
   one a fresh order's operator actually makes.
2. **Many packages.** Shipping the same line in several parcels must be bounded by the
   order-wide cumulative total, not by one package's lines. A per-package check passes every
   row while over-shipping the line.

This is not a re-run of anything: it is the guard exercised at its boundary, through the
service, on orders the real settlement path produced.

## Deliberately not a database-boundary control

There is no `CHECK` under the 70001 rule and there cannot be one - it is a sum across
fulfillments of an order, and a single-row constraint cannot express it. So unlike the
refund caps, a defect here produces no database error: the only enforcement is the service
guard. That is exactly why it needs its own adversarial probe.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import text

# The `shop` fixture is imported rather than inherited: `tests/integration/commerce/conftest.py`
# registers it inside its own package, and `seed.py` is the module that defines it, so an
# explicit import is both correct and unambiguous (PHASE5_DESIGN section 12: one seed).
# `shop` and `engine` are imported under aliases and re-exported below. A direct import
# would collide with the `shop: Shop` parameter every test below declares (ruff F811), and
# the alias keeps the two roles - fixture and type - visibly distinct.
from tests.integration.commerce.seed import (
    Shop,
    engine as _engine_fixture,
    items_of,
    paid_order,
    shop as _shop_fixture,
)

from app.core.errors import ErrorCode, FulfillmentQuantityError
from app.modules.fulfillment.repository import FulfillmentRepository
from app.modules.fulfillment.schemas import ShipFulfillmentRequest, ShipLineIn
from app.modules.fulfillment.service import FulfillmentService
from app.modules.identity.enums import PermissionCode
from app.modules.order.workflow import OrderLineInput
from app.shared.db.session import get_session_factory

#: pytest resolves a fixture by the name it is bound to in this module, so the aliases are
#: re-exported under their canonical names. Both are the shared seed's own objects - not
#: second definitions of them (PHASE5_DESIGN section 12).
engine = _engine_fixture
shop = _shop_fixture

pytestmark = [pytest.mark.integration]

ORDERED_QUANTITY = 3


def _shipper(shop: Shop):
    """``shop.staff`` plus ``fulfillment:ship``.

    Built rather than widened in the shared seed on purpose: a fixture that grants every
    permission makes an authorization test incapable of failing.
    """
    return replace(
        shop.staff,
        permissions=frozenset(
            {PermissionCode.FULFILLMENT_SHIP.value, PermissionCode.ORDER_READ.value}
        ),
    )


def _fresh_order(shop: Shop, suffix: str):
    order = paid_order(shop, lines=[OrderLineInput(shop.sku_ids[0], ORDERED_QUANTITY)], suffix=suffix)
    session = get_session_factory()()
    try:
        order_item_id = items_of(session, order_id=order.id)[0].id
        package_id = FulfillmentRepository(session).list_for_order(order.id)[0].id
    finally:
        session.close()
    return order, order_item_id, package_id


def _ship(shop: Shop, *, fulfillment_id: int, order_item_id: int, quantity: int, tracking: str):
    session = get_session_factory()()
    try:
        result = FulfillmentService(session).ship(
            principal=_shipper(shop),
            fulfillment_id=fulfillment_id,
            payload=ShipFulfillmentRequest(
                carrier="SF",
                tracking_no=tracking,
                item_quantities=[ShipLineIn(order_item_id=order_item_id, quantity=quantity)],
            ),
        )
        session.commit()
        return result
    finally:
        session.close()


def test_the_first_shipment_may_ship_the_full_ordered_quantity(shop: Shop) -> None:
    """The order's **first** shipment, at full width, is legal.

    This is the exact request the original defect refused. The shell created by settlement
    carries the planned units (``ORDERED_QUANTITY`` of them); if those are counted as
    shipped, the guard computes ``3 + 3 > 3`` and refuses a shipment of the whole order -
    so `POST /fulfillments/{id}/ship` could never be used on a fresh order at all.

    The weak version of this test would ship 2 (what the committed test does) and pass
    against the broken code in the common case, because ``3 + 2 > 3`` also holds - but the
    failure would then be attributed to the quantity rather than to the plan, which is why
    the boundary case is the full width.
    """
    order, order_item_id, package_id = _fresh_order(shop, "v-full")

    # Precondition, asserted rather than assumed: one shell carrying the full plan and
    # nothing shipped yet. Without this the test could pass for an unrelated reason.
    session = get_session_factory()()
    try:
        repository = FulfillmentRepository(session)
        assert repository.planned_quantities_for_order(order.id) == {
            order_item_id: ORDERED_QUANTITY
        }
        assert repository.shipped_quantities_for_order(order.id) == {}, (
            "a fresh shell must ship nothing, or this probe is not testing the real state"
        )
    finally:
        session.close()

    _ship(
        shop,
        fulfillment_id=package_id,
        order_item_id=order_item_id,
        quantity=ORDERED_QUANTITY,
        tracking=f"SF{shop.marker}-full",
    )

    # Read the axis back on a FRESH session (HANDOFF section 6: a re-read on the writing
    # connection can compare a snapshot against itself).
    session = get_session_factory()()
    try:
        row = session.execute(
            text("SELECT fulfillment_status FROM orders WHERE id = :oid"), {"oid": order.id}
        ).scalar_one()
        assert row == "SHIPPED", f"a fully shipped order reads {row!r}, not SHIPPED"
        assert FulfillmentRepository(session).shipped_quantities_for_order(order.id) == {
            order_item_id: ORDERED_QUANTITY
        }
    finally:
        session.close()


def test_shipping_across_two_packages_cannot_exceed_the_ordered_quantity(shop: Shop) -> None:
    """The cumulative rule: 2 out in one parcel then 2 more must be refused.

    A per-package check would accept this - each parcel's own lines are within the package -
    and the line would end up over-shipped with every individual row looking valid. The
    refusal must be ``FULFILLMENT_QUANTITY_EXCEEDS_ORDER`` (70001) and it must leave the
    order's shipped total at exactly what really left.
    """
    order, order_item_id, package_id = _fresh_order(shop, "v-cum")

    # First parcel: 2 of the 3. Legal, and it leaves a residual package for the last unit.
    _ship(
        shop,
        fulfillment_id=package_id,
        order_item_id=order_item_id,
        quantity=2,
        tracking=f"SF{shop.marker}-cum1",
    )

    session = get_session_factory()()
    try:
        repository = FulfillmentRepository(session)
        assert repository.shipped_quantities_for_order(order.id) == {order_item_id: 2}
        axis = session.execute(
            text("SELECT fulfillment_status FROM orders WHERE id = :oid"), {"oid": order.id}
        ).scalar_one()
        assert axis == "PARTIAL_SHIPPED", f"2 of 3 shipped must read PARTIAL_SHIPPED, got {axis!r}"
        # The residual carries the last unit; it is a new package, so shipping more than
        # one unit is what must be refused.
        residual = [
            package
            for package in repository.list_for_order(order.id)
            if package.fulfillment_status == "UNFULFILLED"
        ]
        assert len(residual) == 1, "the held-back unit must survive in a residual package"
        residual_id = residual[0].id
    finally:
        session.close()

    # Second parcel: 2 more against a line that has only 1 left. Refused.
    with pytest.raises(FulfillmentQuantityError) as caught:
        _ship(
            shop,
            fulfillment_id=residual_id,
            order_item_id=order_item_id,
            quantity=2,
            tracking=f"SF{shop.marker}-cum2",
        )
    assert caught.value.code == ErrorCode.FULFILLMENT_QUANTITY_EXCEEDS_ORDER, caught.value.code

    # And nothing partial survived: still 2 out, axis still PARTIAL_SHIPPED.
    session = get_session_factory()()
    try:
        repository = FulfillmentRepository(session)
        assert repository.shipped_quantities_for_order(order.id) == {order_item_id: 2}, (
            "the refused shipment changed the shipped total"
        )
        axis = session.execute(
            text("SELECT fulfillment_status FROM orders WHERE id = :oid"), {"oid": order.id}
        ).scalar_one()
        assert axis == "PARTIAL_SHIPPED", f"the refused shipment moved the axis to {axis!r}"
    finally:
        session.close()

    # Finally the last legal unit: exactly at the cap, accepted. This is the positive
    # control that makes the refusal above meaningful rather than "it always refuses".
    _ship(
        shop,
        fulfillment_id=residual_id,
        order_item_id=order_item_id,
        quantity=1,
        tracking=f"SF{shop.marker}-cum3",
    )
    session = get_session_factory()()
    try:
        assert FulfillmentRepository(session).shipped_quantities_for_order(order.id) == {
            order_item_id: ORDERED_QUANTITY
        }
        axis = session.execute(
            text("SELECT fulfillment_status FROM orders WHERE id = :oid"), {"oid": order.id}
        ).scalar_one()
        assert axis == "SHIPPED", f"3 of 3 shipped must read SHIPPED, got {axis!r}"
    finally:
        session.close()
