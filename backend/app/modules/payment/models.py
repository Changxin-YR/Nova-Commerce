"""Payment domain models - the money-in record and the callback that makes it real.

    payments, payment_callbacks

## The two sentences this module exists to make true

PHASE5_DESIGN section 2: **a payment can only become PAID from a verified
provider callback, and that callback is idempotent at the database.** Every
column and constraint below is arranged around those two sentences rather than
around convenience:

* ``payment_callbacks`` carries ``UNIQUE (provider, provider_event_id)`` - a
  *global* unique, not merchant-scoped, because an event id is unique at the
  provider. Two deliveries of the same event therefore cannot both insert, so the
  second one loses **at the index** rather than inside an ``if``. That is the
  FG-11 serialisation point, and it is why the callback handler must *attempt*
  the insert and never ``SELECT``-then-``INSERT`` (a check-then-act race: see
  ``HANDOFF.md`` section 6 and ``CreateOrderWorkflow``'s module docstring).
* ``payments.status`` distinguishes a **record** from the **order's** money axis.
  ``SUCCESS`` is written by exactly one code path inside
  ``PaymentSuccessWorkflow``, and a normal JWT user cannot reach it (INV-008).

## ``UNIQUE (user_id, client_request_id)`` on ``payments`` - the second guard

Same shape as ``orders``: the ``Idempotency-Key`` header is the *first* guard, and
this unique is the second, so a client that loses its header still cannot create
two attempts for one intent. ``request_hash`` is the third piece: it lets the
service tell "the same request arriving twice" (replay the stored response) from
"this key reused for a different amount" (``IDEMPOTENCY_KEY_REUSED_WITH_
DIFFERENT_PAYLOAD``), which a unique violation alone cannot distinguish.

## Signed money, again

Every money column is ``MoneyMinor`` = signed ``BIGINT``. This is not stylistic:
MySQL promotes a mixed signed/unsigned comparison to UNSIGNED, so
``refunded_amount <= paid_amount`` would evaluate its operands in unsigned
arithmetic and reject arithmetically correct rows if either side were unsigned.
FG-09 lost an hour to exactly this on the stock ledger. Every operand in every
constraint below is signed, so no ``CAST`` is needed.

``amount`` therefore carries both ``amount > 0`` (an attempt to pay a
non-positive amount is meaningless and a zero-amount payment is a free order) and
``amount >= 0`` - the latter is subsumed by the former and is deliberately *not*
written, because a redundant constraint is another thing to keep in sync by hand.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.modules.payment.enums import (
    CALLBACK_PROCESS_STATUSES,
    PAYMENT_CHANNELS,
    PAYMENT_RECORD_STATUSES,
    CallbackProcessStatus,
    PaymentRecordStatus,
)
from app.shared.db.base import (
    Base,
    MerchantScopedMixin,
    PkMixin,
    TimestampMixin,
    short_str,
    status_column,
)
from app.shared.db.types import BigIntUnsigned, DateTimeMS, MoneyMinor

__all__ = ["Payment", "PaymentCallback"]


def _sql_vocabulary(values: tuple[str, ...]) -> str:
    """Render a vocabulary tuple as the SQL list of a ``CHECK (col IN (...))``.

    Derived from the Python enum rather than typed out by hand, so adding a member
    cannot leave the database rejecting a value the application believes is legal.
    (Alembic does not autogenerate ``CHECK`` changes on MySQL - see the evidence in
    ``HANDOFF.md`` section 6 - so a hand-typed list here would drift silently and
    only fail at write time in production.)
    """
    rendered = ",".join(f"'{value}'" for value in values)
    return f"({rendered})"


class Payment(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    """One payment attempt against one provider (REQ-PAY-001).

    An order may have several attempts over its life (a failed one, then a
    successful one) but at most one ``SUCCESS`` - the uniqueness of that outcome
    is enforced by the workflow's state guard, not by an index, because the
    *negative control* for FG-11 is precisely that a second, distinct event for
    the same payment is refused by the guard rather than by a unique key.

    ``amount`` must equal ``orders.payable_amount``. That is asserted inside the
    workflow (it spans tables, so no single-row ``CHECK`` can express it) and the
    callback path re-checks the provider's reported amount against *this* row
    (``PAYMENT_AMOUNT_MISMATCH``, 60002).
    """

    __tablename__ = "payments"
    __table_args__ = (
        # The public identifier, scoped by merchant: V1's single merchant is a
        # deployment choice, not a modelling one.
        UniqueConstraint("merchant_id", "payment_no", name="uq_payments_merchant_payment_no"),
        # The first idempotency guard: a retried create with the same key finds
        # this row instead of making a second attempt.
        UniqueConstraint("merchant_id", "idempotency_key", name="uq_payments_merchant_idempotency_key"),
        # The second guard, exactly as on `orders`: a client that loses its
        # Idempotency-Key header still cannot double-submit. Scoped by user rather
        # than merchant because the request identifier belongs to the client.
        UniqueConstraint("user_id", "client_request_id", name="uq_payments_user_client_request"),
        CheckConstraint(f"channel IN {_sql_vocabulary(PAYMENT_CHANNELS)}", name="channel_valid"),
        CheckConstraint(f"status IN {_sql_vocabulary(PAYMENT_RECORD_STATUSES)}", name="status_valid"),
        CheckConstraint("amount > 0", name="amount_positive"),
        # Written with every operand signed, so plain comparison is correct (see
        # the module docstring). `paid_amount` is 0 until SUCCESS and
        # `refunded_amount` is written only by RefundWorkflow, so this single
        # constraint is the database half of FG-12's cap 1.
        CheckConstraint("paid_amount >= 0 AND refunded_amount >= 0", name="amounts_non_negative"),
        # FG-12 cap 1, at the boundary. An operator with a MySQL client cannot
        # refund more than was settled, and no application bug can either.
        CheckConstraint("refunded_amount <= paid_amount", name="refund_cap"),
        Index("ix_payments_merchant_status", "merchant_id", "status"),
        Index("ix_payments_order_created", "order_id", "created_at"),
        Index("ix_payments_user_created", "user_id", "created_at"),
    )

    #: ``NVPAY<YYYYMMDD><id:06d>``. Stampable only *after* flush, because it
    #: embeds the auto-increment id - see ``PaymentService.create``.
    payment_no: Mapped[str] = mapped_column(short_str(32), nullable=False)

    order_id: Mapped[int] = mapped_column(
        # RESTRICT: a payment is a financial record. Deleting the order must fail
        # loudly rather than take the money trail with it.
        BigIntUnsigned,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: Denormalised so a console list renders without joining ``orders`` - the
    #: same decision as ``order_status_logs.order_no``, for the same reason.
    order_no: Mapped[str] = mapped_column(short_str(32), nullable=False)

    #: The payer. Ownership is applied **in the query**, never post-load
    #: (IDOR defence, spec section 14.6): a consumer asking for somebody else's
    #: payment must get ``PAYMENT_NOT_FOUND`` (60000) - never 403, which would
    #: confirm the row exists.
    user_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    #: What the customer owes on this attempt. Must equal
    #: ``orders.payable_amount``; the workflow asserts that across tables.
    amount: Mapped[int] = mapped_column(MoneyMinor, nullable=False)

    status: Mapped[str] = mapped_column(
        status_column(20),
        nullable=False,
        default=PaymentRecordStatus.INITIATED.value,
        server_default=PaymentRecordStatus.INITIATED.value,
    )

    #: The provider's own transaction id. Written **only** by a verified callback,
    #: which is why it is nullable: an INITIATED/PAYING row has none, and a row
    #: that claims one without a SUCCESS status would be the shape of a forged
    #: settlement.
    external_transaction_no: Mapped[str | None] = mapped_column(String(128), nullable=True)

    #: The client's ``Idempotency-Key``. Required (not nullable) because the
    #: create endpoint requires the header.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The client's own request identifier - the header-free second guard.
    client_request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``sha256`` of the canonical business inputs, so a key reused for a
    #: *different* amount is detected as a client bug instead of being replayed
    #: as a success for the wrong money.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: What the provider actually settled. **0 until SUCCESS** - deliberately not
    #: set to ``amount`` at create time, because "what was requested" and "what
    #: arrived" are different facts and the whole point of the amount guard is
    #: that they can disagree.
    paid_amount: Mapped[int] = mapped_column(MoneyMinor, nullable=False, default=0, server_default="0")
    #: Cumulative refunded total. Written only by ``RefundWorkflow``, which
    #: re-validates the caps inside the payment row's ``FOR UPDATE`` lock.
    refunded_amount: Mapped[int] = mapped_column(MoneyMinor, nullable=False, default=0, server_default="0")

    #: The provider's payment window. Phase 6's reconciliation reads it to reach
    #: ``CLOSED``; Phase 5 only records it.
    expires_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    #: ``noload``, and there is deliberately **no** ORM ``relationship()`` between
    #: this table and ``payment_callbacks``. The two are joined by
    #: ``payment_no``, which is not a primary key and has no FK (see
    #: ``PaymentCallback``'s docstring), and SQLAlchemy would need a
    #: ``foreign()``-annotated ``primaryjoin`` to express that - exactly the kind
    #: of implicit join ``app/modules/order/models.py`` refuses for the catalogue,
    #: for the same reason: the join must be asked for explicitly.
    #: ``PaymentCallbackRepository.list_for_payment_no`` is that explicit
    #: request; nothing here loads callbacks as a side effect of reading a payment.

    # -- derived ---------------------------------------------------------
    @property
    def refundable_amount(self) -> int:
        """What may still be refunded on this attempt.

        Guarded at zero because ``paid_amount`` and ``refunded_amount`` are
        written by different code paths; a transient ordering that made the
        difference negative would otherwise reach a customer as a negative
        refundable balance. The ``refund_cap`` CHECK above is the authority - this
        property exists so callers do not each invent their own clamp.
        """
        return max(self.paid_amount - self.refunded_amount, 0)

    @property
    def is_settled(self) -> bool:
        """Whether this attempt is the one that took the money."""
        return self.status == PaymentRecordStatus.SUCCESS.value

    def __repr__(self) -> str:
        return f"<Payment {self.payment_no} {self.channel} {self.status} amount={self.amount}>"


class PaymentCallback(Base, PkMixin, TimestampMixin):
    """One delivery of one provider event - **the** idempotency anchor (REQ-PAY-002/003).

    ``UNIQUE (provider, provider_event_id)`` is the entire reason this table
    exists. It is deliberately **not** merchant-scoped: an event id is unique at
    the provider, and scoping it by merchant would let the same event insert twice
    under two merchants - which is exactly the double-settlement the phase is
    built to prevent.

    No foreign key to ``payments``. This is a deliberate choice and it is
    load-bearing: a callback routinely arrives for a ``payment_no`` that does not
    resolve (a truncated release change, a provider retry after a purge, a probe),
    and that delivery must still be **recorded** as ``FAILED``/``IGNORED`` for
    triage. A FK would make the record impossible to keep, so the audit trail
    would lose precisely the events an operator most needs to read. The join is by
    ``payment_no``. ``merchant_id`` is likewise a denormalised column with no FK.

    ``payload_snapshot`` holds a **sensitive-field filtered** copy of the
    provider's body (design section 5.3). It must remain recognisably the
    provider's payload - a filter that redacts everything destroys the audit value
    - which is why ``providers.filter_callback_payload`` keeps unknown keys and
    replaces only the documented secret-bearing ones.
    """

    __tablename__ = "payment_callbacks"
    __table_args__ = (
        # THE serialisation point. Two concurrent deliveries of one event cannot
        # both insert; the loser gets a duplicate-key error and replays instead.
        UniqueConstraint("provider", "provider_event_id", name="uq_payment_callbacks_provider_event"),
        CheckConstraint(
            f"process_status IN {_sql_vocabulary(CALLBACK_PROCESS_STATUSES)}",
            name="process_status_valid",
        ),
        # A negative attempt count is not a state the retry logic can produce, and
        # `attempt_count` is what an operator reads to decide whether the provider
        # is stuck in a retry loop.
        CheckConstraint("attempt_count >= 1", name="attempt_count_positive"),
        Index("ix_payment_callbacks_payment_no", "payment_no"),
        Index("ix_payment_callbacks_received_at", "received_at"),
        Index("ix_payment_callbacks_process_status", "process_status"),
    )

    #: e.g. ``MOCK``, ``ALIPAY``. The first half of the idempotency key, together
    #: with ``provider_event_id`` below.
    provider: Mapped[str] = mapped_column(status_column(32), nullable=False)
    #: The provider's own event id. The second half of the key.
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Resolved from the payload. **Nullable on purpose**: NULL is the honest
    #: value for a delivery whose ``payment_no`` could not be resolved, and the row
    #: is kept so the unresolved delivery is auditable rather than invisible.
    payment_no: Mapped[str | None] = mapped_column(short_str(32), nullable=True)
    #: Denormalised for triage: reading "which order was this?" should not require
    #: the payment row to still exist.
    order_no: Mapped[str | None] = mapped_column(short_str(32), nullable=True)
    #: Denormalised, no FK - see the class docstring. Set once ``payment_no``
    #: resolves; NULL for a delivery that never resolved.
    merchant_id: Mapped[int | None] = mapped_column(BigIntUnsigned, nullable=True, index=True)

    #: The provider's body, sensitive fields replaced. JSON because it is a
    #: snapshot of somebody else's document whose shape we do not own.
    payload_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    #: Computed **before** insert. An invalid signature never reaches the
    #: workflow, and the row records that it did not - so "was this rejected for
    #: the right reason?" is answerable from the table.
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")

    process_status: Mapped[str] = mapped_column(
        status_column(16),
        nullable=False,
        default=CallbackProcessStatus.RECEIVED.value,
        server_default=CallbackProcessStatus.RECEIVED.value,
    )
    #: The business reason when FAILED (e.g. the error code from
    #: ``app/core/errors.py``). Bounded at 500 so a provider's error text cannot
    #: overflow the column and fail the insert that was meant to record it.
    process_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    #: ``PAYMENT_SUCCEEDED`` etc. Kept as a plain string rather than an enum
    #: because it is the *provider's* vocabulary, not ours: a new provider event
    #: type must be storable without a migration, and the workflow decides what it
    #: means.
    event_type: Mapped[str] = mapped_column(status_column(32), nullable=False)

    received_at: Mapped[datetime] = mapped_column(DateTimeMS, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    #: Starts at 1 and is what makes a retry loop visible rather than suspicious.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    def __repr__(self) -> str:
        return (
            f"<PaymentCallback {self.provider}:{self.provider_event_id} "
            f"{self.process_status} payment_no={self.payment_no}>"
        )
