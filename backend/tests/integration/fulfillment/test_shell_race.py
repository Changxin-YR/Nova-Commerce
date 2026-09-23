"""Concurrent ``create_shell``: exactly one shell, with **no** caller-side lock.

## The defect this test pins

``create_shell``'s duplicate guard is check-then-act - "is there an unshipped shell?"
and then "insert one" - and no constraint covers the gap:

* a partial unique index (one ``UNFULFILLED`` row per order) is not expressible in
  MySQL 8;
* ``uq_fulfillments_merchant_fulfillment_no`` cannot help, because two racing inserts
  carry *different* ``fulfillment_no`` values, so both satisfy it.

The guard was previously serialised only by the payment workflow's
``SELECT ... FOR UPDATE`` on the order row - a lock taken in **another module**. That
is a real guarantee for the settlement path (FG-11 exercises it 10-way), but it is an
invisible precondition: any caller that did not happen to lock the order first would
silently create duplicate packages and nothing would reject them.

**Measured before the fix: 8 concurrent calls produced 8 shells and 8 item rows for
one order.** The fix is a ``SELECT ... FOR UPDATE`` on the order row *inside*
``create_shell``, before the guard, which makes the guarantee this module's own rather
than its caller's discipline.

## Why there is no caller-side lock here, deliberately

Every worker calls the service directly, exactly as a future admin repair path, re-ship
tool or data migration would. Holding the order lock in the test would reproduce the
payment path's conditions and prove nothing about the precondition this test exists to
remove. That asymmetry is the whole point: this test fails on the unfixed code and
passes on the fixed code.

Real MySQL, real threads, one session per thread - the FG-09/FG-11 precedent. A single
session looping would be serial by construction and would pass either way.
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from app.modules.fulfillment.models import Fulfillment
from app.modules.fulfillment.service import FulfillmentService
from app.modules.order.models import Order, OrderItem
from app.shared.db.session import get_session_factory
from tests.integration.fulfillment.conftest import Commerce

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

#: Enough racers to make a lost update overwhelmingly likely on unfixed code (it was
#: 8/8 shells), while keeping the serialised critical section short enough that the
#: lock queue drains well inside MySQL's 50s innodb_lock_wait_timeout.
CONCURRENCY = 8


def test_concurrent_create_shell_yields_exactly_one_shell(session, commerce: Commerce) -> None:
    factory = get_session_factory()
    start = threading.Barrier(CONCURRENCY)
    outcomes: list[object] = []
    lock = threading.Lock()

    def call() -> None:
        with factory() as worker:
            # NOTE: no with_for_update on the order here - that is the point.
            order = worker.get(Order, commerce.order_id)
            assert order is not None
            items = list(worker.scalars(select(OrderItem).where(OrderItem.order_id == commerce.order_id)))
            try:
                start.wait(timeout=30)
                shell = FulfillmentService(worker).create_shell(
                    order=order, items=items, warehouse_id=commerce.warehouse_id
                )
                worker.commit()
                result: object = shell.id
            except Exception as exc:  # noqa: BLE001 - a refused race is a real outcome
                worker.rollback()
                result = f"ERR {type(exc).__name__}: {exc}"
            with lock:
                outcomes.append(result)

    threads = [threading.Thread(target=call) for _ in range(CONCURRENCY)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=90)

    session.expire_all()
    shells = list(session.scalars(select(Fulfillment).where(Fulfillment.order_id == commerce.order_id)))

    assert len(shells) == 1, (
        f"exactly one shell must exist for the order, found {len(shells)}: "
        f"{[(s.fulfillment_no, s.fulfillment_status) for s in shells]} "
        f"(outcomes={outcomes})"
    )

    # Every caller must have been given the SAME row - a second distinct id would mean
    # a caller observed a shell it did not create.
    ids = {str(o) for o in outcomes}
    assert len(ids) == 1, f"callers disagreed about the shell: {outcomes}"
    assert len(outcomes) == CONCURRENCY, "a worker never reported"

    # And the one shell is still the record of what is owed, not a half-write.
    assert shells[0].fulfillment_status == "UNFULFILLED"
    assert shells[0].carrier is None
    assert len(shells[0].items) == 1
