"""FG-10 concurrency - N threads, one ``Idempotency-Key``, exactly one order.

Spec §48/§96, §112 INV-015, §113 (real MySQL; a mocked database does not count),
PHASE4_DESIGN §11.

The property under test is a **database** property: ``UNIQUE (scope,
idempotency_key)`` is what serialises concurrent creates, and it does not exist in a
mock. So this runs against real MySQL, with real threads and their own sessions.

## What it proves, beyond "the JSON looked the same"

A replay that returned the original order while quietly creating a second one and
locking a second unit of stock would pass any assertion made on the response alone.
The assertions here are therefore on the **rows**: exactly one ``orders`` row, exactly
one ``ORDER_LOCK`` movement, and ``available_qty`` down by exactly one basket.

## Why the losers are allowed three different outcomes

Under InnoDB, the losing transactions' ``INSERT`` into ``idempotency_records`` blocks
on the winner's unique-index lock until the winner commits. Once it does, the loser's
own read sees the winner's ``COMPLETED`` row and replays it. That is the common
outcome, and the best one. But the frozen contract also allows
``IDEMPOTENCY_REQUEST_IN_PROGRESS (10012)`` for a claim that is still in flight, and a
``10011`` is legitimate if two callers somehow disagree about the body. All three are
acceptable; what is *not* acceptable is a second order, a second reservation, or a raw
``IntegrityError`` escaping as a 500. The test asserts the invariants and records
which outcome each call took, rather than pinning one.
"""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import (
    IdempotencyInProgressError,
    IdempotencyPayloadMismatchError,
)
from app.modules.inventory.enums import MovementType
from app.modules.order.service import OrderService
from app.modules.order.workflow import OrderLineInput
from app.shared.db.session import get_session_factory

from .conftest import (
    OPENING_STOCK,
    Shop,
    movements_for,
    order_count_for,
    read_position,
)

pytestmark = [pytest.mark.concurrency, pytest.mark.integration]

#: Enough contention to interleave without turning the suite into a load test.
CONCURRENT_CALLERS = 8

#: Units per order. Kept small and specific so the assertion is exact ("down by 3",
#: not "down by at least something").
QUANTITY = 3


def _attempt(shop: Shop, key: str, barrier: threading.Barrier) -> str:
    """One create attempt in its own session. Returns the outcome as a label."""
    session = get_session_factory()()
    try:
        # Every thread waits here, so the calls collide rather than queueing up
        # behind each other's connection acquisition.
        barrier.wait(timeout=30)
        try:
            result = OrderService(session).create_order(
                principal=shop.consumer,
                items=[OrderLineInput(shop.sku_ids[0], QUANTITY)],
                address_id=shop.address_id,
                client_request_id=shop.client_request_id("race"),
                idempotency_key=key,
            )
        except IdempotencyInProgressError:
            session.rollback()
            return "in_progress_1012"
        except IdempotencyPayloadMismatchError:
            session.rollback()
            return "mismatch_10011"
        except IntegrityError:
            # A raw IntegrityError escaping the workflow would become a 500 for the
            # client, so it is counted as a distinct - and failing - outcome rather
            # than swallowed.
            session.rollback()
            return "raw_integrity_error"
        return "replayed" if result.replayed else "created"
    finally:
        session.close()


def test_concurrent_creates_with_one_key_make_exactly_one_order(shop: Shop) -> None:
    key = shop.key("race")
    barrier = threading.Barrier(CONCURRENT_CALLERS)

    with ThreadPoolExecutor(max_workers=CONCURRENT_CALLERS) as pool:
        outcomes = list(pool.map(lambda _index: _attempt(shop, key, barrier), range(CONCURRENT_CALLERS)))

    tally = Counter(outcomes)
    assert tally["raw_integrity_error"] == 0, f"a raw IntegrityError escaped: {tally}"
    # Exactly one caller created it; every other caller either replayed it or was told
    # to retry. Never a second create.
    assert tally["created"] == 1, f"expected exactly one create, got {tally}"
    assert tally["created"] + tally["replayed"] + tally["in_progress_1012"] == CONCURRENT_CALLERS

    session = get_session_factory()()
    try:
        # -- exactly one order row (INV-015) ----------------------------
        assert order_count_for(session, user_id=shop.consumer_id) == 1

        # -- exactly one reservation, and no extra stock movement (INV-003) --
        locks = [
            row
            for row in movements_for(session, sku_id=shop.sku_ids[0])
            if row.movement_type == MovementType.ORDER_LOCK.value
        ]
        assert len(locks) == 1, f"expected one ORDER_LOCK movement, got {len(locks)}"
        assert locks[0].idempotency_key == (
            f"order-lock:{shop.consumer_id}:{shop.client_request_id('race')}:{shop.sku_ids[0]}"
        )

        # -- and the balance moved exactly once -------------------------
        available, locked, _version = read_position(
            session, sku_id=shop.sku_ids[0], warehouse_id=shop.warehouse_id
        )
        assert available == OPENING_STOCK - QUANTITY
        assert locked == QUANTITY

        # -- the ledger still explains the balance (INV-007) ------------
        assert locks[0].before_available == OPENING_STOCK
        assert locks[0].after_available == OPENING_STOCK - QUANTITY
        assert locks[0].after_locked == QUANTITY
    finally:
        session.close()


def test_concurrent_creates_with_different_keys_make_one_order_each(shop: Shop) -> None:
    """The negative control, and the reason the test above means something.

    A single-reservation result could also be produced by a lock that serialises
    *everything*. This proves that distinct keys really do run concurrently: N
    different keys produce N orders and N reservations, so the uniqueness above is the
    key doing its job rather than a global mutex making the first test vacuous.
    """
    barrier = threading.Barrier(CONCURRENT_CALLERS)

    def attempt(index: int) -> str:
        session = get_session_factory()()
        try:
            barrier.wait(timeout=30)
            result = OrderService(session).create_order(
                principal=shop.consumer,
                items=[OrderLineInput(shop.sku_ids[2], 1)],
                address_id=shop.address_id,
                client_request_id=f"{shop.marker}-multi-{index}",
                idempotency_key=f"{shop.marker}-multi-{index}",
            )
            return "replayed" if result.replayed else "created"
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=CONCURRENT_CALLERS) as pool:
        outcomes = list(pool.map(attempt, range(CONCURRENT_CALLERS)))

    assert Counter(outcomes)["created"] == CONCURRENT_CALLERS, Counter(outcomes)

    session = get_session_factory()()
    try:
        assert order_count_for(session, user_id=shop.consumer_id) == CONCURRENT_CALLERS
        locks = [
            row
            for row in movements_for(session, sku_id=shop.sku_ids[2])
            if row.movement_type == MovementType.ORDER_LOCK.value
        ]
        assert len(locks) == CONCURRENT_CALLERS
        available, locked, _version = read_position(
            session, sku_id=shop.sku_ids[2], warehouse_id=shop.warehouse_id
        )
        assert available == OPENING_STOCK - CONCURRENT_CALLERS
        assert locked == CONCURRENT_CALLERS
    finally:
        session.close()


def test_concurrent_creates_against_scarce_stock_reserve_exactly_what_exists(shop: Shop) -> None:
    """Distinct keys, but only two baskets' worth of stock.

    This joins the two halves: idempotency does not by itself stop a *different* order
    from taking the last unit, and the reservation path must refuse rather than
    oversell (INV-001 is the ``CHECK``; the lock is the decision). Two win, the rest
    get ``INSUFFICIENT_STOCK (40000)``, and no order is left behind for a failed
    attempt.
    """
    from app.core.errors import InsufficientStockError
    from app.modules.inventory.models import Inventory

    session = get_session_factory()()
    try:
        # Leave room for exactly two single-unit orders.
        inventory = session.query(Inventory).filter(Inventory.sku_id == shop.sku_ids[1]).one()
        inventory.available_qty = 2
        inventory.version += 1
        session.commit()
    finally:
        session.close()

    callers = 6
    # The barrier needs exactly as many parties as there are threads - a mismatch
    # deadlocks the whole test until the timeout breaks it.
    barrier = threading.Barrier(callers)

    def attempt(index: int) -> str:
        session = get_session_factory()()
        try:
            barrier.wait(timeout=30)
            OrderService(session).create_order(
                principal=shop.consumer,
                items=[OrderLineInput(shop.sku_ids[1], 1)],
                address_id=shop.address_id,
                client_request_id=f"{shop.marker}-scarce-{index}",
                idempotency_key=f"{shop.marker}-scarce-{index}",
            )
            return "created"
        except InsufficientStockError:
            session.rollback()
            return "refused_40000"
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=callers) as pool:
        outcomes = list(pool.map(attempt, range(callers)))

    tally = Counter(outcomes)
    assert tally["created"] == 2, tally
    assert tally["refused_40000"] == callers - 2, tally

    session = get_session_factory()()
    try:
        available, locked, _version = read_position(
            session, sku_id=shop.sku_ids[1], warehouse_id=shop.warehouse_id
        )
        assert (available, locked) == (0, 2)
        # The refused attempts left no order behind: two rows, not six.
        assert order_count_for(session, user_id=shop.consumer_id) == 2
        locks = [
            row
            for row in movements_for(session, sku_id=shop.sku_ids[1])
            if row.movement_type == MovementType.ORDER_LOCK.value
        ]
        assert len(locks) == 2
    finally:
        session.close()


def test_every_claim_from_the_race_is_attributable(shop: Shop) -> None:
    """Housekeeping with a point: a key that is not marker-prefixed could never be
    cleaned up, and ``idempotency_records`` has no owner column to sweep by."""
    from sqlalchemy import select

    from app.shared.db.models.idempotency import IdempotencyRecord

    key = shop.key("attrib")
    _attempt(shop, key, threading.Barrier(1))

    session = get_session_factory()()
    try:
        keys = list(session.execute(select(IdempotencyRecord.idempotency_key)).scalars().all())
    finally:
        session.close()

    assert key in keys
    assert any(candidate.startswith(shop.marker) for candidate in keys)
