"""``outbox_messages`` - the transactional outbox (spec section 49).

    outbox_messages

REQ-CON-003, verbatim: *"Transactional outbox: business rows + outbox row commit
in ONE transaction; worker publishes afterwards; retry with next_retry_at"*.

## Why this table exists at all

A workflow that writes a business row and then calls a broker has two writes and
one failure mode: the row commits and the message never leaves, or the message
leaves and the row rolls back. Neither is detectable afterwards. The outbox makes
the message a **row in the same database**, written by the caller's own
transaction, so "the order exists" and "order.created is queued" are the same
fact. Publication becomes a *separate, retryable* step that can fail as often as
it likes without losing the intent.

That is why :class:`~app.shared.outbox.writer.OutboxWriter` never commits and
never opens its own transaction: a row in its own transaction is exactly the
message-not-recorded bug the pattern exists to remove.

## The uniqueness rule (``UNIQUE (event_type, aggregate_type, aggregate_id)``)

**One event of a given type per aggregate.** The dedup key is the event's own
identity - ``(order.created, order, 17)`` - rather than the emitter's idempotency
key. That is a deliberate departure from a first reading of the seam comment
(*"the outbox row must be written with the refund's own idempotency key"*), and
the reason is that an emitter key is only unique **inside its own scope**:

* an order's ``Idempotency-Key`` is unique within ``idempotency_records``
  (``scope = order:create``), but it is still a client-supplied string and two
  users may legitimately send the same ``"abc"``;
* a refund's idempotency key is unique per **merchant**;
* a payment event's natural key is the provider's event id, unique per
  **provider**.

A *global* unique on a string like that silently drops a legitimate second event
whenever two scopes happen to pick the same value. That false-collision direction
is strictly worse than a duplicate: a duplicate is loud, a lost event is
invisible. Keying on the aggregate cannot collide - ``aggregate_type`` names the
table and ``aggregate_id`` is that table's primary key, so the triple is unique
by construction - and "an order is created once" / "a refund succeeds once" is
exactly the set of facts that must not be recorded twice.

``idempotency_key`` is still stored (indexed alongside ``event_type``), because it
is what an operator greps during an incident and because the seam comment asks
for it. It is traceability, not the anchor.

A fresh uuid would be worse than either: it makes every replay emit a second row
(spec §44's "no duplicate outbox side effect").

## ``status`` and ``next_retry_at``

``PENDING`` -> ``PUBLISHED`` is the happy path. A failed delivery moves the row to
``FAILED`` with a ``next_retry_at`` in the future, so the next scan skips it
rather than hot-looping on a broken transport. ``attempt_count`` starts at 0 (it
counts *delivery attempts*, and a row that has never been handed to a transport
has had none - the ``payment_callbacks`` table starts at 1 because that row only
exists *because of* a delivery). ``DEAD`` is terminal: ``MAX_PUBLISH_ATTEMPTS``
failures are a broken integration, and a row that retries forever is an outage
nobody is paged about.

## ``payload`` is non-sensitive (spec §48's rule, applied here)

This row leaves the database for a broker, a log line, and possibly a third
party, so it carries the same discipline as ``idempotency_records.response_
snapshot``: a **summary** of what happened (identifiers, amounts, statuses), never
a token, a signature, a phone number or an address. ``REQ-CON-002`` states the rule
for snapshots; an event payload is *more* exposed than a snapshot, not less.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db.base import Base, MerchantScopedMixin, PkMixin, TimestampMixin, status_column
from app.shared.db.types import BigIntUnsigned, DateTimeMS

__all__ = [
    "MAX_PUBLISH_ATTEMPTS",
    "OUTBOX_STATUSES",
    "OutboxMessage",
    "OutboxStatus",
]

#: Terminal retry bound. Exposed as a module constant so the publisher and the
#: tests read one number rather than two literals that can disagree.
MAX_PUBLISH_ATTEMPTS: int = 5


class OutboxStatus(StrEnum):
    """Lifecycle of one queued event.

    ``StrEnum`` for the same reason as every other status in this project: the
    values are stored verbatim in a ``VARCHAR`` that also carries a hand-written
    ``CHECK (status IN (...))``, so the Python vocabulary and the SQL vocabulary
    must be byte-identical.
    """

    #: Written with the business rows; not yet handed to a transport.
    PENDING = "PENDING"
    #: Delivered. Terminal, and the only state a healthy row ends in.
    PUBLISHED = "PUBLISHED"
    #: A delivery attempt failed; will be retried at ``next_retry_at``.
    FAILED = "FAILED"
    #: ``MAX_PUBLISH_ATTEMPTS`` attempts failed. Terminal; needs a human.
    DEAD = "DEAD"


#: Vocabulary tuple in the exact order the migration's ``CHECK`` lists it.
#: Derived from the enum so a new member cannot be added in Python while the
#: database still rejects it (the "Alembic does not autogenerate CHECK changes on
#: MySQL" trap - HANDOFF section 6).
OUTBOX_STATUSES: tuple[str, ...] = tuple(member.value for member in OutboxStatus)


class OutboxMessage(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    """One queued domain event, committed with the business rows that caused it."""

    __tablename__ = "outbox_messages"
    __table_args__ = (
        # The dedup key. See the module docstring: the *aggregate* identity, not
        # the emitter's scope-local key, so two scopes cannot collide and a replay
        # still loses at the index instead of emitting a second row.
        UniqueConstraint(
            "event_type",
            "aggregate_type",
            "aggregate_id",
            name="uq_event_type_aggregate",
        ),
        CheckConstraint(
            "status IN ('PENDING','PUBLISHED','FAILED','DEAD')",
            name="status_valid",
        ),
        # The publisher's scan: "everything due now, oldest first". Without this
        # index the worker table-scans on every tick and degrades as the outbox
        # grows, which is the failure mode an outbox is supposed to prevent.
        Index("ix_outbox_messages_status_next_retry_at", "status", "next_retry_at"),
        # The operator's query, not the writer's: "what did we emit for this key?"
        # during an incident. The unique above is the correctness anchor; this is
        # for a human grep that should not be a table scan.
        Index(
            "ix_outbox_messages_event_type_idempotency_key",
            "event_type",
            "idempotency_key",
        ),
    )

    #: Our vocabulary (``order.created``, ``payment.settled``,
    #: ``refund.succeeded``) - see :class:`~app.shared.outbox.contract.OutboxEventType`.
    #: A plain ``String`` rather than a MySQL ``ENUM`` so a new event type is an
    #: ordinary migration, not a table rewrite.
    event_type: Mapped[str] = mapped_column(status_column(64), nullable=False)

    #: What the event is about: ``order`` / ``payment`` / ``refund`` / ...
    aggregate_type: Mapped[str] = mapped_column(status_column(32), nullable=False)
    #: The aggregate's own id. ``BIGINT UNSIGNED`` to match every other id here.
    aggregate_id: Mapped[int] = mapped_column(BigIntUnsigned, nullable=False)

    #: The emitter's idempotency key, verbatim - **not** a fresh uuid. See the
    #: module docstring; this is the column the whole dedup argument rests on.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Non-sensitive summary only (module docstring). JSON because each event
    #: type has its own shape and none of them is a table.
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    status: Mapped[str] = mapped_column(
        status_column(16),
        nullable=False,
        default=OutboxStatus.PENDING.value,
        server_default=OutboxStatus.PENDING.value,
    )

    #: Starts at 0: this counts *delivery attempts*, and a row that has never been
    #: handed to a transport has had none. (Contrast ``payment_callbacks``, whose
    #: row exists only because of a delivery and therefore starts at 1.)
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    #: When the row becomes eligible for the next attempt. NULL means "now" -
    #: the state a freshly written PENDING row is in, so the happy path never
    #: has to invent a timestamp.
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    #: Bounded at 500 for the same reason as ``payment_callbacks.process_error``:
    #: a transport's error text must not overflow the column and fail the very
    #: write that was meant to record the failure.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    @property
    def is_pending(self) -> bool:
        return self.status in (OutboxStatus.PENDING.value, OutboxStatus.FAILED.value)

    def __repr__(self) -> str:
        return (
            f"<OutboxMessage {self.event_type} {self.aggregate_type}#{self.aggregate_id} "
            f"{self.status} attempts={self.attempt_count}>"
        )
