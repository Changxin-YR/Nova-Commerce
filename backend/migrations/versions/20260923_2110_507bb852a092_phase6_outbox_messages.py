"""phase6 outbox messages

Revision ID: 507bb852a092
Revises: 3f1ae2c55c54
Create Date: 2026-09-23 21:10:55.361356

Phase 6 (spec §49, ``REQ-CON-003``) adds ``outbox_messages`` - the transactional
outbox. One table, and it is the *whole* schema change: the writer
(``app.shared.outbox.writer``) and the publisher
(``app.shared.outbox.publisher``) are code, not DDL.

## Why the row must commit with the business rows

The outbox exists so that "the order exists" and "``order.created`` is queued"
are one fact rather than two writes with a failure mode between them. That is a
property of *where the insert sits in the caller's transaction*, not of this
migration - but it is why the table has no separate id sequence, no queue table
per event type, and no triggers: the plain row **is** the queue.

## The dedup key, and why it is the aggregate rather than the emitter's key

``UNIQUE (event_type, aggregate_type, aggregate_id)`` - one event of a given type
per aggregate. The seam comment asks for the emitter's own idempotency key, and
that key **is** stored (``idempotency_key``, indexed with ``event_type``), but it
is not the anchor, because an emitter key is only unique inside its own scope: an
order's key is unique within ``idempotency_records`` (a client-supplied string two
users may both send), a refund's per merchant, a payment event's per provider. A
*global* unique on such a key drops a legitimate second event whenever two scopes
pick the same string, and a lost event is invisible while a duplicate is loud. The
aggregate triple cannot collide by construction - ``aggregate_type`` names the
table and ``aggregate_id`` is that table's primary key - and "an order is created
once" / "a refund succeeds once" is exactly the set of facts that must not be
recorded twice.

## Why this file was reviewed rather than trusted

Alembic generated the skeleton and it was read back line by line, for the two
MySQL behaviours recorded in ``HANDOFF.md`` section 6:

* **``compare_type`` does not distinguish BIGINT from BIGINT UNSIGNED.** Both id
  columns here are ``BIGINT UNSIGNED`` (spec §19) and the signedness is invisible
  to autogenerate.
* **A ``CHECK`` constraint is easy to lose.** ``ck_outbox_messages_status_valid``
  *did* come through autogenerate this time (Alembic 1.20 renders model-level
  ``CheckConstraint`` objects), which is exactly why it is re-read from
  ``information_schema`` after ``upgrade`` rather than assumed - the trap is that
  a *change* to an existing CHECK is invisible, and it is cheaper to verify the
  rule than to remember which case is broken.

## Deliberately absent

* **No ``op.drop_index`` in ``downgrade()``.** ``DROP TABLE`` removes a table's
  indexes with it; dropping ``ix_outbox_messages_merchant_id`` separately while
  ``fk_outbox_messages_merchant_id_merchants`` still needs it fails with errno
  1553, and because MySQL DDL is non-transactional that failure leaves the
  database half-downgraded. Phase 4 paid for this lesson (see the Phase 5
  migration's docstring); autogenerate still emits the drops, so they are removed
  by hand here.
* **No per-event-type policy columns.** Routing, delivery guarantees and a
  dead-letter quarantine policy belong to the publisher, not the table. ``DEAD``
  is the only terminal state the schema needs to record.

## Verification

After ``alembic upgrade head`` the following were read back from
``information_schema`` and matched against what is declared here: the eight
columns' types and nullability, the ``CHECK``, the unique key, and the FK::

    SELECT tc.TABLE_NAME, tc.CONSTRAINT_NAME, tc.CONSTRAINT_TYPE
      FROM information_schema.TABLE_CONSTRAINTS tc
     WHERE tc.TABLE_SCHEMA = DATABASE() AND tc.TABLE_NAME = 'outbox_messages'
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.shared.db.types

# revision identifiers, used by Alembic.
revision: str = "507bb852a092"
down_revision: str | None = "3f1ae2c55c54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``outbox_messages``."""
    op.create_table(
        "outbox_messages",
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("aggregate_type", sa.String(length=32), nullable=False),
        sa.Column("aggregate_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="PENDING", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_retry_at", app.shared.db.types.DateTimeMS(), nullable=True),
        sa.Column("published_at", app.shared.db.types.DateTimeMS(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("id", app.shared.db.types.BigIntUnsigned(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            app.shared.db.types.DateTimeMS(),
            server_default=sa.text("now(3)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.shared.db.types.DateTimeMS(),
            server_default=sa.text("now(3)"),
            nullable=False,
        ),
        sa.Column("merchant_id", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.CheckConstraint(
            "status IN ('PENDING','PUBLISHED','FAILED','DEAD')",
            name=op.f("ck_outbox_messages_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_outbox_messages_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_messages")),
        sa.UniqueConstraint(
            "event_type", "aggregate_type", "aggregate_id", name="uq_event_type_aggregate"
        ),
    )
    op.create_index(
        op.f("ix_outbox_messages_created_at"), "outbox_messages", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_outbox_messages_merchant_id"), "outbox_messages", ["merchant_id"], unique=False
    )
    # The publisher's scan: "everything due now, oldest first". Named explicitly
    # (not via op.f) because the name is a deliberate two-column contract, not a
    # single-column convention result.
    op.create_index(
        "ix_outbox_messages_status_next_retry_at",
        "outbox_messages",
        ["status", "next_retry_at"],
        unique=False,
    )
    # The operator's query - "what did we emit for this key?" - not the writer's.
    # The unique above is the correctness anchor; this one exists so an incident
    # grep is not a table scan.
    op.create_index(
        "ix_outbox_messages_event_type_idempotency_key",
        "outbox_messages",
        ["event_type", "idempotency_key"],
        unique=False,
    )


def downgrade() -> None:
    """Drop ``outbox_messages``.

    One statement, on purpose. The indexes and the unique key are removed by
    ``DROP TABLE``; an explicit ``DROP INDEX`` first is what broke the Phase 4
    downgrade with errno 1553 (see the module docstring), so there are none here.
    """
    op.drop_table("outbox_messages")
