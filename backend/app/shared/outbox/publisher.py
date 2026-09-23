"""The outbox publisher - deliver committed events, retry via ``next_retry_at`` (§49).

    publish_due(...)   the core, pure-of-framework and testable on real MySQL
    run_publish_cycle(...)  the one-shot worker entry point

## What "publish" means here

Publication is a *separate, retryable* step from the transaction that wrote the
row (spec §49). That separation is the whole point: the business transaction must
not depend on a broker being up, and a broker that is down must not lose the
event. So the publisher reads committed rows, hands each to a
:class:`OutboxTransport`, and records what happened.

The default transport writes to a Redis Stream. Consumers deduplicate on the
stable outbox row id: a Redis write may succeed while the subsequent database
commit fails, in which case the worker will deliver the same event again.

## Selection: ``FOR UPDATE SKIP LOCKED``

Two workers must be able to run at once without one delivering the other's rows
or blocking on them. ``SKIP LOCKED`` is the primitive for exactly that: a second
worker stepping over a row another worker holds, rather than waiting for it and
then delivering it again. It is also what makes the concurrency property
checkable - a test can start two cycles and assert each row was delivered once.

``next_retry_at`` (spec §49's named column) is what keeps a broken transport from
hot-looping: a failed row is pushed into the future instead of being retried on
the very next scan.

## Retry arithmetic

``attempt_count`` is incremented before delivery in the current transaction.
A process crash rolls that transaction back; the row stays eligible for retry.
Backoff is exponential from
``base_backoff_seconds`` and capped, and ``attempt_count >= max_attempts`` moves
the row to terminal ``DEAD`` - a row that retries forever is an outage nobody is
paged about.

## Who commits

``publish_due`` does not commit; it flushes so the outcome is observable and
leaves the transaction boundary to the caller, exactly as the workflows do.
:func:`run_publish_cycle` is that caller for the worker case and uses
``session_scope``, which commits on success.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.shared.db.base import utc_now
from app.shared.db.models.outbox import MAX_PUBLISH_ATTEMPTS, OutboxMessage, OutboxStatus
from app.shared.db.session import session_scope

__all__ = [
    "DEFAULT_BATCH_SIZE",
    "LoggingTransport",
    "OutboxTransport",
    "PublishOutcome",
    "publish_due",
    "run_publish_cycle",
]

logger = get_logger(__name__)

#: Rows per cycle. Bounded so one worker's transaction is short (spec §27's
#: "short transactions" rule) and so a backlog is worked off in slices rather
#: than in one lock-holding sweep.
DEFAULT_BATCH_SIZE: int = 100

#: First-retry delay. Doubles per attempt up to MAX_BACKOFF_SECONDS.
DEFAULT_BASE_BACKOFF_SECONDS: int = 30
DEFAULT_MAX_BACKOFF_SECONDS: int = 3600


@runtime_checkable
class OutboxTransport(Protocol):
    """Where a published event goes. Raise to signal a retryable failure."""

    def deliver(self, message: OutboxMessage) -> None:
        """Deliver ``message``; raise on failure so the row is retried."""
        ...


class LoggingTransport:
    """Placeholder transport: logs the event and succeeds.

    Honest about being a placeholder - the stack has no broker yet, and inventing
    one here would be Phase 6 scope creep. What it does prove is the contract
    every real transport must meet: ``deliver`` returns on success and raises on
    failure.
    """

    def deliver(self, message: OutboxMessage) -> None:
        logger.info(
            "outbox event delivered",
            event_type=message.event_type,
            aggregate_type=message.aggregate_type,
            aggregate_id=message.aggregate_id,
            attempt=message.attempt_count,
        )


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """What one cycle did. Counts, not a verdict - the caller decides."""

    published: int = 0
    failed: int = 0
    dead: int = 0

    @property
    def claimed(self) -> int:
        return self.published + self.failed + self.dead


def _retry_delay_seconds(
    attempt_count: int,
    *,
    base_backoff_seconds: int,
    max_backoff_seconds: int,
) -> int:
    """Exponential backoff: ``base * 2**(attempt-1)``, capped.

    ``attempt_count`` is 1 on the first failure (it was incremented before the
    attempt), so the first retry waits ``base`` seconds.
    """
    exponent = max(attempt_count - 1, 0)
    return min(base_backoff_seconds * (2**exponent), max_backoff_seconds)


def publish_due(
    session: Session,
    *,
    transport: OutboxTransport,
    now: datetime | None = None,
    limit: int = DEFAULT_BATCH_SIZE,
    max_attempts: int = MAX_PUBLISH_ATTEMPTS,
    base_backoff_seconds: int = DEFAULT_BASE_BACKOFF_SECONDS,
    max_backoff_seconds: int = DEFAULT_MAX_BACKOFF_SECONDS,
) -> PublishOutcome:
    """Claim and deliver up to ``limit`` due events. Does **not** commit.

    Due means: status is ``PENDING`` or ``FAILED``, and ``next_retry_at`` is NULL
    or already passed. Rows are locked ``SKIP LOCKED`` so concurrent workers do
    not contend.
    """
    moment = now or utc_now()

    stmt = (
        select(OutboxMessage)
        .where(
            OutboxMessage.status.in_(
                (OutboxStatus.PENDING.value, OutboxStatus.FAILED.value)
            ),
            or_(
                OutboxMessage.next_retry_at.is_(None),
                OutboxMessage.next_retry_at <= moment,
            ),
        )
        .order_by(OutboxMessage.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    messages = list(session.execute(stmt).scalars())

    published = failed = dead = 0
    for message in messages:
        # Count an attempt in the same transaction as its outcome. A process
        # crash rolls this back and the event remains eligible for retry.
        message.attempt_count += 1
        try:
            transport.deliver(message)
        except Exception as exc:  # noqa: BLE001 - any transport failure is retryable
            message.last_error = str(exc)[:500]
            if message.attempt_count >= max_attempts:
                message.status = OutboxStatus.DEAD.value
                message.next_retry_at = None
                dead += 1
                logger.warning(
                    "outbox event dead",
                    event_type=message.event_type,
                    aggregate_type=message.aggregate_type,
                    aggregate_id=message.aggregate_id,
                    attempts=message.attempt_count,
                    error=message.last_error,
                )
            else:
                message.status = OutboxStatus.FAILED.value
                message.next_retry_at = moment + timedelta(
                    seconds=_retry_delay_seconds(
                        message.attempt_count,
                        base_backoff_seconds=base_backoff_seconds,
                        max_backoff_seconds=max_backoff_seconds,
                    )
                )
                failed += 1
        else:
            message.status = OutboxStatus.PUBLISHED.value
            message.published_at = moment
            message.next_retry_at = None
            message.last_error = None
            published += 1

    # Flush (not commit): the caller owns the transaction boundary, but the
    # outcome must be readable before it closes.
    session.flush()
    return PublishOutcome(published=published, failed=failed, dead=dead)


def run_publish_cycle(
    *,
    transport: OutboxTransport | None = None,
    limit: int = DEFAULT_BATCH_SIZE,
    now: datetime | None = None,
) -> PublishOutcome:
    """Worker entry point: one cycle in its own transaction.

    Uses ``session_scope`` so the status writes commit together or not at all.
    Celery beat schedules this every five seconds (spec §50).
    """
    if transport is None:
        from app.shared.outbox.redis_transport import RedisStreamTransport

        transport = RedisStreamTransport()
    with session_scope() as session:
        return publish_due(session, transport=transport, limit=limit, now=now)
