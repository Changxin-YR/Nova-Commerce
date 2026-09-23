"""After-sales data access (spec section 18: a repository does data access, nothing else).

    AfterSaleRepository, RefundRepository

No eligibility decision (``AFTER_SALE_NOT_ELIGIBLE``), no cap arithmetic, no claim
state decision lives here. Whether an order's lines are still claimable is decided
by ``AfterSaleService`` from the order's refundable balance; whether a refund
exceeds either cap is decided by ``RefundWorkflow`` **against freshly locked rows**
(design section 6.2 step 4). This module answers "what is in the database" and
"write this row".

## The locks

:meth:`AfterSaleRepository.get_for_update` and
:meth:`RefundRepository.insert_pending`'s caller-side lock order are the reason the
FG-12 caps hold under concurrency. Following the rule generalised in
``HANDOFF.md`` section 7 - **decide inside the lock, not before it** - the workflow
locks the claim, then the payment, then the order lines, and re-reads every counter
it is about to compare. A cap validated against a value read *before* the lock is
not a cap; it is a race that two concurrent refunds both win.

The lock acquisition order (claim -> payment -> order lines **sorted by
order_item_id**) is fixed here rather than left to each caller: ``refunds.RefundSplit``
already sorts its shares by ``order_item_id`` for exactly this reason, and two
refunds that acquired line locks in different orders would deadlock instead of
serialising.

## Read-back helpers exist so the workflow writes no SQL

``mark_*`` methods do **not** commit: ``RefundWorkflow`` owns exactly one commit
(design section 6.2 step 7), and a repository that committed would split the money
movement from the cap counters it must agree with.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.modules.aftersales.enums import AfterSaleClaimStatus, RefundStatus
from app.modules.aftersales.models import AfterSale, AfterSaleItem, Refund
from app.shared.db.base import utc_now

__all__ = ["AfterSaleRepository", "RefundRepository"]


class AfterSaleRepository:
    """Queries over ``after_sales`` / ``after_sale_items`` plus the write paths."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- writes ----------------------------------------------------------
    def add(self, claim: AfterSale) -> AfterSale:
        """Persist a new claim and flush so its id is available.

        The flush is load-bearing: ``after_sale_no`` is stamped from the
        auto-increment id afterwards, so the service needs the id before it can build
        the identifier. It happens inside the caller's transaction, which is why
        ``uq_after_sales_merchant_after_sale_no`` can be relied on - nothing is
        committed until the whole apply succeeds.
        """
        self._session.add(claim)
        self._session.flush()
        return claim

    def add_item(self, item: AfterSaleItem) -> AfterSaleItem:
        self._session.add(item)
        self._session.flush()
        return item

    def approve(
        self, claim: AfterSale, *, approved_amount: int, processed_at: datetime | None = None
    ) -> AfterSale:
        """Move a locked claim to ``APPROVED`` for ``approved_amount``.

        Takes the row rather than an id, because the caller must be holding its
        ``FOR UPDATE`` lock. Does **not** validate that the amount is positive or
        no larger than ``requested_amount``: V1 forbids the latter in the service
        rather than in a constraint (design section 5.5), and a re-check here would
        be a second place for that policy to live.
        """
        claim.claim_status = AfterSaleClaimStatus.APPROVED.value
        claim.approved_amount = approved_amount
        claim.processed_at = processed_at or utc_now()
        claim.reject_reason = None
        self._session.flush()
        return claim

    def reject(
        self,
        claim: AfterSale,
        *,
        reject_reason: str,
        processed_at: datetime | None = None,
    ) -> AfterSale:
        """Move a locked claim to ``REJECTED`` with the reason the customer reads.

        ``reject_reason`` is required by the ``reject_reason_required`` CHECK, and
        truncated here rather than at the column so a provider-length explanation
        cannot fail the write that was meant to record it.
        """
        claim.claim_status = AfterSaleClaimStatus.REJECTED.value
        claim.reject_reason = reject_reason[:500]
        claim.processed_at = processed_at or utc_now()
        self._session.flush()
        return claim

    def cancel(self, claim: AfterSale) -> AfterSale:
        """Withdraw a ``PENDING`` claim at the customer's request.

        ``processed_at`` is deliberately **not** stamped: nobody processed anything,
        and a CANCELLED claim carrying a processing timestamp would look like an
        operator decision in a queue report.
        """
        claim.claim_status = AfterSaleClaimStatus.CANCELLED.value
        self._session.flush()
        return claim

    def apply_refund(
        self, claim: AfterSale, *, amount: int, completed_at: datetime | None = None
    ) -> AfterSale:
        """Add ``amount`` to the claim's refunded total, completing it when full.

        Called by ``RefundWorkflow`` under the claim's ``FOR UPDATE`` lock. The
        transition to ``COMPLETED`` is derived from the two amounts here rather than
        passed in, because "the claim's own refunded total reached its approved
        amount" is the frozen rule (design section 6.2 step 5) - and a
        caller-supplied status is a second place for it to be wrong. The *order's*
        axis is a separate write done by the workflow; this method never touches it.
        """
        claim.refunded_amount += amount
        if claim.approved_amount > 0 and claim.refunded_amount >= claim.approved_amount:
            claim.claim_status = AfterSaleClaimStatus.COMPLETED.value
            claim.completed_at = completed_at or utc_now()
        self._session.flush()
        return claim

    # -- reads -----------------------------------------------------------
    def get(self, after_sale_id: int, *, for_update: bool = False) -> AfterSale | None:
        """Load one claim by primary key.

        ``for_update`` is a keyword rather than a second method so a caller cannot
        accidentally use the unlocked variant on a write path without the parameter
        being visible in the diff.
        """
        if not for_update:
            return self._session.get(AfterSale, after_sale_id)
        stmt = select(AfterSale).where(AfterSale.id == after_sale_id).with_for_update()
        return self._session.execute(stmt).scalars().first()

    def get_by_after_sale_no(
        self,
        after_sale_no: str,
        *,
        merchant_id: int | None = None,
        user_id: int | None = None,
        for_update: bool = False,
    ) -> AfterSale | None:
        """Resolve a claim by its public identifier.

        ``user_id``/``merchant_id`` are applied **in the query**, not checked after
        loading. That ordering is the IDOR defence (spec section 14.6): a consumer
        asking for somebody else's claim must get ``AFTER_SALE_NOT_FOUND`` (80000),
        and the only way to be certain that happens is for the foreign row never to
        be fetched. A post-load ownership check is one early ``return`` away from
        leaking existence.
        """
        stmt = select(AfterSale).where(AfterSale.after_sale_no == after_sale_no)
        if merchant_id is not None:
            stmt = stmt.where(AfterSale.merchant_id == merchant_id)
        if user_id is not None:
            stmt = stmt.where(AfterSale.user_id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self._session.execute(stmt).scalars().first()

    def get_by_after_sale_no_for_update(self, after_sale_no: str) -> AfterSale | None:
        """Lock a claim by its public identifier (section 27 rule, step 2 of FG-12).

        Named as its own method, rather than left to the ``for_update`` flag, so a
        reviewer can see at the call site that the refund path takes this lock
        **before** it reads any cap counter.
        """
        return self.get_by_after_sale_no(after_sale_no, for_update=True)

    def get_by_client_request_id(
        self, *, user_id: int, client_request_id: str, for_update: bool = False
    ) -> AfterSale | None:
        """The second idempotency guard: find the claim a retry already created.

        Returns the row regardless of ``request_hash``; the caller compares the hash
        and raises ``IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD`` when it differs.
        Keeping the comparison out of the query is deliberate - a hash mismatch and a
        missing row are different failures with different business codes.
        """
        stmt = select(AfterSale).where(
            AfterSale.user_id == user_id,
            AfterSale.client_request_id == client_request_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self._session.execute(stmt).scalars().first()

    def get_by_idempotency_key(
        self, *, merchant_id: int, idempotency_key: str, for_update: bool = False
    ) -> AfterSale | None:
        """The first idempotency guard, as a read.

        Used for the replay path *after* the service has attempted the insert - not
        instead of it. A ``SELECT`` before the ``INSERT`` is the check-then-act race
        the design forbids (``HANDOFF.md`` section 6).
        """
        stmt = select(AfterSale).where(
            AfterSale.merchant_id == merchant_id,
            AfterSale.idempotency_key == idempotency_key,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self._session.execute(stmt).scalars().first()

    def list_for_order(self, order_id: int) -> list[AfterSale]:
        """Every claim on one order, oldest first.

        Used by the eligibility check: "is there anything left to claim?" is a
        question about the sum over this list, not about the newest row.
        """
        stmt = (
            select(AfterSale)
            .where(AfterSale.order_id == order_id)
            .order_by(AfterSale.created_at.asc(), AfterSale.id.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def list_for_order_item(self, order_item_id: int) -> list[AfterSaleItem]:
        """Claim lines covering one order line, oldest first.

        The per-line cumulative cap is a sum over these, which is why the query
        exists rather than being inlined into a workflow.
        """
        stmt = (
            select(AfterSaleItem)
            .where(AfterSaleItem.order_item_id == order_item_id)
            .order_by(AfterSaleItem.id.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def open_claim_for_order(self, order_id: int) -> AfterSale | None:
        """The claim on this order that could still be approved, if any.

        "Open" is ``PENDING`` or ``APPROVED`` - the two states a console queue acts
        on. Used to refuse a second concurrent claim where the service decides that
        is right; nothing here enforces it, because whether a customer may file a
        second claim is a policy question (a split return is legitimate).
        """
        stmt = (
            select(AfterSale)
            .where(
                AfterSale.order_id == order_id,
                AfterSale.claim_status.in_(
                    [
                        AfterSaleClaimStatus.PENDING.value,
                        AfterSaleClaimStatus.APPROVED.value,
                    ]
                ),
            )
            .order_by(AfterSale.created_at.desc(), AfterSale.id.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalars().first()

    def items_for(self, after_sale_id: int) -> list[AfterSaleItem]:
        """The claim's lines, ordered by id - the order the refund split reads them."""
        stmt = (
            select(AfterSaleItem)
            .where(AfterSaleItem.after_sale_id == after_sale_id)
            .order_by(AfterSaleItem.order_item_id.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def list_admin_claims(
        self,
        *,
        merchant_id: int | None = None,
        claim_status: str | None = None,
        order_no: str | None = None,
        after_sale_type: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AfterSale], int]:
        """One page of the console queue.

        ``merchant_id`` is mandatory in practice for a merchant-scoped principal and
        is applied in the query (INV-004): the filter is what keeps a DataScope of
        ``MERCHANT`` from silently meaning "all merchants".

        The type filter is named ``after_sale_type`` rather than ``type`` because
        shadowing the builtin inside a signature is the kind of thing that later
        costs somebody a confusing ``TypeError`` - ruff's A002 is right about it.
        """
        stmt = select(AfterSale)
        if merchant_id is not None:
            stmt = stmt.where(AfterSale.merchant_id == merchant_id)
        if claim_status is not None:
            stmt = stmt.where(AfterSale.claim_status == _as_value(claim_status))
        if order_no is not None:
            stmt = stmt.where(AfterSale.order_no == order_no)
        if after_sale_type is not None:
            stmt = stmt.where(AfterSale.type == _as_value(after_sale_type))
        return self._paginate(stmt, page=page, page_size=page_size)

    def list_customer_claims(
        self,
        *,
        user_id: int,
        claim_status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AfterSale], int]:
        """One page of a customer's own claims, newest first."""
        stmt = select(AfterSale).where(AfterSale.user_id == user_id)
        if claim_status is not None:
            stmt = stmt.where(AfterSale.claim_status == _as_value(claim_status))
        return self._paginate(stmt, page=page, page_size=page_size)

    def count_for_order(self, order_id: int) -> int:
        """Used by integration tests to prove no extra claim was created."""
        stmt = select(func.count()).select_from(AfterSale).where(AfterSale.order_id == order_id)
        return int(self._session.execute(stmt).scalar_one())

    # -- internals -------------------------------------------------------
    def _paginate(self, stmt: Select[object], *, page: int, page_size: int) -> tuple[list[AfterSale], int]:
        # Count from the filter chain, not from a fetched page: a page-size cap is not
        # a bound on how many claims exist.
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int(self._session.execute(count_stmt).scalar_one())

        offset = max(page - 1, 0) * page_size
        rows = (
            self._session.execute(
                stmt.order_by(AfterSale.created_at.desc(), AfterSale.id.desc())
                .limit(page_size)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return list(rows), total


class RefundRepository:
    """Access to ``refunds`` - the money-out record."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- writes ----------------------------------------------------------
    def insert_succeeded(
        self,
        *,
        after_sale_id: int,
        order_id: int,
        order_no: str,
        user_id: int,
        merchant_id: int | None,
        amount: int,
        operator_type: str,
        operator_id: int | None,
        idempotency_key: str,
        request_hash: str,
        payment_id: int | None = None,
        reason: str | None = None,
        completed_at: datetime | None = None,
        refund_no: str | None = None,
    ) -> Refund:
        """Write the refund row straight to ``SUCCEEDED`` and flush.

        V1 has no asynchronous refund, so the row is written in its final state in
        the same transaction that moves the money and updates every cap counter
        (design section 6.2 step 5). A ``PENDING`` row left for a worker would tell
        the customer money is coming when nothing will send it.

        ``refund_no`` may be supplied when the caller has already stamped it. When it is
        omitted, a 32-character ``uuid4`` hex is used as a temporary unique value and
        :meth:`stamp_refund_no` can replace it - the same placeholder trick
        ``CreateOrderWorkflow._insert_order`` uses for ``order_no``: the identifier embeds
        an auto-increment id, so the id has to exist first, and a *random* placeholder
        rather than a sequential one keeps two concurrent transactions from colliding on
        the placeholder itself.

        ## What the live refund path actually does (do not assume the placeholder)

        ``RefundWorkflow`` does **not** use either mechanism: it calls this method with no
        ``refund_no`` and then assigns the frozen ``NVR<YYYYMMDD><id:06d>`` directly at
        ``workflow.py:669`` via ``refund_no_for(refund_id=refund.id, created_at=now)``, so
        the id-embedding lives in exactly one place. Consequences worth knowing rather than
        discovering:

        * the placeholder is written and then overwritten within the same transaction, so
          nothing is ever selected by its temporary value - but it does mean this method
          cannot be called *outside* a caller that stamps afterwards, because the column
          would keep the random value;
        * ``stamp_refund_no`` currently has **no caller on the refund path**, so it is dead
          code today. It is kept because it is the documented counterpart to this
          parameter, and this docstring previously claimed the workflow used it - reported
          by after-sales-workflow, who checked rather than assumed.
        """
        from uuid import uuid4

        refund = Refund(
            refund_no=refund_no or uuid4().hex,
            after_sale_id=after_sale_id,
            order_id=order_id,
            order_no=order_no,
            payment_id=payment_id,
            user_id=user_id,
            merchant_id=merchant_id,
            amount=amount,
            status=RefundStatus.SUCCEEDED.value,
            reason=reason,
            operator_type=operator_type,
            operator_id=operator_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            completed_at=completed_at or utc_now(),
        )
        self._session.add(refund)
        self._session.flush()
        return refund

    def mark_failed(self, refund: Refund, *, reason: str | None = None, at: datetime | None = None) -> Refund:
        """Record a refusal on an already-written row.

        Provided because ``FAILED`` is a real outcome that must be recordable rather
        than being modelled as "the row was never written": a provider refusal the
        customer can see is different from a refund that silently did not happen.
        The workflow normally raises instead - a failed refund rolls back with its
        cap counters - so this exists for a caller that deliberately keeps the row
        visible.
        """
        refund.status = RefundStatus.FAILED.value
        refund.reason = None if reason is None else reason[:500]
        refund.completed_at = at or utc_now()
        self._session.flush()
        return refund

    def stamp_refund_no(self, refund: Refund, *, refund_no: str) -> Refund:
        """Set ``refund_no`` after the flush that produced the id it embeds."""
        refund.refund_no = refund_no
        self._session.flush()
        return refund

    # -- reads -----------------------------------------------------------
    def get(self, refund_id: int, *, for_update: bool = False) -> Refund | None:
        if not for_update:
            return self._session.get(Refund, refund_id)
        stmt = select(Refund).where(Refund.id == refund_id).with_for_update()
        return self._session.execute(stmt).scalars().first()

    def get_by_refund_no(
        self,
        refund_no: str,
        *,
        merchant_id: int | None = None,
        user_id: int | None = None,
        for_update: bool = False,
    ) -> Refund | None:
        """Resolve a refund by its public identifier, scoped in the query (IDOR)."""
        stmt = select(Refund).where(Refund.refund_no == refund_no)
        if merchant_id is not None:
            stmt = stmt.where(Refund.merchant_id == merchant_id)
        if user_id is not None:
            stmt = stmt.where(Refund.user_id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self._session.execute(stmt).scalars().first()

    def get_by_idempotency_key(
        self, *, merchant_id: int, idempotency_key: str, for_update: bool = False
    ) -> Refund | None:
        """The idempotency guard for ``refund:execute``, as a read.

        Read *after* attempting the claim through ``IdempotencyRepository`` - never
        as a substitute for it. The caller compares ``request_hash`` and raises
        ``REFUND_ALREADY_COMPLETED`` (80007) on a mismatch, so a reused key for a
        different amount cannot send money twice for the wrong figure.
        """
        stmt = select(Refund).where(
            Refund.merchant_id == merchant_id,
            Refund.idempotency_key == idempotency_key,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self._session.execute(stmt).scalars().first()

    def list_for_after_sale(self, after_sale_id: int) -> list[Refund]:
        """Every movement against one claim, oldest first.

        This is the list the FG-12 positive control reads: a legal partial refund
        followed by a legal second one must appear here as two rows whose amounts sum
        to ``paid_amount``, and a third attempt must be refused.
        """
        stmt = (
            select(Refund)
            .where(Refund.after_sale_id == after_sale_id)
            .order_by(Refund.created_at.asc(), Refund.id.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def list_for_order(self, order_id: int) -> list[Refund]:
        """Every movement against one order, oldest first."""
        stmt = (
            select(Refund)
            .where(Refund.order_id == order_id)
            .order_by(Refund.created_at.asc(), Refund.id.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def total_for_payment(self, payment_id: int) -> int:
        """The sum of every refund against one payment.

        FG-12 asserts ``total_for_payment(payment_id) == payments.refunded_amount``:
        the denormalised counter and the rows that explain it must agree, which is
        the money-out form of INV-007 ("a balance no ledger explains is worse than no
        ledger").
        """
        stmt = select(func.coalesce(func.sum(Refund.amount), 0)).where(Refund.payment_id == payment_id)
        return int(self._session.execute(stmt).scalar_one())

    def total_for_after_sale(self, after_sale_id: int) -> int:
        """The sum of every refund against one claim."""
        stmt = select(func.coalesce(func.sum(Refund.amount), 0)).where(Refund.after_sale_id == after_sale_id)
        return int(self._session.execute(stmt).scalar_one())

    def count_for_after_sale(self, after_sale_id: int) -> int:
        """Used by integration tests to prove no extra movement was written."""
        stmt = select(func.count()).select_from(Refund).where(Refund.after_sale_id == after_sale_id)
        return int(self._session.execute(stmt).scalar_one())

    def list_admin_refunds(
        self,
        *,
        merchant_id: int | None = None,
        status: str | None = None,
        order_no: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Refund], int]:
        """One page of the read-only console refund list (design section 7)."""
        stmt = select(Refund)
        if merchant_id is not None:
            stmt = stmt.where(Refund.merchant_id == merchant_id)
        if status is not None:
            stmt = stmt.where(Refund.status == _as_value(status))
        if order_no is not None:
            stmt = stmt.where(Refund.order_no == order_no)
        return self._paginate(stmt, page=page, page_size=page_size)

    def list_customer_refunds(
        self, *, user_id: int, page: int = 1, page_size: int = 20
    ) -> tuple[list[Refund], int]:
        """One page of a customer's own refunds, newest first."""
        stmt = select(Refund).where(Refund.user_id == user_id)
        return self._paginate(stmt, page=page, page_size=page_size)

    # -- internals -------------------------------------------------------
    def _paginate(self, stmt: Select[object], *, page: int, page_size: int) -> tuple[list[Refund], int]:
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int(self._session.execute(count_stmt).scalar_one())

        offset = max(page - 1, 0) * page_size
        rows = (
            self._session.execute(
                stmt.order_by(Refund.created_at.desc(), Refund.id.desc()).limit(page_size).offset(offset)
            )
            .scalars()
            .all()
        )
        return list(rows), total


def _as_value(status: str | object) -> str:
    """Normalise an enum member or a raw string to the stored ``VARCHAR`` value.

    Accepts both because the API layer filters with enums while a row read from the
    database yields a plain string, and a comparison that silently fails on one of
    them is a filter that quietly returns everything.
    """
    value = getattr(status, "value", None)
    return str(value) if value is not None else str(status)
