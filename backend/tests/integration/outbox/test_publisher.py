"""``publish_due`` on real MySQL - selection, retry arithmetic and the terminal state (§49).

## What is under test, and what a mock would hide

The publisher's contract has four parts and every one of them is a database property or a
clock property:

* **selection** - ``status IN (PENDING, FAILED)`` *and* ``next_retry_at IS NULL OR <= now``,
  with ``FOR UPDATE SKIP LOCKED``;
* **the attempt counter** - incremented **before** the delivery is attempted, so a crash
  mid-delivery is counted rather than invisible;
* **the backoff** - ``base * 2**(attempt-1)`` capped, written to ``next_retry_at`` so a
  broken transport cannot hot-loop;
* **the terminal state** - ``attempt_count >= max_attempts`` is ``DEAD``, because a row that
  retries forever is an outage nobody is paged about.

A fake session can be made to "pass" all four (spec §113 forbids claiming that as evidence),
and the retry-boundary test in particular needs a **real** ``DATETIME(3)`` column: the
assertion is about rows *not being selected*, which is a ``WHERE`` clause evaluated by MySQL.

## The clock, and why every test here passes an explicit ``now``

``publish_due(now=...)`` is what makes the retry boundary testable at all. Every test pins
it to a whole second (``whole_second()``), because ``next_retry_at`` is stored as a
``DATETIME(3)``: a boundary computed from a microsecond-precision clock is stored truncated,
so "one second before due" and "due" would differ by less than the driver's rounding and the
test would be measuring rounding rather than the retry rule.

## The transports

:class:`RecordingTransport` succeeds and remembers what it was handed, so "delivered exactly
once, with the right aggregate" is assertable. :class:`ExplodingTransport` fails with a
message, and can be told to fail only the first N calls - which is what lets the retry test
show a row going ``PENDING -> FAILED -> PUBLISHED`` rather than only that it goes red.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.shared.db.models.outbox import MAX_PUBLISH_ATTEMPTS, OutboxMessage, OutboxStatus
from app.shared.db.session import get_session_factory
from app.shared.outbox import (
    OutboxAggregateType,
    OutboxEventType,
    OutboxWriter,
    PublishOutcome,
    publish_due,
    run_publish_cycle,
)
from tests.integration.outbox.conftest import (
    Shop,
    rows_for_merchant,
    whole_second,
)

pytestmark = pytest.mark.integration

DEFAULT_BASE = 30
DEFAULT_CAP = 3600


class RecordingTransport:
    """Delivers everything, and records the rows it was handed."""

    def __init__(self) -> None:
        self.delivered: list[tuple[str, str, int]] = []

    def deliver(self, message: OutboxMessage) -> None:
        self.delivered.append(
            (message.event_type, message.aggregate_type, message.aggregate_id)
        )


class ExplodingTransport:
    """Fails every call, or the first ``fail_first`` calls.

    ``fail_first`` exists so one test can drive the whole lifecycle - fail, wait, succeed -
    without swapping the transport mid-test, which would leave the second half untested by
    whatever the first half broke.
    """

    def __init__(self, *, fail_first: int | None = None, message: str = "broker is down") -> None:
        self._fail_first = fail_first
        self._message = message
        self.calls = 0

    def deliver(self, message: OutboxMessage) -> None:
        self.calls += 1
        if self._fail_first is not None and self.calls > self._fail_first:
            return
        raise RuntimeError(self._message)


def enqueue_order_event(
    session: Session,
    shop: Shop,
    *,
    aggregate_id: int,
    suffix: str,
    payload: dict[str, Any] | None = None,
) -> OutboxMessage:
    """One committed ``order.created`` row, ready for the publisher to find."""
    return OutboxWriter(session).enqueue(
        event_type=OutboxEventType.ORDER_CREATED.value,
        aggregate_type=OutboxAggregateType.ORDER.value,
        aggregate_id=aggregate_id,
        idempotency_key=shop.key(suffix),
        payload=payload or {"order_no": f"NV{suffix}", "payable_amount": 100, "item_count": 1},
        merchant_id=shop.merchant_id,
    )


def row_state(message_id: int) -> dict[str, Any]:
    """The publisher's own columns, re-read after the publishing session closed.

    ``publish_due`` flushes rather than commits, so a caller that wants the outcome to
    survive must commit - which is what these tests do before reading. Reading through
    ``get_session_factory`` (a **different** session) is deliberate: the writing session's
    identity map would hand back its own version of the row.
    """
    factory = get_session_factory()
    with factory() as session:
        row = session.get(OutboxMessage, message_id)
        assert row is not None, f"outbox row {message_id} vanished"
        return {
            "status": row.status,
            "attempt_count": row.attempt_count,
            "next_retry_at": row.next_retry_at,
            "published_at": row.published_at,
            "last_error": row.last_error,
            "event_type": row.event_type,
            "idempotency_key": row.idempotency_key,
        }


# ---------------------------------------------------------------------------
# 5. The happy path
# ---------------------------------------------------------------------------
def test_a_pending_event_is_published_once_with_exactly_one_attempt(shop: Shop) -> None:
    """``PENDING -> PUBLISHED``, ``published_at`` set, ``attempt_count`` exactly ``+1``.

    "Exactly +1" rather than "==", and the row is read back from a fresh session on purpose:
    the counter is the retry ceiling's input, so an off-by-one here is a row that dies one
    attempt early or late - and a test asserting ``attempt_count > 0`` would not see it.

    **Mutation that must turn this red:** move ``message.attempt_count += 1`` *after*
    ``transport.deliver`` (the "count only what was attempted" reading). The happy path
    then reads 0. (That mutation is separately fatal to the failure path, where a crash
    mid-delivery would become invisible - no test here can observe a crash, which is why
    the ordering argument is a docstring reason rather than an assertion.)
    """
    factory = get_session_factory()
    with factory() as session:
        message = enqueue_order_event(session, shop, aggregate_id=101_001, suffix="pub-ok")
        message_id = int(message.id)
        session.commit()

    transport = RecordingTransport()
    now = whole_second()
    with factory() as session:
        outcome = publish_due(session, transport=transport, now=now, limit=10)
        session.commit()

    assert outcome.published == 1
    assert outcome.failed == 0
    assert outcome.dead == 0
    assert outcome.claimed == 1
    assert transport.delivered == [
        (OutboxEventType.ORDER_CREATED.value, OutboxAggregateType.ORDER.value, 101_001)
    ]

    state = row_state(message_id)
    assert state["status"] == OutboxStatus.PUBLISHED.value
    assert state["attempt_count"] == 1
    assert state["published_at"] is not None
    assert state["published_at"] == now
    # A published row is never selected again: the retry columns are cleared, not left set.
    assert state["next_retry_at"] is None
    assert state["last_error"] is None


def test_a_second_cycle_does_not_redeliver_a_published_row(shop: Shop) -> None:
    """Terminal means terminal: the next cycle claims nothing.

    Without this, "published once" would only mean "published once *so far*" - and the
    selection's ``status IN (PENDING, FAILED)`` clause is exactly what the test exercises.

    **Mutation that must turn this red:** add ``PUBLISHED`` to the selected statuses.
    """
    factory = get_session_factory()
    with factory() as session:
        enqueue_order_event(session, shop, aggregate_id=101_002, suffix="pub-twice")
        session.commit()

    first, second = RecordingTransport(), RecordingTransport()
    now = whole_second()
    with factory() as session:
        assert publish_due(session, transport=first, now=now, limit=10).published == 1
        session.commit()
    with factory() as session:
        again = publish_due(session, transport=second, now=now, limit=10)
        session.commit()

    assert again == PublishOutcome(published=0, failed=0, dead=0)
    assert second.delivered == []
    assert len(first.delivered) == 1


def test_limit_bounds_one_cycle(shop: Shop) -> None:
    """``limit`` is honoured, and the *oldest* rows go first.

    Ordering matters because the cycle is one-shot: with a bounded batch and no
    ``ORDER BY id``, a large outbox could starve the oldest events indefinitely.

    **Mutation that must turn this red:** drop ``.limit(limit)``, or the ``order_by``.
    """
    factory = get_session_factory()
    with factory() as session:
        for index in range(4):
            enqueue_order_event(
                session, shop, aggregate_id=102_000 + index, suffix=f"limit-{index}"
            )
        session.commit()

    transport = RecordingTransport()
    now = whole_second()
    with factory() as session:
        outcome = publish_due(session, transport=transport, now=now, limit=2)
        session.commit()

    assert outcome.published == 2
    assert [entry[2] for entry in transport.delivered] == [102_000, 102_001]


def test_run_publish_cycle_commits_by_itself(shop: Shop) -> None:
    """The worker entry point owns its transaction - the outcome survives with no caller.

    ``publish_due`` deliberately does not commit; ``run_publish_cycle`` is the caller that
    does, via ``session_scope``. A test that asserted only on ``publish_due`` would leave
    "who commits in production?" unanswered, and the answer is the difference between a
    worker that publishes and a worker that publishes and then throws it away.

    **Mutation that must turn this red:** replace ``session_scope()`` with a plain session
    in ``run_publish_cycle`` (no commit) - the row reads back ``PENDING``.
    """
    factory = get_session_factory()
    with factory() as session:
        message = enqueue_order_event(session, shop, aggregate_id=103_001, suffix="cycle")
        message_id = int(message.id)
        session.commit()

    transport = RecordingTransport()
    outcome = run_publish_cycle(transport=transport, limit=10, now=whole_second())

    assert outcome.published == 1
    assert transport.delivered == [
        (OutboxEventType.ORDER_CREATED.value, OutboxAggregateType.ORDER.value, 103_001)
    ]
    assert row_state(message_id)["status"] == OutboxStatus.PUBLISHED.value


# ---------------------------------------------------------------------------
# 6. Failure, backoff, and the boundary
# ---------------------------------------------------------------------------
def test_a_failed_delivery_backs_off_and_is_invisible_before_next_retry_at(shop: Shop) -> None:
    """``FAILED`` + a future ``next_retry_at``, and the boundary is real in both directions.

    Three assertions, and the middle one is why this test needs a database: at a ``now``
    *before* ``next_retry_at`` the row must not be selected, and at a ``now`` *after* it the
    row must be selected again. Asserting only on ``next_retry_at`` would test the
    arithmetic; asserting the skip tests the ``WHERE`` clause the arithmetic exists to serve
    - and a row that hot-loops on a broken transport is an outage, not a slow retry.

    ``base_backoff_seconds=30`` and the first attempt gives ``30 * 2**0 == 30``. The exact
    expected value is asserted rather than "some time in the future", because ``2**`` is
    exactly the part that gets written wrong (``2**attempt`` with an un-decremented
    ``attempt_count`` shifts every retry by one doubling and is still "in the future").

    **Mutation that must turn this red:** write ``now + base`` for every attempt instead of
    ``base * 2**(attempt-1)``, or drop the ``next_retry_at <= now`` clause from the
    selection.
    """
    factory = get_session_factory()
    with factory() as session:
        message = enqueue_order_event(session, shop, aggregate_id=104_001, suffix="fail")
        message_id = int(message.id)
        session.commit()

    now = whole_second()
    transport = ExplodingTransport()
    with factory() as session:
        outcome = publish_due(
            session, transport=transport, now=now, limit=10, base_backoff_seconds=DEFAULT_BASE
        )
        session.commit()

    assert outcome.published == 0
    assert outcome.failed == 1
    assert outcome.dead == 0
    assert transport.calls == 1

    state = row_state(message_id)
    assert state["status"] == OutboxStatus.FAILED.value
    assert state["attempt_count"] == 1
    assert state["published_at"] is None
    assert state["last_error"] == "broker is down"
    assert state["next_retry_at"] == now + timedelta(seconds=DEFAULT_BASE)

    # -- not due yet: the transport must not even be called ------------------
    early = ExplodingTransport()
    with factory() as session:
        skipped = publish_due(
            session, transport=early, now=now + timedelta(seconds=DEFAULT_BASE - 1), limit=10
        )
        session.commit()
    assert skipped == PublishOutcome(published=0, failed=0, dead=0)
    assert early.calls == 0
    assert row_state(message_id)["attempt_count"] == 1, "a skipped row was still counted"

    # -- due again: one second past the boundary the row comes back ----------
    retry = RecordingTransport()
    with factory() as session:
        retried = publish_due(session, transport=retry, now=now + timedelta(seconds=DEFAULT_BASE),
                              limit=10)
        session.commit()
    assert retried.published == 1
    assert len(retry.delivered) == 1

    state = row_state(message_id)
    assert state["status"] == OutboxStatus.PUBLISHED.value
    # 1 failed + 1 successful attempt = 2, and the failure recovery clears the error text.
    assert state["attempt_count"] == 2
    assert state["last_error"] is None
    assert state["next_retry_at"] is None


def test_the_backoff_doubles_per_attempt_and_is_capped(shop: Shop) -> None:
    """``base * 2**(n-1)``, capped at ``max_backoff_seconds`` - asserted as a sequence.

    Four failures are driven one cycle at a time, each with an explicit ``now`` a little past
    the previous ``next_retry_at``, so the recorded delays are ``30, 60, 120, 240`` - and
    then the cap is shown to bite by re-running the same arithmetic with a small
    ``max_backoff_seconds``. The sequence assertion is what catches a ``2**n`` (off by a
    doubling) or a cap that truncates the first delay.

    **Mutation that must turn this red:** swap ``2**exponent`` for a linear
    ``base * attempt_count``, or drop the ``min(..., max_backoff_seconds)``.
    """
    factory = get_session_factory()
    with factory() as session:
        message = enqueue_order_event(session, shop, aggregate_id=104_002, suffix="backoff")
        message_id = int(message.id)
        session.commit()

    delays: list[int] = []
    moment = whole_second()
    transport = ExplodingTransport()
    for _ in range(4):
        with factory() as session:
            publish_due(
                session,
                transport=transport,
                now=moment,
                limit=10,
                base_backoff_seconds=DEFAULT_BASE,
                max_backoff_seconds=DEFAULT_CAP,
            )
            session.commit()
        state = row_state(message_id)
        assert state["status"] == OutboxStatus.FAILED.value
        wrote_at = moment
        moment = state["next_retry_at"]
        delays.append(int((moment - wrote_at).total_seconds()))

    assert delays == [30, 60, 120, 240]
    assert row_state(message_id)["attempt_count"] == 4

    # -- the cap ---------------------------------------------------------------
    with factory() as session:
        capped = enqueue_order_event(session, shop, aggregate_id=104_003, suffix="capped")
        capped_id = int(capped.id)
        session.commit()

    minute = whole_second()
    with factory() as session:
        publish_due(
            session,
            transport=ExplodingTransport(),
            now=minute,
            limit=10,
            base_backoff_seconds=DEFAULT_BASE,
            max_backoff_seconds=45,
        )
        session.commit()
    first_delay = row_state(capped_id)["next_retry_at"] - minute
    assert int(first_delay.total_seconds()) == 30, "under the cap, the base delay is unchanged"

    with factory() as session:
        publish_due(
            session,
            transport=ExplodingTransport(),
            now=minute + timedelta(seconds=30),
            limit=10,
            base_backoff_seconds=DEFAULT_BASE,
            # The uncapped second delay would be 60; 45 must win.
            max_backoff_seconds=45,
        )
        session.commit()
    second_delay = row_state(capped_id)["next_retry_at"] - (minute + timedelta(seconds=30))
    assert int(second_delay.total_seconds()) == 45


# ---------------------------------------------------------------------------
# 7. The terminal state
# ---------------------------------------------------------------------------
def test_consecutive_failures_reach_dead_at_max_attempts(shop: Shop) -> None:
    """Exactly ``max_attempts`` failures -> ``DEAD``, and the row stops being selected.

    The count has to be *exact* in both directions: one attempt short of ``max_attempts``
    must still be ``FAILED`` (or the ceiling is lower than the constant says and a
    transient broker outage kills an event), and the attempt at the ceiling must be ``DEAD``
    (or the row retries forever).

    The terminal row is also shown to be **unselectable and unreachable** - ``next_retry_at``
    is cleared, and a later cycle delivers nothing - which is the property an operator's
    "is it still trying?" question depends on.

    **Mutation that must turn this red:** change ``>=`` to ``>`` in the dead transition (the
    row survives one extra attempt), or stop selecting ``FAILED`` (nothing is ever retried
    at all - caught instead by the backoff test).
    """
    factory = get_session_factory()
    with factory() as session:
        message = enqueue_order_event(session, shop, aggregate_id=105_001, suffix="dead")
        message_id = int(message.id)
        session.commit()

    transport = ExplodingTransport()
    moment = whole_second()
    for attempt in range(1, MAX_PUBLISH_ATTEMPTS + 1):
        with factory() as session:
            outcome = publish_due(session, transport=transport, now=moment, limit=10)
            session.commit()
        state = row_state(message_id)
        assert state["attempt_count"] == attempt
        if attempt < MAX_PUBLISH_ATTEMPTS:
            assert outcome.failed == 1, f"attempt {attempt} should still be retryable"
            assert state["status"] == OutboxStatus.FAILED.value
            assert outcome.dead == 0
            moment = state["next_retry_at"]
        else:
            assert outcome.dead == 1
            assert outcome.failed == 0
            assert state["status"] == OutboxStatus.DEAD.value

    final = row_state(message_id)
    assert final["status"] == OutboxStatus.DEAD.value
    assert final["attempt_count"] == MAX_PUBLISH_ATTEMPTS
    assert final["next_retry_at"] is None
    assert final["published_at"] is None
    assert final["last_error"] == "broker is down"
    assert transport.calls == MAX_PUBLISH_ATTEMPTS

    # A dead row is terminal: a much later cycle neither retries nor redelivers it.
    later = RecordingTransport()
    with factory() as session:
        after = publish_due(session, transport=later, now=moment + timedelta(days=30), limit=10)
        session.commit()
    assert after == PublishOutcome(published=0, failed=0, dead=0)
    assert later.delivered == []
    assert row_state(message_id)["attempt_count"] == MAX_PUBLISH_ATTEMPTS


def test_max_attempts_is_honoured_when_the_caller_lowers_it(shop: Shop) -> None:
    """``max_attempts`` is a parameter, not a literal - a two-attempt budget dies on two.

    The constant is read from one place (``MAX_PUBLISH_ATTEMPTS``) so the publisher and the
    tests cannot disagree, but the parameter is what a caller tunes; a hard-coded ``5`` in
    the comparison would pass the test above and make this one red.

    **Mutation that must turn this red:** compare against the module constant instead of the
    ``max_attempts`` argument.
    """
    factory = get_session_factory()
    with factory() as session:
        message = enqueue_order_event(session, shop, aggregate_id=105_002, suffix="custom-max")
        message_id = int(message.id)
        session.commit()

    transport = ExplodingTransport()
    moment = whole_second()
    with factory() as session:
        first = publish_due(session, transport=transport, now=moment, limit=10, max_attempts=2)
        session.commit()
    assert first.failed == 1
    assert row_state(message_id)["status"] == OutboxStatus.FAILED.value

    with factory() as session:
        second = publish_due(
            session,
            transport=transport,
            now=row_state(message_id)["next_retry_at"],
            limit=10,
            max_attempts=2,
        )
        session.commit()
    assert second.dead == 1
    assert row_state(message_id)["status"] == OutboxStatus.DEAD.value


def test_a_transport_error_is_recorded_and_truncated_to_the_column(shop: Shop) -> None:
    """``last_error`` carries the transport's message, bounded at 500 characters.

    Bounded because the column is ``String(500)``: an unbounded write would fail the very
    statement meant to record the failure, turning a retryable delivery error into a data
    error on the publisher - and on MySQL (strict mode) the ``INSERT``/``UPDATE`` would be
    rejected outright, so the row would stay ``PENDING`` and be retried with no error
    recorded. The long-message case is the one a mock never reproduces.

    **Mutation that must turn this red:** remove the ``[:500]`` slice.
    """
    factory = get_session_factory()
    with factory() as session:
        message = enqueue_order_event(session, shop, aggregate_id=105_003, suffix="long-error")
        message_id = int(message.id)
        session.commit()

    long_message = "x" * 900
    with factory() as session:
        publish_due(
            session,
            transport=ExplodingTransport(message=long_message),
            now=whole_second(),
            limit=10,
        )
        session.commit()

    state = row_state(message_id)
    assert state["status"] == OutboxStatus.FAILED.value
    assert state["last_error"] == long_message[:500]
    assert len(state["last_error"]) == 500


def test_a_non_exception_error_does_not_stop_the_batch(shop: Shop) -> None:
    """One failing row does not abandon the rest of the batch, and does not abort the cycle.

    ``publish_due`` catches every exception from the transport (``BLE001``, deliberate) and
    continues. Catching only a broker-specific exception would let one poisoned row stop
    every row behind it - and because the selection is ``ORDER BY id``, that is *every*
    later event, permanently.

    The assertion is on the batch's arithmetic: 1 dead + 2 published from 3 rows, with the
    dead one being the first by id, so the failure is genuinely in the middle of the loop.

    **Mutation that must turn this red:** re-raise instead of catching in the ``except``
    block.
    """
    factory = get_session_factory()
    with factory() as session:
        for index in range(3):
            enqueue_order_event(
                session, shop, aggregate_id=106_000 + index, suffix=f"batch-{index}"
            )
        session.commit()

    class Selective:
        """Fails on exactly one aggregate, publishes the others."""

        def __init__(self) -> None:
            self.delivered: list[int] = []

        def deliver(self, message: OutboxMessage) -> None:
            if message.aggregate_id == 106_001:
                raise RuntimeError("this one row is poisoned")
            self.delivered.append(message.aggregate_id)

    transport = Selective()
    with factory() as session:
        outcome = publish_due(
            session,
            transport=transport,
            now=whole_second(),
            limit=10,
            max_attempts=1,
        )
        session.commit()

    assert (outcome.published, outcome.failed, outcome.dead) == (2, 0, 1)
    assert transport.delivered == [106_000, 106_002]

    rows = {row.aggregate_id: row.status for row in rows_for_merchant(merchant_id=shop.merchant_id)}
    assert rows[106_000] == OutboxStatus.PUBLISHED.value
    assert rows[106_001] == OutboxStatus.DEAD.value
    assert rows[106_002] == OutboxStatus.PUBLISHED.value


# ---------------------------------------------------------------------------
# A selection guard the retry tests depend on
# ---------------------------------------------------------------------------
def test_a_row_with_published_at_is_never_selected_by_a_later_clock(shop: Shop) -> None:
    """The selection is a *state* test, not a timestamp comparison against ``created_at``.

    Cheap, and it pins a plausible wrong implementation: "select everything older than now"
    would re-deliver the whole outbox on every tick once the clock moved past a published
    row's ``created_at``. ``published_at`` is a historical fact; ``status`` is the state.

    **Mutation that must turn this red:** select on ``created_at <= now`` instead of
    ``status``/``next_retry_at``.
    """
    factory = get_session_factory()
    with factory() as session:
        message = enqueue_order_event(session, shop, aggregate_id=107_001, suffix="state-only")
        message_id = int(message.id)
        session.commit()

    with factory() as session:
        publish_due(session, transport=RecordingTransport(), now=whole_second(), limit=10)
        session.commit()
    published_at = row_state(message_id)["published_at"]
    assert published_at is not None

    transport = RecordingTransport()
    with factory() as session:
        outcome = publish_due(
            session, transport=transport, now=published_at + timedelta(days=1), limit=10
        )
        session.commit()

    assert outcome == PublishOutcome(published=0, failed=0, dead=0)
    assert transport.delivered == []


def test_the_publisher_does_not_commit_for_the_caller(shop: Shop) -> None:
    """``publish_due`` flushes but does not commit - the caller's rollback undoes it.

    This is the contract the docstring states and no other test observes: ``publish_due``
    must leave the transaction boundary to its caller, because a caller may be publishing
    inside a larger unit of work. ``run_publish_cycle`` is the caller that commits, and it
    is tested above.

    **Mutation that must turn this red:** add ``session.commit()`` next to the ``flush()``.
    """
    factory = get_session_factory()
    with factory() as session:
        message = enqueue_order_event(session, shop, aggregate_id=108_001, suffix="no-commit")
        message_id = int(message.id)
        session.commit()

    with factory() as session:
        publish_due(session, transport=RecordingTransport(), now=whole_second(), limit=10)
        # Visible inside the transaction, invisible after the rollback.
        in_transaction = session.get(OutboxMessage, message_id)
        assert in_transaction is not None
        assert in_transaction.status == OutboxStatus.PUBLISHED.value
        session.rollback()

    state = row_state(message_id)
    assert state["status"] == OutboxStatus.PENDING.value
    assert state["attempt_count"] == 0
    assert state["published_at"] is None
