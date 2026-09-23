"""``OutboxWriter`` - append an event to the caller's own transaction (spec §49).

    OutboxWriter.enqueue(...)

## The one rule this class exists to enforce

The event row must commit **with** the business rows that caused it, so this
class opens no transaction and calls no ``commit()``. It sits inside whatever
transaction the caller already owns (``CreateOrderWorkflow`` step 9,
``PaymentSuccessWorkflow`` step 10, ``RefundWorkflow`` step 7) and appends a row
to it. If the caller rolls back, the event was never queued - which is correct:
an event about an order that does not exist is a lie, and one about a rollback
is worse than nothing.

The seam comments make the same point in the other direction: the row is placed
*before* the caller's single ``commit()`` and must not be moved above the guards
that decide whether the settlement happened at all.

## Why ``enqueue`` is idempotent rather than "expected to be called once"

The emitters are built so a replay never reaches the emit point (the duplicate
callback is answered in step 1, the duplicate refund returns early). The unique
index ``(event_type, aggregate_type, aggregate_id)`` is therefore a **backstop**,
and a backstop that raises turns a would-be duplicate into a 500 on a retry that
was supposed to be safe. The method instead attempts the insert and treats the
unique violation as "this exact event is already queued" - the same
attempt-don't-pre-check shape as ``IdempotencyRepository.insert_in_progress``,
and for the same reason: a ``SELECT``-then-``INSERT`` cannot be made atomic, and
the database can.

(The unique is keyed on the aggregate rather than on ``idempotency_key`` because
an emitter key is only unique inside its own scope - two merchants can pick the
same string, and a global unique would then drop a legitimate event. See the
model docstring for the full argument.)

## Why a SAVEPOINT

Same trap ``insert_in_progress`` documents: a failed statement on MySQL does not
abort the transaction, but SQLAlchemy does not know that and marks the session
for rollback - so catching the ``IntegrityError`` and continuing on the same
session would raise ``PendingRollbackError`` on the next statement. The nested
scope confines the failure to a SAVEPOINT that can be rolled back on its own, so
the caller's transaction stays healthy and the caller can carry on.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.redaction import assertion_that_no_secret_remains
from app.shared.db.models.outbox import OutboxMessage, OutboxStatus
from app.shared.outbox.contract import OUTBOX_AGGREGATE_TYPES, OUTBOX_EVENT_TYPES

__all__ = ["OutboxWriter"]


class OutboxWriter:
    """Appends domain events to ``outbox_messages`` inside the caller's transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- writes ----------------------------------------------------------
    def enqueue(
        self,
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: int,
        idempotency_key: str,
        payload: dict[str, Any],
        merchant_id: int | None = None,
    ) -> OutboxMessage:
        """Queue one event; return the row (existing one if already queued).

        Never commits. Raises before writing anything if the event vocabulary is
        unknown or the payload carries something sensitive - both are programmer
        errors, and a value that reached the outbox has already been exposed by
        the time anyone could notice.
        """
        if event_type not in OUTBOX_EVENT_TYPES:
            msg = (
                f"unknown outbox event_type {event_type!r}; "
                f"add it to OutboxEventType first (known: {OUTBOX_EVENT_TYPES})"
            )
            raise ValueError(msg)
        if aggregate_type not in OUTBOX_AGGREGATE_TYPES:
            msg = (
                f"unknown outbox aggregate_type {aggregate_type!r} "
                f"(known: {OUTBOX_AGGREGATE_TYPES})"
            )
            raise ValueError(msg)

        # REQ-CON-002's rule, applied at the point where it can still be enforced.
        # A payload that names a secret-bearing key is a bug in the emitter, and
        # the outbox is the last place it can be caught before publication.
        leaks = assertion_that_no_secret_remains(payload)
        if leaks:
            msg = (
                "outbox payload would publish sensitive values; "
                f"offending key paths: {leaks}"
            )
            raise ValueError(msg)

        message = OutboxMessage(
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            idempotency_key=idempotency_key,
            payload=payload,
            merchant_id=merchant_id,
            status=OutboxStatus.PENDING.value,
        )
        try:
            with self._session.begin_nested():
                self._session.add(message)
                self._session.flush()
        except IntegrityError:
            # The unique index answered: this event is already queued. Return the
            # row that won rather than writing a second one. The lookup uses the
            # same triple the unique does - looking a row up by anything else
            # would find nothing and turn a dedup into a crash.
            #
            # ``scalar_one_or_none`` rather than ``scalar_one``: a **concurrent**
            # uncommitted winner is not visible to this session (READ-COMMITTED), so
            # the lookup can legitimately find nothing even though the index refused
            # the insert. Re-raising preserves the real cause - an IntegrityError with
            # the constraint's name - instead of replacing it with a NoResultFound
            # that points at the lookup rather than at the conflict.
            existing = self._session.execute(
                select(OutboxMessage).where(
                    OutboxMessage.event_type == event_type,
                    OutboxMessage.aggregate_type == aggregate_type,
                    OutboxMessage.aggregate_id == aggregate_id,
                )
            ).scalar_one_or_none()
            if existing is None:
                raise
            return existing
        return message
