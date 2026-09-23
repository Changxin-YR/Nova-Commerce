"""After-sales use cases - the claim lifecycle, and nothing about money.

    AfterSaleService

## What this module is *not* allowed to do, and why it is a rule

PHASE5_DESIGN section 4.4 is explicit: **a claim never writes
``orders.after_sale_status``**. Approving a claim changes nothing about the order;
only a refund moves money, and only money movement rolls the order's axis up. The
tempting shortcut - "the customer asked for a refund, so set the order to
PROCESSING" - produces an order that claims to be in an after-sales process before
anybody has agreed to anything, and the two axes can then never disagree truthfully
again. So: this module writes ``after_sales.*`` and ``after_sale_items``. The
``orders`` columns it reads are read-only here, and ``RefundWorkflow`` is the only
writer of the order's money and after-sale axes.

## Eligibility is computed, never trusted

The request body carries ``requested_amount`` and per-line ``quantity``. Both are
**inputs to be checked**, not facts:

* the quantity must be available *on that line* - the line must belong to the order,
  and the units already claimed by *committed* claims must be subtracted;
* the amount must be within a cap the server computes from the order's persisted
  ``payable_amount`` per line (INV-014: yesterday's prices, not today's) and from
  what has already been refunded for the line.

## Rejected and cancelled claims do not consume quantity

A claim that was rejected or cancelled never gave the customer anything, so its units
return to the claimable pool - otherwise a customer who was wrongly rejected would be
permanently unable to re-apply, and the support agent's only remedy would be a manual
database edit. ``COMPLETED`` claims *do* consume quantity, because those units were
refunded and the money is gone.

## Permissions are checked on the service, not only on the route

``AFTER_SALE_READ`` / ``AFTER_SALE_REVIEW`` are checked here: Phase 9's tool gateway
calls services directly, and a check that exists only in a route is a check a
non-HTTP caller never sees (the same reasoning ``OrderService`` documents).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.core.errors import (
    AfterSaleNotEligibleError,
    AfterSaleNotFoundError,
    AfterSaleStateInvalidError,
    IdempotencyInProgressError,
    IdempotencyKeyRequiredError,
    IdempotencyPayloadMismatchError,
    OrderNotFoundError,
    RefundAmountInvalidError,
    ValidationError,
)
from app.core.logging import get_logger
from app.modules.aftersales.enums import AfterSaleClaimStatus, AfterSaleType
from app.modules.aftersales.models import AfterSale, AfterSaleItem
from app.modules.aftersales.repository import AfterSaleRepository
from app.modules.aftersales.schemas import (
    ApplyAfterSaleRequest,
    ApproveAfterSaleRequest,
    CancelAfterSaleRequest,
    RejectAfterSaleRequest,
)
from app.modules.identity.enums import PermissionCode
from app.modules.identity.service import Principal
from app.modules.order.enums import PaymentStatus
from app.modules.order.models import Order
from app.modules.order.repository import IdempotencyRepository, OrderRepository
from app.shared.db.base import utc_now

logger = get_logger(__name__)

__all__ = [
    "AFTER_SALE_APPLY_SCOPE",
    "CLAIMABLE_PAYMENT_STATUSES",
    "LIVE_CLAIM_STATUSES",
    "AfterSaleService",
    "ClaimEligibility",
    "ClaimPage",
    "after_sale_no_for",
    "request_hash_for_claim",
]


#: Scope namespace for claim creation, matching the frozen ``order:create`` /
#: ``refund:execute`` style. A separate namespace from ``refund:execute`` so a client
#: that reuses one UUID for both calls cannot make an idempotent claim masquerade as
#: an idempotent refund.
AFTER_SALE_APPLY_SCOPE = "after-sale:apply"

#: Claim statuses whose units are **spoken for**. Rejected and cancelled claims are
#: absent on purpose (see the module docstring): they returned nothing, so their units
#: go back to the claimable pool.
LIVE_CLAIM_STATUSES: tuple[str, ...] = (
    AfterSaleClaimStatus.PENDING.value,
    AfterSaleClaimStatus.APPROVED.value,
    AfterSaleClaimStatus.COMPLETED.value,
)

#: Payment axes on which an order is claimable: ``PAID`` (nothing refunded yet),
#: ``PARTIAL_REFUNDED`` (something came back, more can) and ``REFUNDED`` (everything
#: came back - the per-line arithmetic then refuses any further claim on its own, so
#: this is not a special case so much as the state in which the caps bind).
#: ``UNPAID``/``PAYING`` are excluded: there is no money to give back.
CLAIMABLE_PAYMENT_STATUSES: tuple[str, ...] = (
    PaymentStatus.PAID.value,
    PaymentStatus.PARTIAL_REFUNDED.value,
    PaymentStatus.REFUNDED.value,
)

#: Order states in which nothing may be claimed. Both terminal states spell "this
#: order is over".
_DEAD_ORDER_STATUSES = frozenset({"CANCELLED", "CLOSED"})


@dataclass(frozen=True, slots=True)
class ClaimEligibility:
    """The server's verdict on one proposed claim, with every figure that produced it.

    Frozen and fully populated rather than a boolean, for two reasons: the refusal
    context needs the numbers (an operator told "not eligible" without the cap is
    being told nothing), and a test can assert the *arithmetic* rather than only the
    refusal - which is the difference between testing the guard and testing
    ``if not eligible``.
    """

    #: Largest total the claim may ask for, in minor units.
    amount_cap: int
    #: ``order_item_id -> units still claimable on that line``.
    claimable_quantity: dict[int, int]
    #: ``order_item_id -> payable_amount``, from the order's persisted snapshot.
    line_payables: dict[int, int]
    #: ``order_item_id -> minor units already refunded`` for the line.
    line_refunded: dict[int, int]

    @property
    def has_capacity(self) -> bool:
        return self.amount_cap > 0 and any(q > 0 for q in self.claimable_quantity.values())


@dataclass(frozen=True, slots=True)
class ClaimPage:
    """One page of claims plus the unpaginated total.

    A named type rather than a ``(rows, total)`` tuple so the API layer cannot unpack
    it in the wrong order - a reversed pair typechecks and renders a list with the
    wrong page count.
    """

    rows: tuple[AfterSale, ...]
    total: int


def after_sale_no_for(*, claim_id: int, created_at: datetime) -> str:
    """``NVAS<YYYYMMDD><id:06d>`` (PHASE5_DESIGN section 5.5).

    Derived from the row's id and date **after** flush, because the id is the only
    thing guaranteed unique - a counter or random suffix would need its own uniqueness
    story, and the table already has one (``UNIQUE (merchant_id, after_sale_no)``).
    The date is the claim's own ``created_at``, not "now": a claim stamped at
    23:59:59.999 must not be numbered with tomorrow's date.
    """
    return f"NVAS{created_at.strftime('%Y%m%d')}{claim_id:06d}"


def request_hash_for_claim(
    *,
    order_no: str,
    claim_type: str,
    requested_amount: int,
    items: list[tuple[int, int]],
) -> str:
    """``sha256`` of the canonical business inputs (the order module's pattern).

    Canonicalisation matters more than the hash function: the same claim must hash
    identically whether the client listed its lines as ``[(7, 1), (3, 2)]`` or
    ``[(3, 2), (7, 1)]``, or a retry would look like a *different* request and be
    answered ``IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD``. Sorting the lines and
    using explicit separators is what makes it order-insensitive and collision-free
    (``"1:23"`` must not collide with ``"12:3"``).
    """
    parts = [
        f"order_no={order_no}",
        f"type={claim_type}",
        f"amount={int(requested_amount)}",
        "items=" + ";".join(f"{int(line_id)}:{int(qty)}" for line_id, qty in sorted(items)),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class AfterSaleService:
    """Apply, cancel, approve, reject, and the two read paths.

    Every mutating method assumes it is running inside a caller-owned transaction (the
    API layer's session dependency commits once per request, per PHASE5_DESIGN section
    49's "business rows commit together"). No method here commits, because a commit
    inside a use case is how half a workflow becomes durable.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._claims = AfterSaleRepository(session)
        self._orders = OrderRepository(session)
        self._idempotency = IdempotencyRepository(session)

    # ------------------------------------------------------------------
    # Eligibility - the shared arithmetic
    # ------------------------------------------------------------------
    def eligibility(
        self,
        *,
        order: Order,
        requested_items: list[tuple[int, int]],
        exclude_after_sale_id: int | None = None,
    ) -> ClaimEligibility:
        """Compute the caps for one proposed claim from persisted rows.

        Args:
            order: the order, with ``items`` loaded (``Order.items`` is ``selectin``,
                so this costs one extra query, not an N+1).
            requested_items: ``(order_item_id, quantity)`` the client asked for.
            exclude_after_sale_id: when re-checking an existing claim, its own
                committed quantity must not count against it.

        Raises:
            AfterSaleNotEligibleError: the order is not in a claimable state, a
                requested line is not on the order, or nothing on the order can still
                be claimed.

        The cap arithmetic, line by line, for a line of ``quantity`` units with
        ``payable_amount`` minor units:

        * ``claimed`` = units on *live* claims (pending/approved/completed) for that
          line, excluding this claim, summed over ``after_sale_items``;
        * ``claimable_units`` = ``quantity - claimed``, floored at 0;
        * ``line_remaining_amount`` = ``payable_amount - refunded_amount`` (the money
          that has not already gone back for this line), floored at 0;
        * the line's **max claimable amount** is ``floor(line_remaining_amount *
          claimable_units / quantity)`` - proportional, because a claim for 1 of 3
          units may not ask for the whole line's money.

        The floor-then-sum convention is the same one the refund split uses when it
        places money back onto lines (``refunds.allocate_refund_across_lines``): the
        two must not disagree, or a claim could be approved for an amount the refund
        path then refuses.
        """
        if str(order.order_status) in _DEAD_ORDER_STATUSES:
            raise AfterSaleNotEligibleError(
                "a cancelled or closed order cannot be claimed against",
                context={"order_no": order.order_no, "order_status": str(order.order_status)},
            )
        if str(order.payment_status) not in CLAIMABLE_PAYMENT_STATUSES:
            raise AfterSaleNotEligibleError(
                "only a paid order can be claimed against",
                context={"order_no": order.order_no, "payment_status": str(order.payment_status)},
            )

        by_id = {item.id: item for item in order.items}
        for order_item_id, _ in requested_items:
            if order_item_id not in by_id:
                raise AfterSaleNotEligibleError(
                    "the claim names a line that is not on this order",
                    context={"order_no": order.order_no, "order_item_id": order_item_id},
                )

        claimed_live = self._live_claimed_quantity(
            order_id=order.id, exclude_after_sale_id=exclude_after_sale_id
        )

        claimable_quantity: dict[int, int] = {}
        line_payables: dict[int, int] = {}
        line_refunded: dict[int, int] = {}
        claimed_amount_cap: dict[int, int] = {}
        amount_cap = 0
        for item in order.items:
            claimed = int(claimed_live.get(item.id, 0))
            remaining_units = max(int(item.quantity) - claimed, 0)
            remaining_amount = max(int(item.payable_amount) - int(item.refunded_amount), 0)
            # The line's own cap, computed once and kept per line so the claim can be measured
            # against the lines it actually names (see below).
            line_cap = (
                remaining_amount * remaining_units // int(item.quantity)
                if remaining_units > 0 and int(item.quantity) > 0
                else 0
            )
            claimable_quantity[item.id] = remaining_units
            line_payables[item.id] = int(item.payable_amount)
            line_refunded[item.id] = int(item.refunded_amount)
            claimed_amount_cap[item.id] = line_cap
            amount_cap += line_cap

        # The cap that applies to a claim is the sum over the lines the claim **names**, not
        # over every line of the order. Summing all lines is the wrong number in two ways: it
        # lets a claim ask for money that only unclaimed lines could carry (which the refund
        # step then has to refuse with a *cap* error, after an operator has approved it), and
        # it hides the case where every named line is exhausted.
        named = [order_item_id for order_item_id, _ in requested_items]
        claimed_cap = sum(claimed_amount_cap[line_id] for line_id in dict.fromkeys(named))
        eligibility = ClaimEligibility(
            amount_cap=claimed_cap,
            claimable_quantity=claimable_quantity,
            line_payables=line_payables,
            line_refunded=line_refunded,
        )
        if not eligibility.has_capacity:
            raise AfterSaleNotEligibleError(
                "there is nothing left to claim on the lines this claim names",
                context={
                    "order_no": order.order_no,
                    "order_amount_cap": amount_cap,
                    "claimed_amount_cap": claimed_cap,
                    "order_item_ids": sorted(set(named)),
                },
            )
        return eligibility

    def _live_claimed_quantity(
        self, *, order_id: int, exclude_after_sale_id: int | None = None
    ) -> dict[int, int]:
        """``order_item_id -> units on live claims`` for one order.

        Computed from ``after_sale_items`` reached through the order's claims rather
        than with a SQL aggregate, so the *set of statuses that count* lives in one
        place (``LIVE_CLAIM_STATUSES``) and is visible in Python where a reviewer can
        check it. The query count is bounded by the order's claim count.
        """
        live = set(LIVE_CLAIM_STATUSES)
        totals: dict[int, int] = {}
        for claim in self._claims.list_for_order(order_id):
            if exclude_after_sale_id is not None and claim.id == exclude_after_sale_id:
                continue
            if str(claim.claim_status) not in live:
                continue
            for row in self._claims.items_for(claim.id):
                totals[row.order_item_id] = totals.get(row.order_item_id, 0) + int(row.quantity)
        return totals

    # ------------------------------------------------------------------
    # Customer: apply
    # ------------------------------------------------------------------
    def apply(
        self,
        *,
        principal: Principal,
        payload: ApplyAfterSaleRequest,
        idempotency_key: str,
    ) -> AfterSale:
        """``POST /after-sales/customer/after-sales`` - file a claim.

        Order of operations, and why:

        1. **Read the key, then claim it.** A replay is answered from the row the
           winner wrote; a fresh request *attempts the insert* and treats the unique
           violation as "I lost the race" (never ``SELECT``-then-``INSERT``, which two
           concurrent retries can both pass - ``HANDOFF.md`` section 6).
        2. **Load the order scoped to the caller.** The scope is applied *in the query*
           (``user_id``), so a foreign order is simply not found -
           ``ORDER_NOT_FOUND (50003)``, never 403 (section 109: a 403 confirms the
           order exists).
        3. **Compute eligibility from the order's own rows**, then check the request
           against it and refuse with the figures.
        4. Write the claim and its lines, then finalise the key, in one transaction.
        """
        if not idempotency_key:
            raise IdempotencyKeyRequiredError("Idempotency-Key header is required")

        items = sorted(
            ((line.order_item_id, line.quantity) for line in payload.items),
            key=lambda pair: pair[0],
        )
        request_hash = request_hash_for_claim(
            order_no=payload.order_no,
            claim_type=payload.type,
            requested_amount=payload.requested_amount,
            items=items,
        )

        existing_record = self._idempotency.get(
            scope=AFTER_SALE_APPLY_SCOPE, idempotency_key=idempotency_key
        )
        if existing_record is not None:
            return self._replayed_claim(existing_record=existing_record, request_hash=request_hash)

        order = self._orders.get_by_order_no_for_update(
            order_no=payload.order_no, user_id=principal.user_id
        )
        if order is None:
            raise OrderNotFoundError("order not found")

        eligibility = self.eligibility(order=order, requested_items=items)

        for order_item_id, quantity in items:
            available = int(eligibility.claimable_quantity.get(order_item_id, 0))
            if quantity > available:
                raise AfterSaleNotEligibleError(
                    "the claim asks for more units than the line can still be claimed for",
                    context={
                        "order_item_id": order_item_id,
                        "requested": quantity,
                        "claimable": available,
                    },
                )
        if payload.requested_amount > eligibility.amount_cap:
            raise AfterSaleNotEligibleError(
                "the requested amount is above what the order can still be refunded for",
                context={
                    "requested_amount": payload.requested_amount,
                    "amount_cap": eligibility.amount_cap,
                },
            )

        record = self._idempotency.insert_in_progress(
            scope=AFTER_SALE_APPLY_SCOPE,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            resource_type="after_sale",
        )
        if record is None:  # pragma: no cover - the read above makes this a real race
            # Lost the insert race against a concurrent identical request whose row is
            # not visible yet. The honest answer is "retry", not a second claim.
            raise IdempotencyInProgressError(
                "an identical claim is already being filed",
                context={"idempotency_key": idempotency_key},
            )

        now = utc_now()
        claim = AfterSale(
            after_sale_no="",  # stamped below, once the id exists
            order_id=order.id,
            order_no=order.order_no,
            user_id=order.user_id,
            merchant_id=order.merchant_id,
            type=AfterSaleType(payload.type).value,
            claim_status=AfterSaleClaimStatus.PENDING.value,
            requested_amount=payload.requested_amount,
            approved_amount=0,
            refunded_amount=0,
            reason=payload.reason,
            description=payload.description,
            evidence_urls=payload.evidence_urls,
            idempotency_key=idempotency_key,
            client_request_id=payload.client_request_id,
            request_hash=request_hash,
        )
        self._claims.add(claim)
        claim.after_sale_no = after_sale_no_for(claim_id=claim.id, created_at=now)

        order_items = {item.id: item for item in order.items}
        for order_item_id, quantity in items:
            source = order_items[order_item_id]
            self._claims.add_item(
                AfterSaleItem(
                    after_sale_id=claim.id,
                    order_item_id=order_item_id,
                    # The line's *snapshot* names, denormalised onto the claim so it
                    # can be rendered without the order (INV-014: values, not a join).
                    product_name=source.product_name,
                    sku_name=source.sku_name,
                    quantity=quantity,
                )
            )

        logger.info(
            "after-sale claim filed",
            extra={
                "after_sale_no": claim.after_sale_no,
                "order_no": order.order_no,
                "type": claim.type,
                "requested_amount": claim.requested_amount,
            },
        )

        self._idempotency.mark_completed(
            record,
            resource_type="after_sale",
            resource_id=claim.id,
            response_code=0,
            # Non-sensitive summary only (section 48): no reason, no description, no
            # evidence URL.
            response_snapshot={
                "after_sale_no": claim.after_sale_no,
                "order_no": order.order_no,
                "requested_amount": claim.requested_amount,
                "created_at": now.isoformat(),
            },
        )
        self._session.flush()
        # One commit for the whole apply: the claim, its lines and the idempotency
        # record become durable together or not at all (section 49). Committing here
        # (rather than in the handler) matches ``OrderService``'s Phase 4 precedent and
        # is what makes a crash leave either everything or nothing - never a key that
        # claims a claim which does not exist.
        after_sale_no = claim.after_sale_no
        self._session.commit()
        # ``commit`` expires the instance, so reading ``claim.after_sale_no`` now would
        # cost a re-read. The identifier is captured above and re-attached so the
        # returned object serialises without a second query.
        claim.after_sale_no = after_sale_no
        return claim

    def _replayed_claim(self, *, existing_record: object, request_hash: str) -> AfterSale:
        """Answer a retried apply from the row the first attempt produced.

        Three outcomes, each with its own frozen code:

        * the key was used for a **different** claim ->
          ``IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD (10011)``. Returning the
          first claim here would hand the client a claim it did not ask for and could
          not tell apart;
        * the first attempt is still running -> ``IDEMPOTENCY_REQUEST_IN_PROGRESS
          (10012)``, so the client retries rather than assuming success or failure;
        * the first attempt completed -> the **same** claim, so a repeated POST is
          harmless and the client can tell "already filed" from "just filed".
        """
        seen = getattr(existing_record, "request_hash", None)
        if seen != request_hash:
            raise IdempotencyPayloadMismatchError(
                "this key was already used with different claim details",
            )
        is_completed = getattr(existing_record, "is_completed", False)
        if not is_completed:
            raise IdempotencyInProgressError(
                "an identical claim is already being filed",
            )
        resource_id = getattr(existing_record, "resource_id", None)
        if resource_id is None:  # pragma: no cover - only after a manual edit
            raise AfterSaleNotFoundError("the claim for this key no longer exists")
        claim = self._claims.get(int(resource_id))
        if claim is None:  # pragma: no cover - only after a manual delete
            raise AfterSaleNotFoundError("the claim for this key no longer exists")
        return claim

    # ------------------------------------------------------------------
    # Customer: cancel
    # ------------------------------------------------------------------
    def cancel(
        self, *, principal: Principal, after_sale_no: str, payload: CancelAfterSaleRequest
    ) -> AfterSale:
        """``POST .../cancel`` - only a ``PENDING`` claim, and only its owner.

        ``PENDING`` only, because this is the *customer's* withdrawal of an unanswered
        ask. Withdrawing an ``APPROVED`` claim is a different act with money already
        promised, and it belongs to the console. ``AFTER_SALE_STATE_INVALID (80002)``
        rather than a silent no-op: a client that believes it cancelled something must
        not be told "OK" when nothing changed.
        """
        claim = self._locked_claim(after_sale_no=after_sale_no, user_id=principal.user_id)
        if str(claim.claim_status) != AfterSaleClaimStatus.PENDING.value:
            raise AfterSaleStateInvalidError(
                "only a pending claim can be cancelled by the customer",
                context={
                    "after_sale_no": after_sale_no,
                    "claim_status": str(claim.claim_status),
                },
            )
        # The customer's own words go in the free-text column the row has
        # (``reject_reason``): an operator reading a CANCELLED claim without them
        # cannot tell a change of mind from a duplicate submission.
        claim.reject_reason = payload.reason
        self._claims.cancel(claim)
        self._session.commit()
        return claim

    # ------------------------------------------------------------------
    # Console: approve / reject
    # ------------------------------------------------------------------
    def approve(
        self,
        *,
        principal: Principal,
        after_sale_no: str,
        payload: ApproveAfterSaleRequest,
    ) -> AfterSale:
        """``POST .../approve`` - the merchant's answer (section 99 task endpoint).

        Approving writes ``claim_status`` and an amount. It writes **no** order column
        and no stock: the money has not moved yet (PHASE5_DESIGN section 4.4), and an
        approval that moved stock would credit returned goods that have not arrived.

        The approved amount may be **lower** than requested, never higher. That cap is
        policy, not arithmetic - which is exactly why the design refuses to put it in a
        ``CHECK`` constraint (section 5.5): a later phase may allow an operator to
        exceed the ask, and that must be a service change rather than a migration. The
        arithmetic caps (what the payment and the lines can carry) are re-checked by
        ``RefundWorkflow`` inside its locks, because an approval is not a refund.

        Re-approval is allowed while ``refunded_amount == 0`` (a correction). Once a
        refund exists, lowering the approval below what has already gone out would make
        the claim's own arithmetic incoherent, so it is refused.
        """
        principal.require_permission(PermissionCode.AFTER_SALE_REVIEW.value)
        claim = self._locked_claim(
            after_sale_no=after_sale_no, merchant_id=_merchant_filter(principal)
        )

        current = str(claim.claim_status)
        if current not in {
            AfterSaleClaimStatus.PENDING.value,
            AfterSaleClaimStatus.APPROVED.value,
        }:
            raise AfterSaleStateInvalidError(
                "only a pending or approved claim can be approved",
                context={"after_sale_no": after_sale_no, "claim_status": current},
            )
        if claim.refunded_amount > 0 and payload.approved_amount < claim.refunded_amount:
            raise RefundAmountInvalidError(
                "the approved amount cannot be lowered below what has already been refunded",
                context={
                    "approved_amount": payload.approved_amount,
                    "refunded_amount": claim.refunded_amount,
                },
            )
        if payload.approved_amount > claim.requested_amount:
            raise RefundAmountInvalidError(
                "an approved amount may not exceed the requested amount in V1",
                context={
                    "approved_amount": payload.approved_amount,
                    "requested_amount": claim.requested_amount,
                },
            )

        self._claims.approve(claim, approved_amount=payload.approved_amount)
        self._session.commit()
        return claim

    def reject(
        self,
        *,
        principal: Principal,
        after_sale_no: str,
        payload: RejectAfterSaleRequest,
    ) -> AfterSale:
        """``POST .../reject`` - terminal, and the reason is mandatory.

        Reachable from ``PENDING`` and from ``APPROVED``: a merchant that approved and
        then discovered a problem must be able to say no, and refusing to allow it
        would push the operator into a manual ``UPDATE`` - which is how a status
        vocabulary stops describing reality. Rejection is **not** reachable once money
        has moved (``refunded_amount > 0``): the customer has the refund, and
        "rejected" next to a completed refund is a contradiction a console renders as
        a support ticket.
        """
        principal.require_permission(PermissionCode.AFTER_SALE_REVIEW.value)
        claim = self._locked_claim(
            after_sale_no=after_sale_no, merchant_id=_merchant_filter(principal)
        )

        current = str(claim.claim_status)
        if current not in {
            AfterSaleClaimStatus.PENDING.value,
            AfterSaleClaimStatus.APPROVED.value,
        }:
            raise AfterSaleStateInvalidError(
                "only a pending or approved claim can be rejected",
                context={"after_sale_no": after_sale_no, "claim_status": current},
            )
        if claim.refunded_amount > 0:
            raise AfterSaleStateInvalidError(
                "a claim that has already been refunded cannot be rejected",
                context={
                    "after_sale_no": after_sale_no,
                    "refunded_amount": claim.refunded_amount,
                },
            )

        self._claims.reject(claim, reject_reason=payload.reject_reason)
        self._session.commit()
        return claim

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get_customer_claim(self, *, principal: Principal, after_sale_no: str) -> AfterSale:
        """One claim, scoped to its owner in the query (never post-load)."""
        claim = self._claims.get_by_after_sale_no(after_sale_no, user_id=principal.user_id)
        if claim is None:
            raise AfterSaleNotFoundError("after-sale claim not found")
        return claim

    def get_admin_claim(self, *, principal: Principal, after_sale_no: str) -> AfterSale:
        principal.require_permission(PermissionCode.AFTER_SALE_READ.value)
        claim = self._claims.get_by_after_sale_no(
            after_sale_no, merchant_id=_merchant_filter(principal)
        )
        if claim is None:
            raise AfterSaleNotFoundError("after-sale claim not found")
        return claim

    def list_customer_claims(
        self,
        *,
        principal: Principal,
        claim_status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> ClaimPage:
        _validate_status_filter(claim_status, "status")
        rows, total = self._claims.list_customer_claims(
            user_id=principal.user_id,
            claim_status=claim_status,
            page=page,
            page_size=page_size,
        )
        return ClaimPage(rows=tuple(rows), total=total)

    def list_admin_claims(
        self,
        *,
        principal: Principal,
        claim_status: str | None = None,
        order_no: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> ClaimPage:
        """The console queue (PHASE5_DESIGN section 7).

        Every filter is validated against the claim vocabulary, so an unmeetable filter
        is a validation error rather than an empty page: an operator who filters on a
        status that does not exist concludes there is no such work and acts on that
        conclusion.
        """
        principal.require_permission(PermissionCode.AFTER_SALE_READ.value)
        _validate_status_filter(claim_status, "status")
        if order_no is not None and not order_no.strip():
            raise ValidationError("order_no filter must not be blank")

        rows, total = self._claims.list_admin_claims(
            merchant_id=_merchant_filter(principal),
            claim_status=claim_status,
            order_no=order_no,
            page=page,
            page_size=page_size,
        )
        return ClaimPage(rows=tuple(rows), total=total)

    def items_of(self, claim: AfterSale) -> list[AfterSaleItem]:
        """The claim's lines, in ``order_item_id`` order (the split's read order)."""
        return self._claims.items_for(claim.id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _locked_claim(
        self, *, after_sale_no: str, user_id: int | None = None, merchant_id: int | None = None
    ) -> AfterSale:
        """Load a claim ``FOR UPDATE``, scoped, or raise the frozen 404.

        The lock is the point: the status guard and the write must happen while the row
        is held - PHASE5_DESIGN section 6.2's "decide inside the lock, not before it".
        Two concurrent approvals that both read ``PENDING`` outside a lock would both
        decide "allowed" and both write.

        Two statements rather than one, deliberately: the repository's
        ``get_by_after_sale_no_for_update`` takes no scope filter, so a single locked
        read would have to check ownership *after* loading - exactly the post-load
        pattern section 14.6 forbids. The scoped read answers "may this caller touch
        this row"; only then is the row locked.
        """
        scoped = self._claims.get_by_after_sale_no(
            after_sale_no, user_id=user_id, merchant_id=merchant_id
        )
        if scoped is None:
            raise AfterSaleNotFoundError("after-sale claim not found")
        locked = self._claims.get_by_after_sale_no_for_update(after_sale_no)
        if locked is None:  # pragma: no cover - the row cannot vanish mid-request
            raise AfterSaleNotFoundError("after-sale claim not found")
        return locked


def _merchant_filter(principal: Principal) -> int:
    """The merchant scope for a console query.

    ``ConsolePrincipal`` (the API dependency) already requires a staff account with a
    merchant, so ``None`` here means a caller bypassed the dependency. Narrowing to
    ``None`` would make the query unscoped - the one direction a tenant filter must
    never fail in - so it is refused instead.
    """
    if principal.merchant_id is None:
        raise ValidationError("this staff account is not attached to a merchant")
    return principal.merchant_id


def _validate_status_filter(value: str | None, field: str) -> None:
    """Refuse a claim-status filter that can never match (the order module's pattern)."""
    if value is None:
        return
    allowed = tuple(member.value for member in AfterSaleClaimStatus)
    if value not in allowed:
        raise ValidationError(
            f"{field} must be one of {list(allowed)}",
            context={field: value},
        )
