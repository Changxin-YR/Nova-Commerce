"""Duplicate an in-flight shipment: two operators shipping one package at once.

The scenario is mundane and the consequence is not: a warehouse with two consoles, or
one operator who double-clicked, issues two ``POST /fulfillments/{id}/ship`` for the
same package. Both requests read the row, both find it ``UNFULFILLED``, both decide
"ship it" - and unless the decision is taken **inside the row lock**, both write a
shipment, both create a residual package, and the order's axis is recomputed from a
state neither request believed in.

``FulfillmentRepository.get_for_update`` is that lock. This test is what makes it a
claim rather than a comment: with the lock removed, both sessions pass the guard and
the "exactly one SHIPPED package" assertion fails.

Real MySQL, real threads, separate sessions - the FG-11 precedent. A single session
looping twice proves nothing, because a session is serial by definition.

## What the loser is allowed to see

``FULFILLMENT_ALREADY_SHIPPED`` (70 003) is the *desired* answer and the one a real
double-click gets: it waits on the lock, finds the row shipped, and refuses. Under
contention the loser can instead fail with a lock error, which is a legitimate
transport outcome and is asserted as such rather than swallowed - the invariant under
test is "exactly one shipment", not "the loser always sees one particular code".
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from app.core.errors import AppError, ErrorCode
from app.modules.fulfillment.schemas import ShipFulfillmentRequest
from app.modules.fulfillment.service import FulfillmentService
from app.modules.order.enums import FulfillmentStatus
from app.modules.order.models import OrderItem
from app.shared.db.session import get_session_factory
from tests.integration.fulfillment.conftest import Commerce, order_of, packages_of

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

#: How many consoles race for one package.
CONCURRENCY = 10


def test_two_concurrent_shipments_produce_exactly_one_package(session, commerce: Commerce) -> None:
    order = order_of(session, commerce)
    items = list(session.scalars(select(OrderItem).where(OrderItem.order_id == commerce.order_id)))
    shell = FulfillmentService(session).create_shell(
        order=order, items=items, warehouse_id=commerce.warehouse_id
    )
    session.commit()
    shell_id = shell.id

    factory = get_session_factory()
    start = threading.Barrier(CONCURRENCY)
    outcomes: list[tuple[str, int | None]] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        # A fresh session per thread: a shared session is serial, so it would test
        # nothing about concurrency (the FG-09/FG-11 lesson).
        with factory() as worker:
            payload = ShipFulfillmentRequest(
                carrier="SF",
                tracking_no=f"RACE-{commerce.marker}-{index}",
                item_quantities=[{"order_item_id": commerce.order_item_id, "quantity": 1}],
            )
            try:
                start.wait(timeout=20)
                FulfillmentService(worker).ship(
                    principal=commerce.staff, fulfillment_id=shell_id, payload=payload
                )
                worker.commit()
                result = ("ok", None)
            except AppError as exc:
                worker.rollback()
                result = ("app_error", int(exc.code))
            except Exception as exc:  # noqa: BLE001 - a lost race can surface as a driver error
                worker.rollback()
                result = ("driver_error", type(exc).__name__)
            with lock:
                outcomes.append(result)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(CONCURRENCY)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(outcomes) == CONCURRENCY, "a worker never reported"

    winners = [o for o in outcomes if o[0] == "ok"]
    losers = [o for o in outcomes if o[0] != "ok"]

    assert len(winners) == 1, f"exactly one shipment must win, saw {outcomes}"
    # The losers must be refusals, and specifically the state guard's answer wherever
    # the lock let them read the row again.
    for kind, detail in losers:
        assert kind in {"app_error", "driver_error"}, outcomes
        if kind == "app_error":
            assert detail == int(ErrorCode.FULFILLMENT_ALREADY_SHIPPED), outcomes

    session.expire_all()
    packages = packages_of(session, commerce)

    shipped_packages = [p for p in packages if p.fulfillment_status == FulfillmentStatus.SHIPPED.value]
    assert len(shipped_packages) == 1, [(p.fulfillment_no, p.fulfillment_status) for p in packages]

    # Exactly the one unit that won left, and the rest is still owed.
    shipped_units = sum(i.quantity for i in shipped_packages[0].items)
    assert shipped_units == 1

    unshipped = [p for p in packages if p.fulfillment_status == FulfillmentStatus.UNFULFILLED.value]
    assert len(unshipped) == 1, "one residual, not one per racing loser"
    assert sum(i.quantity for i in unshipped[0].items) == 2

    order = order_of(session, commerce)
    assert order.fulfillment_status == FulfillmentStatus.PARTIAL_SHIPPED.value
    # And the axis that must never move did not.
    assert order.order_status == "PROCESSING"
