"""Regression test for a defect in `FulfillmentRepository` found by another writer.

`fulfillment-workflow` found this while writing the ship path, not me, and it is
worth a committed test precisely because no test of mine caught it: the method was
named `shipped_*` and returned *planned* units, and a caller has no reason to doubt
a method whose name says what it does.

The bug, concretely: `PaymentSuccessWorkflow` step 8 creates a shell in
`UNFULFILLED` carrying the order's planned units (one line per order line, full
quantity). The original aggregate summed `fulfillment_items.quantity` with no status
filter, so on a fresh order it reported the plan as shipped. The cumulative guard of
design section 5.4 (`FULFILLMENT_QUANTITY_EXCEEDS_ORDER`, 70001) then computed
`planned + requested > ordered` and refused a perfectly legal shipment - so with a
full-width shell the **first** shipment of any line was refused and
`POST /fulfillments/{id}/ship` could never be used at all.

This test is written against `planned_order()` from the shared seed, which drives the
real settlement path, so the shell under test is the one the product actually
creates rather than one hand-built to match the assumption being tested.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.modules.fulfillment.repository import FulfillmentRepository
from app.modules.fulfillment.schemas import ShipFulfillmentRequest, ShipLineIn
from app.modules.fulfillment.service import FulfillmentService
from app.modules.identity.enums import PermissionCode
from app.modules.order.workflow import OrderLineInput
from app.shared.db.session import get_session_factory

from .seed import Shop, items_of, paid_order

pytestmark = [pytest.mark.integration]


def test_a_fresh_shell_counts_as_planned_and_not_as_shipped(shop: Shop) -> None:
    """The regression: an UNFULFILLED shell must contribute **0** shipped units.

    Three assertions, because the bug is only fully pinned by all three:

    1. `planned_quantities_for_order` reports the full plan (the shell exists);
    2. `shipped_quantities_for_order` reports **nothing** (nobody shipped it);
    3. the two disagree - which is the property that was broken. A test asserting only
       (2) would pass just as well against a method that returned an empty dict for
       the wrong reason.
    """
    order = paid_order(shop, lines=[OrderLineInput(shop.sku_ids[0], 3)], suffix="ship-reg")

    session = get_session_factory()()
    try:
        items = items_of(session, order_id=order.id)
        assert len(items) == 1, "the seed should produce exactly one order line"
        order_item_id = items[0].id

        # The shell really is there, so a zero result below cannot be an empty-table artefact.
        packages = FulfillmentRepository(session).count_for_order(order.id)
        assert packages == 1

        planned = FulfillmentRepository(session).planned_quantities_for_order(order.id)
        shipped = FulfillmentRepository(session).shipped_quantities_for_order(order.id)

        assert planned == {order_item_id: 3}, "the shell must carry the full plan"
        assert shipped == {}, "an unshipped shell ships nothing"
        assert planned != shipped, "planned and shipped must not be the same question"

        per_line = FulfillmentRepository(session).shipped_quantity_for_order_item(order_item_id)
        assert per_line == 0
    finally:
        session.close()


def test_shipping_moves_units_from_planned_to_shipped(shop: Shop) -> None:
    """The other half: once a package has shipped, the units *do* count.

    Without this, the fix could be "return zero always" and the regression test above
    would still pass - so this is the positive control the negative one needs.
    """
    order = paid_order(shop, lines=[OrderLineInput(shop.sku_ids[0], 3)], suffix="ship-pos")

    session = get_session_factory()()
    try:
        order_item_id = items_of(session, order_id=order.id)[0].id
        repository = FulfillmentRepository(session)

        package = repository.list_for_order(order.id)[0]
        FulfillmentService(session).ship(
            # `shop.staff` holds only `order:read`; shipping needs
            # `fulfillment:ship` (or `order:admin`). A principal is built here rather
            # than widening the shared seed's staff, because a fixture that grants
            # every permission makes an authorization test unable to fail.
            principal=replace(
                shop.staff,
                permissions=frozenset(
                    {PermissionCode.FULFILLMENT_SHIP.value, PermissionCode.ORDER_READ.value}
                ),
            ),
            fulfillment_id=package.id,
            payload=ShipFulfillmentRequest(
                carrier="SF",
                tracking_no=f"SF{shop.marker}",
                item_quantities=[ShipLineIn(order_item_id=order_item_id, quantity=2)],
            ),
        )
        session.commit()

        # Read back through a fresh session: HANDOFF section 6 records that comparing
        # a post-mutation row against a REPEATABLE READ snapshot passes as a false green.
        session.close()
        session = get_session_factory()()
        repository = FulfillmentRepository(session)

        shipped = repository.shipped_quantities_for_order(order.id)
        planned = repository.planned_quantities_for_order(order.id)

        assert shipped.get(order_item_id, 0) == 2, "two units went out"
        assert repository.shipped_quantity_for_order_item(order_item_id) == 2
        # The residual package holds the remaining unit as *planned*, and the shipped
        # package still contributes its 2 - so the plan total is unchanged at 3 while
        # shipped is 2. This is exactly the divergence the two methods exist to expose.
        assert planned.get(order_item_id, 0) == 3
    finally:
        session.close()


