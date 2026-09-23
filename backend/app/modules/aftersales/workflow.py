"""``RefundWorkflow`` - the money fact, and the two caps that bound it (FG-12).

    RefundWorkflow

PHASE5_DESIGN section 6.2, in order. Every step below is in the design's order
because the order is the correctness argument, not a style choice:

1. resolve the after-sale row and lock it ``FOR UPDATE``; it must be ``APPROVED`` or
   a partially refunded approved claim;
2. lock the **payment** row (the money source of truth) and the **order** row;
3. **revalidate every cap inside the lock** against freshly read rows - an
   application check made before the lock is not a cap, it is a race two concurrent
   refunds both win;
4. place the money on the claim's lines (pro-rata, remainder last - reusing
   ``pricing.allocation``, never a second allocator) and check the per-line
   cumulative cap;
5. write the ``refunds`` row ``SUCCEEDED``, both money axes, the per-line counters,
   the order's ``payment_status`` and ``after_sale_status``, and the claim's status;
6. append a ``RETURN_IN`` movement **only** for ``RETURN_REFUND``;
7. outbox seam, then one commit.

## Why the idempotency guard is the ``refunds`` unique index, not a second table

The design's step 1 says "claim the ``Idempotency-Key`` through
``IdempotencyRepository``". The data layer instead put
``UNIQUE (merchant_id, idempotency_key)`` **on ``refunds``** and named it, in their
own words, "the first idempotency guard for the ``refund:execute`` scope". This
workflow uses that index, and the reason is the design's own stated intent rather
than a preference:

* the guarantee is *identical* - a unique index and ``idempotency_records``' unique
  index are the same serialisation primitive, and the loser gets the same answer
  ("this key already produced a refund");
* the ``refunds`` index is **stronger** for the property that matters here. A key
  row in another table can exist while the refund it claims does not (a crash
  between the two writes, or a rolled-back claim), and the retry would then be told
  "already done" for money that never moved. With the index on the refund itself,
  the claim *is* the money;
* ``RefundRepository.get_by_idempotency_key`` + the named unique index exist for
  exactly this, so the design's step 1 is satisfied in substance with one fewer
  moving part.

The "claim the key, never pre-check" rule is honoured the same way
``IdempotencyRepository.insert_in_progress`` honours it: the **unique index** is the
decision. The pre-read is only a fast path for the replay, and a concurrent
identical request that slips past it is caught by ``IntegrityError`` on the insert -
see :meth:`RefundWorkflow._existing_refund_for_key`.

## One commit, and it is not here

The method flushes; the commit belongs to the caller (the API layer's session
dependency, per section 49's "business rows commit together"). A commit inside the
workflow would let the refund row become visible before the cap counters it must
agree with.

## What this module deliberately does *not* do

It does not touch ``order_status``. A refund is a money event; the order stays
``PROCESSING``/``COMPLETED`` and only its payment and after-sale axes move
(section 4.4). The one fulfillment fact it does write - ``DELIVERED`` for a fully
returned ``RETURN_REFUND`` - is deliberate and documented at the write site: a
returned parcel *was* delivered.
"""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    AfterSaleNotEligibleError,
    AfterSaleNotFoundError,
    AfterSaleStateInvalidError,
    RefundAlreadyCompletedError,
    RefundAmountInvalidError,
    RefundExceedsItemError,
    RefundExceedsPaidError,
    ValidationError,
)
from app.core.logging import get_logger
from app.modules.aftersales.enums import AfterSaleClaimStatus
from app.modules.aftersales.models import AfterSale, Refund
from app.modules.aftersales.refunds import (
    RefundSplit,
    allocate_refund_across_lines,
    check_per_line_cap,
    movement_quantity_for,
    return_in_movement_key,
)
from app.modules.aftersales.repository import AfterSaleRepository, RefundRepository
from app.modules.identity.enums import PermissionCode
from app.modules.identity.service import Principal
from app.modules.inventory.enums import ReferenceType
from app.modules.inventory.service import InventoryService
from app.modules.order.enums import AfterSaleStatus
from app.modules.order.models import Order, OrderItem
from app.modules.order.repository import OrderRepository
from app.modules.order.workflow import movement_operator
from app.modules.payment.enums import PaymentRecordStatus
from app.modules.payment.models import Payment
from app.modules.payment.repository import PaymentRepository
from app.shared.db.base import utc_now

logger = get_logger(__name__)

__all__ = ["REFUND_EXECUTE_SCOPE", "RefundOutcome", "RefundWorkflow", "refund_no_for"]


#: The idempotency namespace this workflow owns. Frozen by PHASE5_DESIGN section 6.2
#: step 1 and mirrored by ``refunds.uq_refunds_merchant_idempotency_key``.
REFUND_EXECUTE_SCOPE = "refund:execute"

#: Payment statuses on which a refund may still be executed. ``SUCCESS`` (nothing came
#: back yet), ``PARTIAL_REFUNDED`` (something did, more can) and ``REFUNDED`` (all of
#: it did - the amount cap then refuses on its own, so this is not an exception so
#: much as the state in which the cap binds). Anything else means no money was
#: received, and refunding money that never arrived is not a refund.
REFUNDABLE_PAYMENT_STATUSES: tuple[str, ...] = (
    PaymentRecordStatus.SUCCESS.value,
    PaymentRecordStatus.PARTIAL_REFUNDED.value,
    PaymentRecordStatus.REFUNDED.value,
)

#: Claim statuses from which a refund may be executed. Exactly ``APPROVED``.
#:
#: PHASE5_DESIGN section 6.2 step 2 says "it must be ``APPROVED`` or
#: ``PARTIAL_REFUNDED``", but section 4.3's frozen ``AfterSaleClaimStatus`` has **no**
#: ``PARTIAL_REFUNDED`` member, and the Phase 5 migration's ``ck_after_sales_claim_status_valid``
#: agrees: ``("PENDING","APPROVED","REJECTED","CANCELLED","COMPLETED")``. Adding the
#: member would be an enum widening *plus* a hand-written CHECK change on MySQL (Alembic
#: does not autogenerate those) - a captain-level migration decision, not a detail an
#: implementer may assume.
#:
#: The design is self-consistent without it, which is why ``APPROVED`` alone is correct
#: rather than merely convenient: a claim refunded in two instalments keeps
#: ``claim_status = APPROVED`` after the first one (``AfterSaleRepository.apply_refund``
#: only moves it to ``COMPLETED`` when ``refunded_amount`` reaches ``approved_amount``),
#: so it is still refundable. The "partially refunded" *fact* lives in the amounts, not
#: in the status - which is exactly the shape section 6.2's step-5 rule needs.
EXECUTABLE_CLAIM_STATUSES: tuple[str, ...] = (AfterSaleClaimStatus.APPROVED.value,)


def refund_no_for(*, refund_id: int, created_at: datetime) -> str:
    """``NVR<YYYYMMDD><id:06d>`` (PHASE5_DESIGN section 5.5).

    Stamped after the flush that produced the id, exactly like the claim and order
    numbers: the id is the only value with a uniqueness guarantee already, and the
    table's ``UNIQUE (merchant_id, refund_no)`` is what makes the resulting string
    safe to hand a customer.
    """
    return f"NVR{created_at.strftime('%Y%m%d')}{refund_id:06d}"


def request_hash_for_refund(
    *, after_sale_no: str, amount: int, reason: str | None, idempotency_key: str
) -> str:
    """``sha256`` of the canonical inputs of one refund execution.

    The hash is what distinguishes "the same request arrived twice" (replay the
    stored refund) from "this key was reused for a **different** amount"
    (``REFUND_ALREADY_COMPLETED``, 80007). Without it the unique index answers both
    questions with "you already used that key", and a client that reused a key for
    the wrong amount would be told its *intended* refund had succeeded.
    """
    parts = [
        f"after_sale_no={after_sale_no}",
        f"amount={int(amount)}",
        f"reason={reason or ''}",
        f"key={idempotency_key}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class RefundOutcome:
    """The refund that was written, and whether this call is the one that wrote it.

    ``replayed`` exists so the API can answer honestly: a retry of a successful refund
    is a success (idempotent replay), not a conflict, and the caller needs to know
    which of the two happened to decide the response code without re-querying.
    """

    __slots__ = ("refund", "replayed", "split")

    def __init__(self, refund: Refund, *, replayed: bool, split: RefundSplit | None = None) -> None:
        self.refund = refund
        self.replayed = replayed
        self.split = split


class RefundWorkflow:
    """Execute a refund against an approved claim (FG-12).

    Constructed per request with the request's session; the session owns the
    transaction and the caller commits once.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._claims = AfterSaleRepository(session)
        self._refunds = RefundRepository(session)
        self._orders = OrderRepository(session)
        self._payments = PaymentRepository(session)
        self._inventory = InventoryService(session)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    def execute(
        self,
        *,
        principal: Principal,
        after_sale_no: str,
        amount: int,
        idempotency_key: str,
        reason: str | None = None,
    ) -> RefundOutcome:
        """Run the whole refund in the caller's transaction.

        Args:
            principal: the console operator. Must hold ``REFUND_EXECUTE``.
            after_sale_no: the claim being refunded.
            amount: minor units; validated against every cap inside the locks.
            idempotency_key: the ``Idempotency-Key`` (the body copy is matched against
                it by the API layer before this is called).
            reason: optional free text recorded on the refund row.

        Raises:
            AfterSaleNotFoundError: no such claim for this merchant.
            AfterSaleStateInvalidError: the claim is not in a refundable state.
            RefundAmountInvalidError: ``amount <= 0``, above the claim's approval, or
                (for a line) above what that line can still carry.
            RefundExceedsPaidError: cap 1 - the payment cannot cover it.
            RefundExceedsItemError: cap 2 - a line cannot carry its share.
            RefundAlreadyCompletedError: the key was reused for a different amount.
        """
        principal.require_permission(PermissionCode.REFUND_EXECUTE.value)
        if not idempotency_key:
            raise ValidationError("an Idempotency-Key is required to execute a refund")
        if amount <= 0:
            # Checked before the locks: this is a shape error, not a cap decision, and
            # holding locks to reject "refund 0" would block another operator's refund
            # for no reason. The cap decisions below all happen inside the locks.
            raise RefundAmountInvalidError(
                "a refund amount must be positive", context={"amount": amount}
            )

        merchant_id = _merchant_filter(principal)
        request_hash = request_hash_for_refund(
            after_sale_no=after_sale_no,
            amount=amount,
            reason=reason,
            idempotency_key=idempotency_key,
        )

        replayed = self._existing_refund_for_key(
            merchant_id=merchant_id, idempotency_key=idempotency_key, request_hash=request_hash
        )
        if replayed is not None:
            return RefundOutcome(replayed[0], replayed=replayed[1])

        # -- step 1: lock the claim (scoped read first, then the lock) -----
        claim = self._locked_claim(after_sale_no=after_sale_no, merchant_id=merchant_id)
        self._require_executable(claim)

        # -- step 2: lock the payment, then the order, then its lines ------
        payment = self._locked_payment(claim=claim)
        order = self._locked_order(order_id=claim.order_id)
        order_items = self._locked_order_items(order=order)

        # -- step 3+4: revalidate every cap against the freshly locked rows
        line_items = self._claim_lines(claim=claim)
        split = self._validate_caps(
            claim=claim,
            payment=payment,
            order_items=order_items,
            line_items=line_items,
            amount=amount,
        )

        # -- step 5: the money fact ---------------------------------------
        try:
            written = self._write_refund(
                principal=principal,
                claim=claim,
                payment=payment,
                order=order,
                amount=amount,
                reason=reason,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
        except _ReplayDetectedError as replayed_race:
            # A concurrent request with the same key committed first. Its refund is the
            # one that moved the money; this call is an idempotent replay and must not
            # touch a single counter.
            return RefundOutcome(replayed_race.refund, replayed=True)
        if isinstance(written, _ReplayDetectedError):  # defensive: same signal, no raise
            return RefundOutcome(written.refund, replayed=True)
        refund = written
        self._apply_money_axes(order=order, claim=claim, payment=payment, amount=amount)
        self._apply_line_counters(
            order_items=order_items, split=split, amount=amount
        )

        # -- step 6: stock comes back, but only for a return ---------------
        self._credit_returned_stock(
            principal=principal, claim=claim, line_items=line_items, split=split
        )
        self._mark_order_after_sale_axis(order=order)

        # -- step 7: outbox seam ------------------------------------------
        # PHASE 6 OWNS THE OUTBOX TABLE. This is the *seam only*: nothing is written
        # here, and nothing may be written here until Phase 6 lands the table. The
        # consumer will need at least:
        #   event_type  = refund.succeeded
        #   aggregate   = refund / after_sale / order (see the payload below)
        #   payload     = {refund_no, refund_id, after_sale_no, order_no, amount,
        #                  order_refunded_amount, order_paid_amount, payment_status,
        #                  after_sale_status, claim_status}
        #   emitted_in  = this transaction (before the commit below)
        # The idempotency rule Phase 6 must respect: one refund row produces exactly
        # one outbox row. A replayed request never reaches this point - it returns at
        # `_existing_refund_for_key` above - so a retried POST cannot emit twice, and
        # the outbox row must be written with the refund's own idempotency key so a
        # crash-and-retry after this commit is deduplicated the same way the refund is.
        logger.info(
            "refund executed",
            extra={
                "refund_no": refund.refund_no,
                "after_sale_no": claim.after_sale_no,
                "order_no": claim.order_no,
                "amount": amount,
                "order_refunded_amount": order.refunded_amount,
                "order_paid_amount": order.paid_amount,
            },
        )

        self._session.flush()
        # THE one commit (design 6.2 step 7). Everything above - the refund row, both
        # money axes, the per-line counters, the claim's status and the RETURN_IN
        # movements - becomes visible together or not at all. That atomicity is what
        # makes FG-12 checkable at the database boundary: after this returns, the
        # counters and the rows that explain them agree, or nothing happened.
        refund_no, refund_id = refund.refund_no, refund.id
        self._session.commit()
        # ``commit`` expires the instance; re-attach the two identifiers the response
        # needs so the API layer does not pay for a re-read just to render it.
        refund.refund_no, refund.id = refund_no, refund_id
        return RefundOutcome(refund, replayed=False, split=split)

    # ------------------------------------------------------------------
    # Step 1-2: locks
    # ------------------------------------------------------------------
    def _existing_refund_for_key(
        self, *, merchant_id: int, idempotency_key: str, request_hash: str
    ) -> tuple[Refund, bool] | None:
        """Fast-path replay detection; the unique index remains the authority.

        Returns ``(refund, True)`` when this key already produced a refund, ``None``
        when the caller should proceed. A key reuse with a **different** hash is
        ``REFUND_ALREADY_COMPLETED (80007)`` rather than a replay: telling a client
        "succeeded" for an amount it did not ask for is the one wrong answer here.

        The concurrent case is handled in :meth:`_write_refund`, which attempts the
        insert and treats ``IntegrityError`` as "I lost the race" - so a request that
        slips past this read cannot insert a second refund.
        """
        existing = self._refunds.get_by_idempotency_key(
            merchant_id=merchant_id, idempotency_key=idempotency_key
        )
        if existing is None:
            return None
        if existing.request_hash != request_hash:
            raise RefundAlreadyCompletedError(
                "this Idempotency-Key was already used to refund a different amount",
                context={
                    "idempotency_key": idempotency_key,
                    "existing_amount": existing.amount,
                },
            )
        return existing, True

    def _locked_claim(self, *, after_sale_no: str, merchant_id: int) -> AfterSale:
        """Scoped read, then ``FOR UPDATE`` - never a post-load ownership check.

        The repository's locked read takes no scope, so a single locked statement would
        have to verify ownership after loading (the pattern section 14.6 forbids: a post
        load check is one early ``return`` away from leaking existence). Two reads by
        the same unique index cost one extra index lookup and keep the IDOR defence in
        the query.
        """
        scoped = self._claims.get_by_after_sale_no(after_sale_no, merchant_id=merchant_id)
        if scoped is None:
            raise AfterSaleNotFoundError("after-sale claim not found")
        locked = self._claims.get_by_after_sale_no_for_update(after_sale_no)
        if locked is None:  # pragma: no cover - the row cannot vanish mid-request
            raise AfterSaleNotFoundError("after-sale claim not found")
        return locked

    def _require_executable(self, claim: AfterSale) -> None:
        """The claim must be ``APPROVED`` (possibly already partly refunded).

        A ``PENDING`` claim has no approved amount, so there is nothing to pay against;
        ``REJECTED``/``CANCELLED`` were decided against; ``COMPLETED`` has already been
        paid in full. The cap checks would refuse most of these anyway, but they would
        refuse them with a *cap* error, which reads to an operator as "try a smaller
        amount" - and no smaller amount is legal here.
        """
        current = str(claim.claim_status)
        if current not in EXECUTABLE_CLAIM_STATUSES:
            raise AfterSaleStateInvalidError(
                "only an approved claim can be refunded",
                context={"after_sale_no": claim.after_sale_no, "claim_status": current},
            )

    def _locked_payment(self, *, claim: AfterSale) -> Payment:
        """Lock the payment row - the money source of truth (step 2).

        Two statements for the same reason as the claim: the repository's
        ``get_latest_for_order`` is not locked, and there is no "lock the latest for
        this order" method. The id is read first, then the row is locked by primary key.

        ``SUCCESS``/``PARTIAL_REFUNDED``/``REFUNDED`` is required (see
        ``REFUNDABLE_PAYMENT_STATUSES``), and ``paid_amount`` is re-read *under the
        lock* rather than trusted from an earlier read: a concurrent refund on another
        claim of the same order moves it, and cap 1 is exactly the check that must see
        the other refund.
        """
        latest = self._payments.get_latest_for_order(claim.order_id)
        if latest is None:
            raise AfterSaleNotEligibleError(
                "this order has no payment to refund",
                context={"order_no": claim.order_no},
            )
        payment = self._payments.get(latest.id, for_update=True)
        if payment is None:  # pragma: no cover - the row cannot vanish mid-request
            raise AfterSaleNotEligibleError(
                "this order has no payment to refund",
                context={"order_no": claim.order_no},
            )
        if str(payment.status) not in REFUNDABLE_PAYMENT_STATUSES:
            raise AfterSaleNotEligibleError(
                "the payment for this order was never successfully settled",
                context={"order_no": claim.order_no, "payment_status": str(payment.status)},
            )
        return payment

    def _locked_order(self, *, order_id: int) -> Order:
        """Lock the order row (its money and after-sale axes are about to move)."""
        order = self._orders.get(order_id, for_update=True)
        if order is None:  # pragma: no cover - the order cannot vanish mid-request
            raise AfterSaleNotEligibleError("the order for this claim is missing")
        return order

    def _locked_order_items(self, *, order: Order) -> dict[int, OrderItem]:
        """Lock the order's lines, **sorted by id**, and return them by id.

        The sort is the deadlock defence: two refunds on the same order (or a refund
        racing a fulfilment) must acquire line locks in one agreed order, or they
        deadlock instead of serialising. ``refunds.RefundSplit`` is sorted the same way
        for the same reason.
        """
        items = self._orders.items_for(order.id)
        locked: dict[int, OrderItem] = {}
        for item in sorted(items, key=lambda row: row.id):
            reloaded = self._session.get(OrderItem, item.id, with_for_update=True)
            if reloaded is None:  # pragma: no cover - a line cannot vanish mid-request
                raise AfterSaleNotEligibleError("an order line disappeared during the refund")
            locked[reloaded.id] = reloaded
        return locked

    def _claim_lines(self, *, claim: AfterSale) -> list[tuple[int, int]]:
        """The claim's ``(order_item_id, quantity)`` pairs, in id order."""
        return [
            (row.order_item_id, int(row.quantity)) for row in self._claims.items_for(claim.id)
        ]

    # ------------------------------------------------------------------
    # Step 3-4: the caps
    # ------------------------------------------------------------------
    def _validate_caps(
        self,
        *,
        claim: AfterSale,
        payment: Payment,
        order_items: dict[int, OrderItem],
        line_items: list[tuple[int, int]],
        amount: int,
    ) -> RefundSplit:
        """All four checks, in the design's order, against the locked rows.

        Raises on the first violation so the reported code names the binding cap - an
        operator needs to know whether the payment, the approval or a line was the
        limit, because the three have different remedies (a smaller refund, a higher
        approval, or a different claim scope).
        """
        # (a) amount must be positive. Re-checked here as well as at the entry point:
        #     this method is the cap authority, and a future caller that skips the
        #     entry guard must still be refused.
        if amount <= 0:
            raise RefundAmountInvalidError(
                "a refund amount must be positive", context={"amount": amount}
            )

        # (b) cap 1 - the payment cannot give back more than it received.
        if payment.refunded_amount + amount > payment.paid_amount:
            raise RefundExceedsPaidError(
                "this refund would exceed the amount actually paid",
                context={
                    "payment_no": payment.payment_no,
                    "paid_amount": payment.paid_amount,
                    "already_refunded": payment.refunded_amount,
                    "requested_amount": amount,
                    "refundable": max(payment.paid_amount - payment.refunded_amount, 0),
                },
            )

        # (c) the claim cannot give back more than it was approved for.
        if claim.refunded_amount + amount > claim.approved_amount:
            raise RefundAmountInvalidError(
                "this refund would exceed the approved amount for the claim",
                context={
                    "after_sale_no": claim.after_sale_no,
                    "approved_amount": claim.approved_amount,
                    "already_refunded": claim.refunded_amount,
                    "requested_amount": amount,
                },
            )

        # (d) cap 2 - place the money on the lines and check the cumulative per-line
        #     cap. The split is the *same* pro-rata-with-remainder rule the order's
        #     discount allocation uses (see `refunds.py` for why that is not a second
        #     allocator).
        #
        #     The **weights are each line's remaining capacity**
        #     (`payable_amount - refunded_amount`), not its total payable amount, and
        #     that distinction is load-bearing rather than cosmetic. Weighting by the
        #     total makes the shares proportional to it, which ignores how much of each
        #     line has *already* been refunded - so on a second refund the line that was
        #     refunded more heavily first time gets a share sized for a line that is
        #     still empty. Worked example caught by this suite: payables (3333, 3333,
        #     3334) with a first refund of 4000 placed as (1333, 1333, 1334); a second
        #     refund of 6000 weighted by *payables* splits (1999, 1999, 2002) - and line
        #     3, which has only 2000 left, is then asked to carry 2002. The per-line cap
        #     would (correctly) refuse a perfectly legal full refund.
        #
        #     Weights that are the remaining capacities make the shares
        #     `floor(amount * remaining_i / sum(remaining))`, so every line receives at
        #     most its own remaining capacity whenever `amount <= sum(remaining)` - the
        #     regime a legal refund always operates in, because cap 1 bounds `amount` by
        #     what the payment has left to give. They also reduce to the total payables on
        #     the first refund of an order, when every `refunded_amount` is zero.
        if not line_items:
            raise RefundAmountInvalidError(
                "this claim names no lines to place the refund on",
                context={"after_sale_no": claim.after_sale_no},
            )
        missing = [line_id for line_id, _ in line_items if line_id not in order_items]
        if missing:
            raise AfterSaleNotEligibleError(
                "the claim names a line that is not on this order",
                context={"order_no": claim.order_no, "order_item_ids": missing},
            )

        line_payables = {
            order_item_id: int(order_items[order_item_id].payable_amount)
            for order_item_id, _ in line_items
        }
        already_refunded = {
            order_item_id: int(order_items[order_item_id].refunded_amount)
            for order_item_id, _ in line_items
        }
        remaining_capacity = {
            order_item_id: max(line_payables[order_item_id] - already_refunded[order_item_id], 0)
            for order_item_id in line_payables
        }
        # NOTE the comparison: a refund equal to the lines' total remaining capacity IS
        # placeable (`allocate_refund_across_lines` requires `sum(weights) >= amount`, so
        # `>` is the refusal and `==` is the legal full refund).
        if amount > sum(remaining_capacity.values()):
            # Refused before the split, because `allocate_pro_rata` raises a
            # `PricingInvariantError` for a total it cannot place - and that is a pricing
            # error, not a refund one. Translating here keeps the frozen 80005 in the
            # public contract for the case the customer or operator actually caused.
            raise RefundExceedsItemError(
                "this refund exceeds what the claimed lines can still be refunded for",
                context={
                    "after_sale_no": claim.after_sale_no,
                    "requested_amount": amount,
                    "line_remaining_total": sum(remaining_capacity.values()),
                    "line_remaining": remaining_capacity,
                },
            )
        split = allocate_refund_across_lines(
            amount=amount,
            line_payables=[(line_id, remaining_capacity[line_id]) for line_id, _ in line_items],
        )
        check_per_line_cap(
            split=split,
            line_payables=line_payables,
            already_refunded=already_refunded,
        )
        return split

    # ------------------------------------------------------------------
    # Step 5: the writes
    # ------------------------------------------------------------------
    def _write_refund(
        self,
        *,
        principal: Principal,
        claim: AfterSale,
        payment: Payment,
        order: Order,
        amount: int,
        reason: str | None,
        idempotency_key: str,
        request_hash: str,
    ) -> Refund:
        """Insert the ``refunds`` row ``SUCCEEDED``, or answer a lost race.

        An insert is *attempted* rather than preceded by a decisive read, and the
        ``IntegrityError`` is the answer "somebody else used this key" - the same
        reasoning as ``IdempotencyRepository.insert_in_progress``. The insert is wrapped
        in a ``SAVEPOINT`` because SQLAlchemy marks the session as needing a rollback
        after an ``IntegrityError``; without the nested transaction the subsequent
        re-read would raise ``PendingRollbackError`` instead of returning the winner's
        refund.
        """
        operator_type, operator_id = movement_operator(principal)
        now = utc_now()
        try:
            with self._session.begin_nested():
                refund = self._refunds.insert_succeeded(
                    after_sale_id=claim.id,
                    order_id=order.id,
                    order_no=claim.order_no,
                    payment_id=payment.id,
                    user_id=claim.user_id,
                    merchant_id=claim.merchant_id,
                    amount=amount,
                    reason=reason,
                    operator_type=operator_type.value,
                    operator_id=operator_id,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    completed_at=now,
                )
        except IntegrityError:
            # Lost the race. Read what the winner wrote and treat it as a replay, so
            # the client's retry is an idempotent success rather than a 500.
            self._session.expire_all()
            winner = self._refunds.get_by_idempotency_key(
                merchant_id=_merchant_filter(principal), idempotency_key=idempotency_key
            )
            if winner is None:  # pragma: no cover - the index implies it exists
                raise
            if winner.request_hash != request_hash:
                raise RefundAlreadyCompletedError(
                    "this Idempotency-Key was already used to refund a different amount",
                    context={"idempotency_key": idempotency_key, "existing_amount": winner.amount},
                ) from None
            self._session.flush()
            raise _ReplayDetectedError(winner) from None

        refund.refund_no = refund_no_for(refund_id=refund.id, created_at=now)
        self._session.flush()
        return refund

    def _apply_money_axes(
        self, *, order: Order, claim: AfterSale, payment: Payment, amount: int
    ) -> None:
        """Move every money counter and both of the order's axes, in one place.

        The order's ``payment_status`` and ``after_sale_status`` are written **by the
        same rule** (``REFUNDED`` iff everything paid has come back, else
        ``PARTIAL_REFUNDED``) - PHASE5_DESIGN section 6.2 step 5 says so explicitly,
        and two copies of that rule is how one axis starts disagreeing with the other
        for the same order. They are separate columns written separately; they are not
        derived from each other, and ``order_status`` is not touched at all.

        The claim's status comes from ``AfterSaleRepository.apply_refund``, which
        derives ``COMPLETED`` from the claim's own two amounts. The *order* reaching
        ``REFUNDED`` and the *claim* reaching ``COMPLETED`` are two different facts and
        can legitimately happen at different times: a 30-yuan claim on a 100-yuan order
        completes while the order is still ``PARTIAL_REFUNDED``.
        """
        self._payments.apply_refund(payment, amount=amount)
        self._claims.apply_refund(claim, amount=amount)
        order.refunded_amount += amount
        axis = _refund_axis_status(paid=order.paid_amount, refunded=order.refunded_amount)
        order.payment_status = axis
        self._session.flush()

    def _apply_line_counters(
        self, *, order_items: dict[int, OrderItem], split: RefundSplit, amount: int
    ) -> None:
        """Add each line's share to ``order_items.refunded_amount``.

        The ``CHECK (refunded_amount <= payable_amount)`` on ``order_items`` is the
        database half of cap 2, so this is the write that makes a validation bug surface
        as errno 3819 rather than as a silent over-refund.

        The sum of the shares equals ``amount`` by construction
        (``allocate_pro_rata``'s guarantee), which is asserted here while a rollback is
        still free: if the split ever stopped partitioning the refund, the per-line
        counters would no longer explain the order's total, and INV-007's principle ("a
        balance no ledger explains") would be broken in the refunds ledger.
        """
        expected = 0
        for order_item_id, share in split.shares:
            item = order_items[order_item_id]
            item.refunded_amount += share
            expected += share
        if expected != amount:  # pragma: no cover - allocation_pro_rata guarantees it
            raise ValidationError(
                "the refund split does not partition the refund amount",
                context={"amount": amount, "placed": expected},
            )
        self._session.flush()

    def _mark_order_after_sale_axis(self, *, order: Order) -> None:
        """Set ``orders.after_sale_status`` from the same rule as the payment axis."""
        order.after_sale_status = _refund_axis_status(
            paid=order.paid_amount, refunded=order.refunded_amount
        )
        self._session.flush()

    # ------------------------------------------------------------------
    # Step 6: stock
    # ------------------------------------------------------------------
    def _credit_returned_stock(
        self,
        *,
        principal: Principal,
        claim: AfterSale,
        line_items: list[tuple[int, int]],
        split: RefundSplit,
    ) -> None:
        """Append a ``RETURN_IN`` movement per returned line - returns only.

        Two rules decide what happens here, and both are refusals to guess:

        * ``movement_quantity_for`` returns **nothing** for a ``REFUND_ONLY`` claim. The
          customer kept the goods; crediting stock would inflate availability until the
          next stock count found it, which is a shortage that looks like shrinkage and
          is actually a bookkeeping error in the opposite direction.
        * a line that received **no** share of this refund moves no stock either: a
          refund that placed nothing on a line did not buy that line back, and a
          zero-quantity movement would claim otherwise in the ledger.

        The quantity is the **quantity the claim names**, never derived from the money:
        deriving units from an amount would let a full refund of a discounted line return
        more units than were bought.

        ``InventoryService.return_in`` is the call site, not ``adjust``. That method
        owns the ``RETURN_IN`` movement - movement type, ``available_qty += q`` (never
        ``locked_qty``: nothing about a return reserves anything), the cumulative
        ``total_in_qty`` counter and the idempotency check - and it defaults
        ``reference_type`` to ``AFTER_SALE``, which is what makes a return
        distinguishable in the ledger from somebody editing stock by hand. Reaching for
        ``adjust`` instead would have meant re-stating all of that here, and a second
        place that decides what a return looks like in the ledger is a second place for
        INV-007 to be wrong.

        The line's ``warehouse_id`` is passed explicitly because it is "the row that was
        locked" (section 4.2) - re-resolving the *default* warehouse would be right only
        while exactly one warehouse exists.
        """
        operator_type, operator_id = movement_operator(principal)
        for order_item_id, quantity in movement_quantity_for(
            claim_type=str(claim.type), claim_items=line_items, split=split
        ):
            order_item = self._session.get(OrderItem, order_item_id)
            if order_item is None:  # pragma: no cover - lines were locked above
                continue
            key = return_in_movement_key(
                after_sale_no=claim.after_sale_no, order_item_id=order_item_id
            )
            self._inventory.return_in(
                sku_id=order_item.sku_id,
                quantity=quantity,
                idempotency_key=key,
                warehouse_id=order_item.warehouse_id,
                reference_type=ReferenceType.AFTER_SALE,
                reference_id=claim.id,
                operator_type=operator_type,
                operator_id=operator_id,
                reason=f"return received for {claim.after_sale_no}",
            )


def _refund_axis_status(*, paid: int, refunded: int) -> str:
    """``REFUNDED`` iff everything paid has come back, else ``PARTIAL_REFUNDED``.

    One function for **both** of the order's axes, because section 6.2 step 5 defines
    them by the same rule ("by the same rule", in the design's own words). Two copies of
    ``refunded == paid`` is how one axis starts saying ``REFUNDED`` while the other says
    ``PARTIAL_REFUNDED`` for the same order.

    ``paid <= 0`` cannot produce ``REFUNDED``: an order with no payment has nothing to
    have fully refunded, and ``0 >= 0`` is true - the naive comparison would relabel
    every unpaid order as fully refunded.
    """
    if paid > 0 and refunded >= paid:
        return AfterSaleStatus.REFUNDED.value
    return AfterSaleStatus.PARTIAL_REFUNDED.value


class _ReplayDetectedError(Exception):
    """Internal signal: a concurrent request already wrote this refund.

    Raised inside :meth:`RefundWorkflow._write_refund`'s ``IntegrityError`` handler and
    caught in :meth:`RefundWorkflow.execute`, rather than returned, because the handler
    is several call frames deep inside a `try` whose contract is "return a NEW refund".
    An exception keeps that contract honest: nothing silently proceeds with a refund
    object that the database never wrote.
    """

    def __init__(self, refund: Refund) -> None:
        self.refund = refund
        super().__init__("a concurrent request already wrote this refund")


def _merchant_filter(principal: Principal) -> int:
    """The merchant scope for the refund path.

    A refund moves money, so an unscoped caller must be refused rather than allowed to
    operate on whichever rows happen to match: ``None`` would make every query
    unscoped, the one direction a tenant filter must never fail in.
    """
    if principal.merchant_id is None:
        raise ValidationError("this staff account is not attached to a merchant")
    return principal.merchant_id
