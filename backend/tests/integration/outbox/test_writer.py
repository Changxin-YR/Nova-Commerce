"""``OutboxWriter.enqueue`` on real MySQL - atomicity, dedup, and the payload guard (§49).

## What is actually under test

Three distinct properties, and each one is a database property:

1. **Atomicity.** ``enqueue`` never commits and never opens a transaction. The event row
   is a row in the caller's transaction, so it is visible exactly when the business rows
   are. A writer that committed on its own ("so the event is not lost") would pass every
   happy-path assertion in this file and fail :func:`test_a_rolled_back_create_leaves_no_event_row`.
2. **Dedup on the aggregate.** ``uq_event_type_aggregate`` is the anchor, not the emitter's
   ``idempotency_key``. Both directions are asserted, and the *second* one is the important
   one: two different aggregates that happen to share an ``idempotency_key`` must produce
   two rows, because the opposite ("dedup on the emitter key") is the plausible-but-wrong
   design that silently drops a legitimate event.
3. **The payload guard.** A payload that names a secret-bearing key is refused *before*
   anything is written, and the guard is shown to be the thing doing the refusing rather
   than a ``ValueError`` from somewhere else in the call.

## The mutation each test is designed to catch

A test that cannot go red is a comment. Each module docstring names the edit that breaks
its test; the run that produced this file's evidence includes the mutations per HANDOFF
section 18.4 obligation 7 ("a guard never observed failing is a guard nobody has shown
works: mutate it").
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

import pytest

from app.core.errors import SkuNotFoundError
from app.core.redaction import assertion_that_no_secret_remains
from app.modules.order.service import OrderService
from app.modules.order.workflow import OrderLineInput
from app.shared.db.base import utc_now
from app.shared.db.session import get_session_factory
from app.shared.outbox import (
    OUTBOX_AGGREGATE_TYPES,
    OUTBOX_EVENT_TYPES,
    OutboxAggregateType,
    OutboxEventType,
    OutboxWriter,
)
from tests.integration.outbox.conftest import (
    Shop,
    outbox_rows,
    rows_for_merchant,
)

pytestmark = pytest.mark.integration

#: A SKU id that cannot exist: ``BIGINT UNSIGNED AUTO_INCREMENT`` never hands one out,
#: so the lookup in ``CreateOrderWorkflow`` step 4 misses without depending on a fixture
#: having deleted anything.
ABSENT_SKU_ID = 9_999_999_999


# ---------------------------------------------------------------------------
# 1. Atomicity - the row is part of the caller's transaction, not a write of its own
# ---------------------------------------------------------------------------
def test_a_rolled_back_create_leaves_no_event_row(shop: Shop) -> None:
    """A real failed ``CreateOrderWorkflow`` queues nothing - the §49 atomicity claim.

    **Mutation that must turn this red:** give ``OutboxWriter.enqueue`` its own
    ``session.commit()`` (or wrap it in ``session_scope``), which is the plausible
    "make sure the event is not lost" edit. The order row still rolls back; the event
    row survives and announces an order that does not exist.
    """
    session = get_session_factory()()
    try:
        with pytest.raises(SkuNotFoundError) as raised:
            OrderService(session).create_order(
                principal=shop.consumer,
                items=[OrderLineInput(sku_id=ABSENT_SKU_ID, quantity=1)],
                address_id=shop.address_id,
                client_request_id=shop.client_request_id("atomic"),
                idempotency_key=shop.key("atomic"),
            )
        # The failure is the *business* failure, not an unrelated import or fixture fault:
        # without this the test would pass if create_order raised for any reason at all.
        assert raised.value.code == SkuNotFoundError.code
        assert raised.value.http_status == 404
    finally:
        session.close()

    # Read on a fresh session (HANDOFF section 6): the writing session's view of its own
    # rollback is not evidence about the database.
    assert rows_for_merchant(merchant_id=shop.merchant_id) == []


def test_enqueue_inside_a_rolled_back_transaction_writes_nothing(shop: Shop) -> None:
    """The writer in isolation: ``enqueue`` then ``rollback`` -> no row.

    This is the same property as above with the workflow taken out of the picture, so the
    failure of either one localises itself: if the workflow test is red and this one is
    green, the commit is somewhere in the workflow; if both are red, it is in the writer.

    **Mutation that must turn this red:** the same one - a ``commit()`` inside ``enqueue``.
    """
    session = get_session_factory()()
    try:
        message = OutboxWriter(session).enqueue(
            event_type=OutboxEventType.ORDER_CREATED.value,
            aggregate_type=OutboxAggregateType.ORDER.value,
            aggregate_id=424_242,
            idempotency_key=shop.key("rollback"),
            payload={"order_no": "NVROLLBACK", "payable_amount": 1999, "item_count": 1},
            merchant_id=shop.merchant_id,
        )
        # In-transaction the row exists: ``enqueue`` flushes, so the caller can read back
        # what it appended without committing. A writer that buffered the insert until the
        # caller's commit would make this assertion fail - and would also break the
        # IntegrityError path below, which depends on the INSERT actually reaching MySQL.
        assert message.id is not None
        assert outbox_rows(session, merchant_id=shop.merchant_id)[0].id == message.id
        session.rollback()
    finally:
        session.close()

    assert rows_for_merchant(merchant_id=shop.merchant_id) == []


# ---------------------------------------------------------------------------
# 2. Dedup - the anchor is the aggregate, and a shared emitter key is NOT a collision
# ---------------------------------------------------------------------------
def test_the_same_aggregate_twice_yields_one_row(shop: Shop) -> None:
    """Same ``(event_type, aggregate_type, aggregate_id)``, two different keys -> **1** row.

    The second call is answered by the unique index (``IntegrityError``) and returns the
    row that won, rather than raising - the backstop has to be silent, because a replay
    that was supposed to be safe must not become a 500.

    **Mutation that must turn this red:** drop ``uq_event_type_aggregate`` from the model
    (and the test database) so the second append succeeds, or make ``enqueue`` re-raise the
    ``IntegrityError`` - the first gives 2 rows, the second gives an exception instead of a
    row. Both are wrong for a documented reason, and this test is what says so.
    """
    session = get_session_factory()()
    try:
        writer = OutboxWriter(session)
        first = writer.enqueue(
            event_type=OutboxEventType.ORDER_CREATED.value,
            aggregate_type=OutboxAggregateType.ORDER.value,
            aggregate_id=777_001,
            idempotency_key=shop.key("dedup-a"),
            payload={"order_no": "NVONE", "payable_amount": 100, "item_count": 1},
            merchant_id=shop.merchant_id,
        )
        second = writer.enqueue(
            event_type=OutboxEventType.ORDER_CREATED.value,
            aggregate_type=OutboxAggregateType.ORDER.value,
            aggregate_id=777_001,
            # A *different* emitter key on purpose: the anchor must not be the key.
            idempotency_key=shop.key("dedup-b"),
            payload={"order_no": "NVONE", "payable_amount": 100, "item_count": 1},
            merchant_id=shop.merchant_id,
        )
        # The caller also gets the row that won, not the transient one it tried to add:
        # returning a detached/rolled-back instance would hand a publisher a phantom.
        assert second.id == first.id
        assert second.idempotency_key == first.idempotency_key
        session.commit()
    finally:
        session.close()

    rows = rows_for_merchant(merchant_id=shop.merchant_id)
    assert len(rows) == 1
    assert rows[0].aggregate_id == 777_001


def test_two_aggregates_sharing_one_idempotency_key_yield_two_rows(shop: Shop) -> None:
    """**The false-collision guard.** Different ``aggregate_id``, same key -> **2** rows.

    This is the test the whole key choice exists for, and the one the task brief calls
    out as the critical difference from "dedup on the emitter's key". An emitter's
    ``idempotency_key`` is only unique *inside its own scope* - two merchants may send the
    same string, a refund key is unique per merchant, a payment event id is unique per
    provider. A global unique on that string would silently drop the second, legitimate
    event, and a lost event is invisible while a duplicate is loud.

    **Mutation that must turn this red:** change ``uq_event_type_aggregate`` to a unique on
    ``idempotency_key`` (with or without ``event_type``). The second ``enqueue`` then
    returns the first row and this assertion reads 1. That is precisely the defect the
    model docstring argues against, and this is the only test that catches it.

    **Negative control (in the same test, so a green run cannot be ambiguous):** the two
    rows must be *different rows* with different aggregates - asserting only the count
    would pass if ``enqueue`` had simply written the same row twice under one id, which is
    not what "no false collision" means.
    """
    shared_key = shop.key("same-emitter-key")
    session = get_session_factory()()
    try:
        writer = OutboxWriter(session)
        first = writer.enqueue(
            event_type=OutboxEventType.ORDER_CREATED.value,
            aggregate_type=OutboxAggregateType.ORDER.value,
            aggregate_id=888_001,
            idempotency_key=shared_key,
            payload={"order_no": "NVCOLLIDE1", "payable_amount": 100, "item_count": 1},
            merchant_id=shop.merchant_id,
        )
        second = writer.enqueue(
            event_type=OutboxEventType.ORDER_CREATED.value,
            aggregate_type=OutboxAggregateType.ORDER.value,
            aggregate_id=888_002,
            idempotency_key=shared_key,
            payload={"order_no": "NVCOLLIDE2", "payable_amount": 200, "item_count": 2},
            merchant_id=shop.merchant_id,
        )
        assert first.id != second.id, (
            "a shared emitter key collapsed two distinct aggregates into one row - the "
            "false-collision direction, which silently loses a legitimate event"
        )
        session.commit()
    finally:
        session.close()

    rows = rows_for_merchant(
        merchant_id=shop.merchant_id,
        event_type=OutboxEventType.ORDER_CREATED.value,
    )
    assert len(rows) == 2
    assert sorted(row.aggregate_id for row in rows) == [888_001, 888_002]
    assert {row.idempotency_key for row in rows} == {shared_key}


# ---------------------------------------------------------------------------
# 3. The payload guard
# ---------------------------------------------------------------------------
def test_a_payload_naming_a_secret_is_refused_and_writes_nothing(shop: Shop) -> None:
    """``{"token": "x"}`` -> ``ValueError`` and **no** row. The guard fires before the write.

    Two assertions, and the second is the one that makes this a guard test rather than a
    validation test: refusing is easy, and refusing *after* the insert has reached the
    database is a leak with a nice error message. ``enqueue`` must check the vocabulary and
    the payload before constructing the row.

    **Mutation that must turn this red:** move the ``assertion_that_no_secret_remains``
    block below the ``session.add(message)`` / ``flush()`` (or delete it). The first makes
    the row exist despite the ValueError; the second makes the call succeed.

    The three payloads are not interchangeable: ``token`` and ``signature`` are matched by
    the redaction module's key *markers*, and the nested one proves the walk is recursive
    rather than a check of the top-level keys.
    """
    leaks: list[dict[str, Any]] = [
        {"order_no": "NVSECRET", "token": "x"},
        {"order_no": "NVSECRET", "signature": "deadbeef"},
        {"order_no": "NVSECRET", "snapshot": {"nested": {"refresh_token": "y"}}},
    ]
    # The guard itself agrees these are leaks - so a green run cannot mean "the test picked
    # a key the redaction module does not consider secret".
    for payload in leaks:
        assert assertion_that_no_secret_remains(payload) != []

    session = get_session_factory()()
    try:
        writer = OutboxWriter(session)
        for index, payload in enumerate(leaks):
            with pytest.raises(ValueError) as raised:
                writer.enqueue(
                    event_type=OutboxEventType.ORDER_CREATED.value,
                    aggregate_type=OutboxAggregateType.ORDER.value,
                    aggregate_id=999_000 + index,
                    idempotency_key=shop.key(f"secret-{index}"),
                    payload=dict(payload),
                    merchant_id=shop.merchant_id,
                )
            assert "sensitive" in str(raised.value)
        # The session is still usable: ``enqueue`` writes nothing before it raises, so
        # there is no failed statement for SQLAlchemy to mark the transaction with.
        session.commit()
    finally:
        session.close()

    assert rows_for_merchant(merchant_id=shop.merchant_id) == []


@pytest.mark.parametrize("aggregate_type", ["Order", "orders", "  order  "])
def test_an_unknown_aggregate_type_is_refused(shop: Shop, aggregate_type: str) -> None:
    """An unknown ``aggregate_type`` is a ``ValueError``, before any write.

    The vocabulary is checked against ``OUTBOX_AGGREGATE_TYPES`` rather than trusted, so a
    typo becomes a loud failure at the seam instead of a row a consumer cannot route.
    ``"Order"`` is the realistic typo (this project's SQL vocabulary is upper-case and the
    outbox's is lower-case, so the mix-up is easy to make and easy to miss).

    **Mutation that must turn this red:** delete the ``aggregate_type not in
    OUTBOX_AGGREGATE_TYPES`` check from ``enqueue``.
    """
    assert aggregate_type not in OUTBOX_AGGREGATE_TYPES
    session = get_session_factory()()
    try:
        with pytest.raises(ValueError) as raised:
            OutboxWriter(session).enqueue(
                event_type=OutboxEventType.ORDER_CREATED.value,
                aggregate_type=aggregate_type,
                aggregate_id=555_000,
                idempotency_key=shop.key("bad-aggregate"),
                payload={"order_no": "NVBAD", "payable_amount": 1, "item_count": 1},
                merchant_id=shop.merchant_id,
            )
        assert "aggregate_type" in str(raised.value)
    finally:
        session.close()

    assert rows_for_merchant(merchant_id=shop.merchant_id) == []


@pytest.mark.parametrize("event_type", ["order.creatd", "Order.created", "refund.done", " order.created "])
def test_an_unknown_event_type_is_refused(shop: Shop, event_type: str) -> None:
    """Same for ``event_type``: the vocabulary is a closed list, enforced at the seam.

    **Mutation that must turn this red:** delete the ``event_type not in
    OUTBOX_EVENT_TYPES`` check from ``enqueue``.
    """
    assert event_type not in OUTBOX_EVENT_TYPES
    session = get_session_factory()()
    try:
        with pytest.raises(ValueError) as raised:
            OutboxWriter(session).enqueue(
                event_type=event_type,
                aggregate_type=OutboxAggregateType.ORDER.value,
                aggregate_id=556_000,
                idempotency_key=shop.key("bad-event"),
                payload={"order_no": "NVBAD", "payable_amount": 1, "item_count": 1},
                merchant_id=shop.merchant_id,
            )
        assert "event_type" in str(raised.value)
    finally:
        session.close()

    assert rows_for_merchant(merchant_id=shop.merchant_id) == []


def test_the_caller_can_keep_using_its_session_after_a_dedup_collision(shop: Shop) -> None:
    """A dedup hit leaves the caller's transaction healthy - the SAVEPOINT's whole job.

    On MySQL a failed statement does not abort the transaction, but SQLAlchemy *marks* the
    session for rollback, so catching the ``IntegrityError`` and carrying on would raise
    ``PendingRollbackError`` on the next statement. That is why ``enqueue`` uses
    ``begin_nested()``. Without this test, the dedup test above passes while every real
    emitter that relies on the collision being survivable is broken - and it is the
    *emitters* that matter, not the dedup assertion.

    The second half is the interesting one: after the collision the caller appends a
    *different* event **and commits**, and both the business row and that event must be
    visible. A ``rollback()`` instead of a SAVEPOINT rollback would silently discard them.

    **Mutation that must turn this red:** replace ``with self._session.begin_nested()`` with
    a plain ``flush()`` in a try/except, which is the obvious way to write this and the way
    ``writer.py``'s docstring says not to.
    """
    session = get_session_factory()()
    try:
        writer = OutboxWriter(session)
        first = writer.enqueue(
            event_type=OutboxEventType.ORDER_CREATED.value,
            aggregate_type=OutboxAggregateType.ORDER.value,
            aggregate_id=666_001,
            idempotency_key=shop.key("savepoint-a"),
            payload={"order_no": "NVSAVEPOINT", "payable_amount": 100, "item_count": 1},
            merchant_id=shop.merchant_id,
        )
        # The collision: same aggregate, different key.
        duplicate = writer.enqueue(
            event_type=OutboxEventType.ORDER_CREATED.value,
            aggregate_type=OutboxAggregateType.ORDER.value,
            aggregate_id=666_001,
            idempotency_key=shop.key("savepoint-b"),
            payload={"order_no": "NVSAVEPOINT", "payable_amount": 100, "item_count": 1},
            merchant_id=shop.merchant_id,
        )
        assert duplicate.id == first.id

        # A *different* event, appended on the very same session, after the failed insert.
        after = writer.enqueue(
            event_type=OutboxEventType.PAYMENT_SETTLED.value,
            aggregate_type=OutboxAggregateType.PAYMENT.value,
            aggregate_id=666_002,
            idempotency_key=shop.key("savepoint-c"),
            payload={
                "payment_no": "NVP666002",
                "order_no": "NVSAVEPOINT",
                "amount": 100,
                "provider": "MOCK",
            },
            merchant_id=shop.merchant_id,
        )
        assert after.id is not None and after.id != first.id
        session.commit()
    finally:
        session.close()

    rows = rows_for_merchant(merchant_id=shop.merchant_id)
    assert [(row.event_type, row.aggregate_id) for row in rows] == [
        (OutboxEventType.ORDER_CREATED.value, 666_001),
        (OutboxEventType.PAYMENT_SETTLED.value, 666_002),
    ]


def test_the_writer_never_sets_a_publication_state(shop: Shop) -> None:
    """A queued row is ``PENDING``/0 attempts with no ``published_at`` - not half-published.

    Cheap, but it is the boundary the publisher's tests assume. ``run_publish_cycle`` is a
    *different* code path from the writer, and a writer that pre-set ``PUBLISHED`` (or
    bumped ``attempt_count``) would make the publisher's "attempt_count is +1" assertion
    read 2 while looking like an off-by-one in the retry arithmetic.

    **Mutation that must turn this red:** set ``status=PUBLISHED`` or ``attempt_count=1`` as
    the model default.
    """
    before = utc_now()
    session = get_session_factory()()
    try:
        OutboxWriter(session).enqueue(
            event_type=OutboxEventType.ORDER_CREATED.value,
            aggregate_type=OutboxAggregateType.ORDER.value,
            aggregate_id=444_001,
            idempotency_key=shop.key("pending"),
            payload={"order_no": "NVPENDING", "payable_amount": 100, "item_count": 1},
            merchant_id=shop.merchant_id,
        )
        session.commit()
    finally:
        session.close()

    row = rows_for_merchant(merchant_id=shop.merchant_id)[0]
    assert row.status == "PENDING"
    assert row.attempt_count == 0
    assert row.published_at is None
    assert row.next_retry_at is None
    assert row.last_error is None
    # created_at *is* the event time - the contract deliberately carries no timestamp in the
    # payload, so this column is the only record of when the event happened. Bracketed
    # rather than compared to ``whole_second()``: the column is a DATETIME(3) defaulted by
    # MySQL, and a truncated "now" is in the *past* by up to a second, which would make the
    # assertion measure truncation rather than the default being written at all.
    assert row.created_at is not None
    assert before <= row.created_at.astimezone(UTC) <= utc_now()
