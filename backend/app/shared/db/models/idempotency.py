"""``idempotency_records`` - the idempotent-write ledger (spec section 48).

Shared, not order-owned, and that is a deliberate layering decision: Phase 5's
payment callbacks need the identical semantics, and a second copy of this table
(or a second module importing ``app.modules.order``) is how two implementations
of "did I already do this?" end up disagreeing.

## What the record is for

A client retries ``POST /orders`` because the network dropped the response. The
server must not create a second order. Two guards, at different layers:

1. the ``Idempotency-Key`` header, resolved through ``UNIQUE (scope,
   idempotency_key)`` here;
2. ``client_request_id`` in the body, enforced by ``UNIQUE (user_id,
   client_request_id)`` on ``orders`` - for the client that lost the header.

## Why the row commits with the order, not before it

The record is inserted and finalised **inside the order's transaction** (spec
section 49: business rows commit together). Two consequences, both wanted:

* a rolled-back create leaves **no key behind**, so a genuine retry after a real
  failure is allowed rather than being poisoned by a key that never produced an
  order;
* a ``COMPLETED`` record and the order it describes can never disagree, because
  neither can be visible without the other.

If the key had its own transaction (a Redis ``SETNX``, say), a crash between the
two writes would leave a permanently claimed key with no order - the retry would
be told "already done" forever.

## ``expires_at`` is retention, not a lock

It bounds how long a key is remembered (spec section 48). It is **not** a lock
TTL: a lock with a TTL can expire while the work is still running, which lets a
second request in and is precisely the bug a correctness lock must not have. The
``IN_PROGRESS`` status is the in-flight marker, and it is cleared only by the
transaction that set it.

## ``response_snapshot`` holds no sensitive data

Spec section 48 is explicit, and the reason is that this row is outside the
order's own redaction path: whoever reads it back gets exactly what was stored.
The order workflow therefore stores a **non-sensitive summary only**
(``order_no``, ``payable_amount``, ``created_at``) - never a receiver name or
phone, never an address.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, CheckConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db.base import Base, PkMixin, TimestampMixin
from app.shared.db.types import BigIntUnsigned, DateTimeMS

__all__ = ["IDEMPOTENCY_STATUSES", "IdempotencyRecord", "IdempotencyStatus"]


class IdempotencyStatus(StrEnum):
    """Lifecycle of one claim on a key.

    ``FAILED`` exists so a recorded failure is distinguishable from a key that
    was never used. A failed record is *not* replayed as a success - the caller
    decides whether the failure is retryable.
    """

    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


IDEMPOTENCY_STATUSES: tuple[str, ...] = tuple(member.value for member in IdempotencyStatus)


class IdempotencyRecord(Base, PkMixin, TimestampMixin):
    """One claimed idempotency key within one scope."""

    __tablename__ = "idempotency_records"
    __table_args__ = (
        # The serialisation point. Two concurrent requests with the same key both
        # try to insert; exactly one wins at the index, and the loser is told to
        # replay the winner's result instead of running the workflow. This is why
        # the guard is a constraint rather than a `SELECT`-then-`INSERT`, which
        # two transactions can interleave.
        UniqueConstraint("scope", "idempotency_key", name="uq_scope_idempotency_key"),
        CheckConstraint(
            "status IN ('IN_PROGRESS','COMPLETED','FAILED')",
            name="status_valid",
        ),
        Index("ix_idempotency_records_expires_at", "expires_at"),
        # "show me every order created with this key" - a support question that
        # should not be a table scan during an incident.
        Index("ix_idempotency_records_resource", "resource_type", "resource_id"),
    )

    #: Namespace, e.g. ``order:create``. Keeps a payment key and an order key from
    #: colliding when a client reuses one UUID for both calls.
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The raw ``Idempotency-Key`` header value. 128 chars accommodates a UUID
    #: and the composite keys some clients build from a session id plus a counter.
    #: (64 + 128) * 4 bytes = 768, comfortably inside InnoDB's 3072-byte index
    #: limit even at utf8mb4's worst case.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: ``sha256`` of the canonicalised business inputs. This is what makes
    #: "same key, different body" detectable at all - the key alone cannot tell a
    #: safe retry from a client bug that reused a key for a different cart.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=IdempotencyStatus.IN_PROGRESS.value, server_default=IdempotencyStatus.IN_PROGRESS.value
    )

    #: Business error code when ``status == FAILED``, NULL otherwise. A *business*
    #: code (10011, 50010, ...) rather than an HTTP status, so a replay does not
    #: have to re-derive which layer produced the failure.
    response_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Non-sensitive summary only (spec section 48). Redacted before write.
    response_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[int | None] = mapped_column(BigIntUnsigned, nullable=True)

    #: Retention bound, NOT a lock TTL - see the module docstring.
    expires_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    @property
    def is_completed(self) -> bool:
        return self.status == IdempotencyStatus.COMPLETED.value

    @property
    def is_in_progress(self) -> bool:
        return self.status == IdempotencyStatus.IN_PROGRESS.value

    def __repr__(self) -> str:
        return f"<IdempotencyRecord scope={self.scope!r} key={self.idempotency_key!r} {self.status}>"
