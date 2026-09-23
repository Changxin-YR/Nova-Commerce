"""After-sales domain models - the customer's claim, its lines, and the money-out record.

    after_sales, after_sale_items, refunds

## Three tables, because a claim and a payment-out are different facts

``after_sales`` is a **business fact**: somebody asked for money back. ``refunds``
is a **money fact**: money moved. They are separate tables because they become true
at different times and can disagree - a claim can be ``APPROVED`` for a month with
no money moved, and an approval that is later refunded in two instalments produces
two ``refunds`` rows against one claim. Storing "approved" on the refund would make
the approval impossible to express before a refund exists; storing the refund on the
claim would cap a claim at one movement.

The third table, ``after_sale_items``, exists for one reason: the per-line refund cap
(FG-12 cap 2). The cap is a *sum over refunds per line* compared against
``order_items.payable_amount``, and it cannot be evaluated at all unless a claim
records **which order lines it covers**. That is what these rows are - the claim's
scope, not a packing list. A refund's per-line placement is computed from them at
refund time (``app/modules/aftersales/refunds.py``) rather than stored, so a
half-refunded claim does not need its own mutable split table.

## The caps, and which of the three is a constraint

PHASE5_DESIGN section 8 makes the split explicit, and it is worth not "tidying"
later:

* ``payments.refunded_amount <= paid_amount`` - a single-row CHECK, **is** a
  database constraint;
* ``orders.refunded_amount <= paid_amount`` - a single-row CHECK, **is** a database
  constraint;
* ``order_items.refunded_amount <= payable_amount`` - a single-row CHECK, **is** a
  database constraint;
* **the per-line cumulative cap** - ``sum(refunds per line) + this share <=
  order_items.payable_amount`` - spans rows, so MySQL cannot express it. It is
  enforced in ``RefundWorkflow`` against freshly locked rows, and FG-12 proves it
  with a negative control.

## What is deliberately *not* a constraint here

``refunded_amount <= approved_amount <= requested_amount`` on ``after_sales`` is
**not** a CHECK, and the design says so on purpose (section 5.5): an operator may
approve more than was requested when policy allows it, which V1 forbids in the
service rather than in the schema. Making it a constraint would turn a policy
decision into a schema decision - and the day a promotional goodwill approval
becomes legal, the constraint is a migration on the hot path instead of a policy
flag. Note the asymmetry with ``amounts_non_negative`` below, which *is* a
constraint: a negative amount is nonsense in every policy, while "approved more
than requested" is a policy question.

``refunds`` carries **no per-line split**. The design gives the row one ``amount``
for the whole claim, and the placement is recomputed from the claim's items + the
approved amount (section 6.2 step 4). A stored split would be a second version of
an allocation that must match ``pricing/allocation.py`` exactly, and two allocators
that disagree produce an invariant that fails only on odd amounts.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.modules.aftersales.enums import (
    AFTER_SALE_CLAIM_STATUSES,
    AFTER_SALE_TYPES,
    REFUND_STATUSES,
    AfterSaleClaimStatus,
    RefundStatus,
)
from app.modules.order.enums import OPERATOR_TYPES
from app.shared.db.base import (
    Base,
    MerchantScopedMixin,
    PkMixin,
    TimestampMixin,
    short_str,
    status_column,
)
from app.shared.db.types import BigIntUnsigned, DateTimeMS, MoneyMinor

__all__ = ["AfterSale", "AfterSaleItem", "Refund"]


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


class AfterSale(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    """The customer's claim (REQ-AFS-001).

    ``claim_status`` is **this row's** lifecycle and is never a synonym for
    ``orders.after_sale_status``. Approving a claim writes this column and touches
    nothing on the order; only ``RefundWorkflow`` moves the order's axis. See
    :mod:`app.modules.aftersales.enums` for why that separation is the point of
    the module.
    """

    __tablename__ = "after_sales"
    __table_args__ = (
        # The public identifier, scoped by merchant: V1's single merchant is a
        # deployment choice, not a modelling one.
        UniqueConstraint("merchant_id", "after_sale_no", name="uq_after_sales_merchant_after_sale_no"),
        # The first idempotency guard: a retried apply with the same key finds this
        # claim instead of opening a second one.
        UniqueConstraint("merchant_id", "idempotency_key", name="uq_after_sales_merchant_idempotency_key"),
        # The second guard: a client that loses its Idempotency-Key header still
        # cannot double-apply. Scoped by user rather than merchant because the
        # request identifier belongs to the client.
        UniqueConstraint("user_id", "client_request_id", name="uq_after_sales_user_client_request"),
        CheckConstraint(f"type IN {_sql_vocabulary(AFTER_SALE_TYPES)}", name="type_valid"),
        CheckConstraint(
            f"claim_status IN {_sql_vocabulary(AFTER_SALE_CLAIM_STATUSES)}",
            name="claim_status_valid",
        ),
        # Every amount non-negative, and all three operands signed BIGINT - the
        # mixed signed/unsigned comparison trap (HANDOFF section 6) applies to
        # every constraint written with arithmetic in it.
        CheckConstraint(
            "requested_amount >= 0 AND approved_amount >= 0 AND refunded_amount >= 0",
            name="amounts_non_negative",
        ),
        # A claim that was refused must say why. The reject endpoint takes a reason
        # and the customer reads it, so a REJECTED row without one is a support
        # ticket rather than a state.
        CheckConstraint(
            "claim_status <> 'REJECTED' OR reject_reason IS NOT NULL",
            name="reject_reason_required",
        ),
        # A claim cannot be completed before it was approved: this pins the two
        # timestamp columns to the only order they can legally occur in, so a
        # workflow that stamps completion on a PENDING claim fails loudly.
        CheckConstraint(
            "completed_at IS NULL OR processed_at IS NOT NULL",
            name="completed_after_processed",
        ),
        Index("ix_after_sales_merchant_status", "merchant_id", "claim_status"),
        Index("ix_after_sales_order_created", "order_id", "created_at"),
        Index("ix_after_sales_user_created", "user_id", "created_at"),
    )

    #: ``NVAS<YYYYMMDD><id:06d>``. Stampable only *after* flush, because it embeds
    #: the auto-increment id - see ``AfterSaleService.apply``.
    after_sale_no: Mapped[str] = mapped_column(short_str(32), nullable=False)

    order_id: Mapped[int] = mapped_column(
        # RESTRICT: a claim is a financial record. Deleting the order must fail
        # loudly rather than take the after-sales trail with it.
        BigIntUnsigned,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: Denormalised for list rendering and triage, so the customer's "my claims"
    #: list and the console queue are one query rather than 1 + 20 joins.
    order_no: Mapped[str] = mapped_column(short_str(32), nullable=False)

    #: The claimant. Ownership is applied **in the query**, never post-load (IDOR
    #: defence, spec section 14.6): a consumer asking for somebody else's claim must
    #: get ``AFTER_SALE_NOT_FOUND`` (80000), never 403 - which would confirm the
    #: claim exists.
    user_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    type: Mapped[str] = mapped_column(status_column(16), nullable=False)
    claim_status: Mapped[str] = mapped_column(
        status_column(16),
        nullable=False,
        default=AfterSaleClaimStatus.PENDING.value,
        server_default=AfterSaleClaimStatus.PENDING.value,
    )

    #: What the customer asked for. Stored as submitted (it is the customer's
    #: statement), never recalculated - the server validates it against the order's
    #: refundable balance at apply time instead.
    requested_amount: Mapped[int] = mapped_column(MoneyMinor, nullable=False)
    #: What an operator granted. Starts at 0 and is only ever written by approve;
    #: ``requested_amount`` is *not* copied into it at apply time, because "asked
    #: for 50, nobody has decided yet" and "granted 50" must not look the same in a
    #: queue.
    approved_amount: Mapped[int] = mapped_column(MoneyMinor, nullable=False, default=0, server_default="0")
    #: Cumulative money actually refunded against this claim, written only by
    #: ``RefundWorkflow``. The claim reaches ``COMPLETED`` when this reaches
    #: ``approved_amount`` - not when the first refund succeeds, because a claim may
    #: be refunded in instalments.
    refunded_amount: Mapped[int] = mapped_column(MoneyMinor, nullable=False, default=0, server_default="0")

    #: Always present: a claim without a reason cannot be triaged.
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    #: A JSON **list** of URLs. JSON because it is a variable-length snapshot of
    #: customer-supplied evidence whose shape we do not own; the service validates it
    #: is a list rather than a document.
    evidence_urls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    #: Required by CHECK when ``claim_status`` is REJECTED.
    reject_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    #: Stamped when an operator decides (approve/reject). NULL while PENDING.
    processed_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    #: Stamped when the claim's approved amount is fully refunded.
    completed_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    #: The client's ``Idempotency-Key``. Required because the apply endpoint requires
    #: the header (design section 7).
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The client's own request identifier - the header-free second guard.
    client_request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``sha256`` of the canonical business inputs, so a key reused for a *different*
    #: claim is detected as a client bug instead of being replayed as the wrong one.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: ``selectin``: the claim detail endpoint and the refund path both need the
    #: claim's lines, and a lazy load there is the N+1 that shows up as a slow queue
    #: page only once somebody claims a five-line order.
    #: ``cascade="all, delete-orphan"``, no ``passive_deletes`` - an ORM
    #: ``session.delete(claim)`` removes its lines, while a raw
    #: ``DELETE FROM after_sales`` is refused by the RESTRICT FKs below. Fixtures can
    #: still clean up; nothing can lose the claim history by accident.
    items: Mapped[list[AfterSaleItem]] = relationship(
        back_populates="after_sale",
        lazy="selectin",
        order_by="AfterSaleItem.id",
        cascade="all, delete-orphan",
    )

    # -- derived ---------------------------------------------------------
    @property
    def is_refundable(self) -> bool:
        """Whether the refund path may act on this claim.

        ``APPROVED`` and ``COMPLETED`` are both refundable in the workflow's sense
        only as a *guard* check (a COMPLETED claim is refused); this property
        answers the narrower question "has it been approved and is there anything
        left", which is what a console action button needs. The authoritative check
        lives in ``RefundWorkflow`` under the row lock - this one drives UI state.
        """
        return (
            self.claim_status == AfterSaleClaimStatus.APPROVED.value
            and self.refunded_amount < self.approved_amount
        )

    @property
    def refundable_amount(self) -> int:
        """What this claim may still pay out.

        Guarded at zero because ``approved_amount`` and ``refunded_amount`` are
        written by different code paths; a transient ordering that made the
        difference negative would otherwise reach an operator as a negative
        refundable balance.
        """
        return max(self.approved_amount - self.refunded_amount, 0)

    def __repr__(self) -> str:
        return (
            f"<AfterSale {self.after_sale_no} {self.type} {self.claim_status} "
            f"requested={self.requested_amount} approved={self.approved_amount}>"
        )


class AfterSaleItem(Base, PkMixin, TimestampMixin):
    """One order line the claim covers - the claim's **scope**.

    Thin on purpose: ``after_sale_id, order_item_id, quantity`` is the frozen shape
    (design section 5.5), plus the two name snapshots that let a claim render
    without joining ``order_items`` (the same INV-014 reasoning as everywhere else:
    a claim must not change because the catalogue was edited after it was filed).

    ``quantity`` is how many units of the line the claim covers, and it is what
    makes a *partial* return expressible: claiming 1 unit of a 3-unit line is a
    different claim from claiming all 3, and the per-line cap is checked against the
    line's payable amount regardless.

    ``UNIQUE (after_sale_id, order_item_id)`` because a duplicate row inside one
    claim would double-count that line's scope - letting a claim cover twice the
    quantity it appears to, while each row individually looked valid.
    """

    __tablename__ = "after_sale_items"
    __table_args__ = (
        UniqueConstraint("after_sale_id", "order_item_id", name="uq_after_sale_items_claim_line"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    after_sale_id: Mapped[int] = mapped_column(
        BigIntUnsigned,
        ForeignKey("after_sales.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: The order line being claimed. RESTRICT: a line referenced by a claim cannot
    #: be deleted out from under it, which is why the claim history survives.
    order_item_id: Mapped[int] = mapped_column(
        BigIntUnsigned,
        ForeignKey("order_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    #: Snapshot of the order line's names, so a claim renders from this row alone.
    product_name: Mapped[str] = mapped_column(short_str(200), nullable=False)
    sku_name: Mapped[str] = mapped_column(short_str(200), nullable=False)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    after_sale: Mapped[AfterSale] = relationship(back_populates="items", lazy="noload")

    def __repr__(self) -> str:
        return f"<AfterSaleItem claim={self.after_sale_id} line={self.order_item_id} qty={self.quantity}>"


class Refund(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    """The money-out record - the fact that money moved (REQ-AFS-002 / FG-12).

    Written by ``RefundWorkflow`` in the same transaction that updates every cap
    counter, which is what makes the FG-12 invariants checkable at the database
    boundary: after the commit, ``payments.refunded_amount``,
    ``orders.refunded_amount``, ``order_items.refunded_amount`` and this row's
    ``amount`` all agree, or the transaction did not happen.

    ``status`` is ``SUCCEEDED`` in the same transaction in V1 (design section 6.2
    step 5). ``PENDING`` exists as a member and is deliberately not the written
    value: there is no asynchronous refund channel, so a ``PENDING`` row nobody
    completes would tell the customer money is coming when nothing is going to send
    it.
    """

    __tablename__ = "refunds"
    __table_args__ = (
        # The public identifier, scoped by merchant: V1's single merchant is a
        # deployment choice, not a modelling one.
        UniqueConstraint("merchant_id", "refund_no", name="uq_refunds_merchant_refund_no"),
        # The first idempotency guard for the `refund:execute` scope: reusing the key
        # must replay the original refund, not send more money (80007 covers the
        # reuse-with-a-different-body case, which the hash detects).
        UniqueConstraint("merchant_id", "idempotency_key", name="uq_refunds_merchant_idempotency_key"),
        CheckConstraint(f"status IN {_sql_vocabulary(REFUND_STATUSES)}", name="status_valid"),
        # A zero refund is refused earlier (REFUND_AMOUNT_INVALID, 80006) because
        # "refund nothing" is a request that looks like a write and is not one; the
        # constraint is here so no code path can store one anyway.
        CheckConstraint("amount > 0", name="amount_positive"),
        # Who executed it, from the frozen operator vocabulary shared with
        # `order_status_logs` - one vocabulary for "who caused this row", not two.
        CheckConstraint(f"operator_type IN {_sql_vocabulary(OPERATOR_TYPES)}", name="operator_valid"),
        Index("ix_refunds_order_created", "order_id", "created_at"),
        Index("ix_refunds_user_created", "user_id", "created_at"),
        Index("ix_refunds_payment_id", "payment_id"),
    )

    #: ``NVR<YYYYMMDD><id:06d>``. Stampable only *after* flush, because it embeds
    #: the auto-increment id.
    refund_no: Mapped[str] = mapped_column(short_str(32), nullable=False)

    after_sale_id: Mapped[int] = mapped_column(
        # RESTRICT and NOT NULL: money moved *because of* a claim, and a refund
        # whose claim vanished is untraceable.
        BigIntUnsigned,
        ForeignKey("after_sales.id", ondelete="RESTRICT"),
        nullable=False,
        # Indexed through the column rather than through an explicit ``Index()``, so
        # the name follows the project convention (``ix_refunds_after_sale_id``)
        # instead of being spelled out a second time where it could drift from the
        # migration.
        index=True,
    )
    order_id: Mapped[int] = mapped_column(
        BigIntUnsigned,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: Denormalised for list rendering, so the console's refund list is one query
    #: rather than 1 + 20 joins.
    order_no: Mapped[str] = mapped_column(short_str(32), nullable=False)

    #: The **money source of truth** for the caps: ``payment.refunded_amount
    #: <= payment.paid_amount`` is checked against this row inside its ``FOR UPDATE``
    #: lock (design section 6.2 step 3). NULLABLE because Phase 4's orders predate
    #: payment records and a legacy/manual adjustment may legitimately have no
    #: payment row to name - and a required FK here would make that adjustment
    #: impossible to record rather than visibly unusual.
    payment_id: Mapped[int | None] = mapped_column(
        BigIntUnsigned,
        ForeignKey("payments.id", ondelete="RESTRICT"),
        nullable=True,
    )

    #: Who received the money back. Same IDOR rule as everywhere else.
    user_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    #: Minor units. Positive, and the ``refund_cap`` CHECKs on ``payments`` /
    #: ``orders`` / ``order_items`` are what stop the *sum* of these from exceeding
    #: what was collected.
    amount: Mapped[int] = mapped_column(MoneyMinor, nullable=False)

    status: Mapped[str] = mapped_column(
        status_column(16),
        nullable=False,
        default=RefundStatus.PENDING.value,
        server_default=RefundStatus.PENDING.value,
    )
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    #: Who executed it. ``operator_type`` is the frozen vocabulary; ``operator_id`` is
    #: the user id when there was one (NULL for a SYSTEM-executed refund), because a
    #: console operator and a background reconciliation are different actors and the
    #: audit trail must say which.
    operator_type: Mapped[str] = mapped_column(status_column(32), nullable=False)
    operator_id: Mapped[int | None] = mapped_column(BigIntUnsigned, nullable=True)

    #: The client's ``Idempotency-Key`` for the ``refund:execute`` scope.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    #: ``sha256`` of the canonical business inputs: the same key with a different
    #: amount is ``REFUND_ALREADY_COMPLETED`` (80007), not a replay.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    completed_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    def __repr__(self) -> str:
        return f"<Refund {self.refund_no} {self.status} amount={self.amount} claim={self.after_sale_id}>"
