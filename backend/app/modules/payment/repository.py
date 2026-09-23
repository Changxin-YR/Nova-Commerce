"""Payment data access (spec section 18: a repository does data access, nothing else).

    PaymentRepository, PaymentCallbackRepository

No amount comparison, no state decision, no signature verification lives here.
Whether a callback may settle a payment is decided by
``PaymentSuccessWorkflow`` **while the row is locked**; whether a channel is
enabled is decided by the service against settings; whether a signature is valid
is decided by ``providers``. This module answers "what is in the database" and
"write this row".

## Two methods are not ordinary queries

:meth:`PaymentRepository.get_by_payment_no_for_update` is the pessimistic lock the
callback path needs. Following the rule generalised in ``HANDOFF.md`` section 7 -
**decide inside the lock, not before it** - the "is this payment already SUCCESS?"
decision and the SUCCESS write must happen under one lock, or two concurrent
deliveries of two *different* event ids can both read ``PAYING``, both decide
"settle it", and both deduct stock. The unique index does not save that case: the
two events are distinct rows, so both inserts succeed.

:meth:`PaymentCallbackRepository.insert_received` is the serialisation point for
concurrent deliveries of the *same* event, and the only method here that catches a
database exception. It is the pattern ``IdempotencyRepository.insert_in_progress``
already established, for the same reason and with the same SAVEPOINT.

## Read-back helpers

``PaymentCallbackRepository.mark_*`` methods exist so the workflow does not build
its own UPDATE statements; ``PaymentRepository.record_success`` likewise. They
deliberately do **not** commit - the workflow owns exactly one commit
(PHASE5_DESIGN section 6.1 step 11).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.modules.payment.enums import CallbackProcessStatus, PaymentRecordStatus
from app.modules.payment.models import Payment, PaymentCallback
from app.shared.db.base import utc_now

__all__ = ["InsertedCallback", "PaymentCallbackRepository", "PaymentRepository"]

#: The record statuses from which a verified callback may still settle the
#: payment. A payment that is already ``SUCCESS`` is handled *before* this check as
#: a replay; ``FAILED``/``CLOSED`` mean this attempt is over and a new one is the
#: correct recovery, and ``REFUNDED``/``PARTIAL_REFUNDED`` mean money has already
#: moved out - settling again would be the double-settlement the guard exists for.
SETTLEABLE_STATUSES: tuple[str, ...] = (
    PaymentRecordStatus.INITIATED.value,
    PaymentRecordStatus.PAYING.value,
)


class PaymentRepository:
    """Queries over ``payments`` plus the two write paths Phase 5 needs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- writes ----------------------------------------------------------
    def add(self, payment: Payment) -> Payment:
        """Persist a new payment attempt and flush so its id is available.

        The flush is load-bearing, not cosmetic: ``payment_no`` is stamped from
        the auto-increment id afterwards, so the service needs the id before it can
        build the identifier. It happens inside the caller's transaction, which is
        also why ``uq_payments_merchant_payment_no`` can be relied on - nothing is
        committed until the whole create succeeds.
        """
        self._session.add(payment)
        self._session.flush()
        return payment

    def record_success(
        self,
        payment: Payment,
        *,
        paid_amount: int,
        external_transaction_no: str | None,
        paid_at: datetime | None = None,
    ) -> Payment:
        """Write the settlement onto an already-locked row.

        Takes the row rather than an id, because the caller must be holding its
        ``FOR UPDATE`` lock (``get_by_payment_no_for_update``). ``paid_amount`` is
        set from the **verified callback's** amount, never from ``payment.amount``
        - the amount guard has already established that they are equal, and copying
        the request rather than the settlement is how a "settled" row ends up
        recording money that never arrived.

        Status is moved to ``SUCCESS`` here; ``refunded_amount`` is deliberately
        untouched (it starts at 0 and only ``RefundWorkflow`` ever adds to it).
        """
        payment.status = PaymentRecordStatus.SUCCESS.value
        payment.paid_amount = paid_amount
        payment.external_transaction_no = external_transaction_no
        payment.paid_at = paid_at or utc_now()
        self._session.flush()
        return payment

    def apply_refund(self, payment: Payment, *, amount: int) -> Payment:
        """Add ``amount`` to the refunded total and roll the record's status up.

        Called by ``RefundWorkflow`` **while holding the row lock**. The status is
        derived from the two amounts here rather than passed in, because
        ``REFUNDED`` iff ``refunded_amount == paid_amount`` is the frozen rule
        (design section 6.2 step 5) and a caller-supplied status is a second place
        for it to be wrong.

        The ``refund_cap`` CHECK on ``payments`` is the authority on the cap - this
        method does not re-check it, so a bug in the workflow's validation surfaces
        as a database error (errno 3819) rather than as a silent over-refund.
        """
        payment.refunded_amount += amount
        if payment.refunded_amount == payment.paid_amount:
            payment.status = PaymentRecordStatus.REFUNDED.value
        elif payment.refunded_amount > 0:
            payment.status = PaymentRecordStatus.PARTIAL_REFUNDED.value
        self._session.flush()
        return payment

    # -- reads -----------------------------------------------------------
    def get(self, payment_id: int, *, for_update: bool = False) -> Payment | None:
        """Load one payment by primary key.

        ``for_update`` is a keyword rather than a second method so a caller cannot
        accidentally use the unlocked variant on a write path without the parameter
        being visible in the diff.
        """
        if not for_update:
            return self._session.get(Payment, payment_id)
        stmt = select(Payment).where(Payment.id == payment_id).with_for_update()
        return self._session.execute(stmt).scalars().first()

    def get_by_payment_no(
        self,
        payment_no: str,
        *,
        merchant_id: int | None = None,
        user_id: int | None = None,
        for_update: bool = False,
    ) -> Payment | None:
        """Resolve a payment by its public identifier.

        ``user_id``/``merchant_id`` are applied **in the query**, not checked after
        loading. That ordering is the IDOR defence (spec section 14.6): a consumer
        asking for somebody else's payment must get ``PAYMENT_NOT_FOUND`` (60000),
        and the only way to be certain that happens is for the foreign row never to
        be fetched. A post-load ownership check is one early ``return`` away from
        leaking existence.
        """
        stmt = select(Payment).where(Payment.payment_no == payment_no)
        if merchant_id is not None:
            stmt = stmt.where(Payment.merchant_id == merchant_id)
        if user_id is not None:
            stmt = stmt.where(Payment.user_id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self._session.execute(stmt).scalars().first()

    def get_by_payment_no_for_update(self, payment_no: str) -> Payment | None:
        """Lock a payment by its public identifier (section 27 rule).

        Named as its own method, rather than left to the ``for_update`` flag, so a
        reviewer can see at the call site that the callback's write path takes a
        lock. This is the lock that makes step 3/4/6 of
        ``PaymentSuccessWorkflow`` atomic with respect to every other delivery.
        """
        return self.get_by_payment_no(payment_no, for_update=True)

    def get_latest_for_order(self, order_id: int) -> Payment | None:
        """The most recent attempt for an order.

        "Most recent" is ``created_at DESC, id DESC`` - id as the tiebreaker
        because two attempts created in the same millisecond share ``created_at``
        at ``DATETIME(3)`` precision, and a list that reorders itself is a support
        ticket waiting to happen.
        """
        stmt = (
            select(Payment)
            .where(Payment.order_id == order_id)
            .order_by(Payment.created_at.desc(), Payment.id.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalars().first()

    def get_by_client_request_id(
        self, *, user_id: int, client_request_id: str, for_update: bool = False
    ) -> Payment | None:
        """The second idempotency guard: find the attempt a retry already created.

        Returns the row regardless of ``request_hash``; the caller compares the
        hash and raises ``IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD`` when it
        differs. Keeping the comparison out of the query is deliberate - a hash
        mismatch and a missing row are different failures with different business
        codes, and collapsing them into "no row" turns a client bug into a
        confusing second attempt for the wrong money.
        """
        stmt = select(Payment).where(
            Payment.user_id == user_id,
            Payment.client_request_id == client_request_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self._session.execute(stmt).scalars().first()

    def get_by_idempotency_key(
        self, *, merchant_id: int, idempotency_key: str, for_update: bool = False
    ) -> Payment | None:
        """The first idempotency guard, as a read.

        Used for the replay path *after* the service has attempted the insert - not
        instead of it. A ``SELECT`` before the ``INSERT`` is the check-then-act race
        the design forbids (``HANDOFF.md`` section 6).
        """
        stmt = select(Payment).where(
            Payment.merchant_id == merchant_id,
            Payment.idempotency_key == idempotency_key,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self._session.execute(stmt).scalars().first()

    def list_admin_payments(
        self,
        *,
        merchant_id: int | None = None,
        status: str | None = None,
        order_no: str | None = None,
        channel: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Payment], int]:
        """One page of the console list.

        ``merchant_id`` is mandatory in practice for a merchant-scoped principal and
        is applied in the query (INV-004): the filter is what keeps a DataScope of
        ``MERCHANT`` from silently meaning "all merchants". It is optional only so a
        platform-scoped administrator can pass ``None`` explicitly.
        """
        stmt = select(Payment)
        if merchant_id is not None:
            stmt = stmt.where(Payment.merchant_id == merchant_id)
        if status is not None:
            stmt = stmt.where(Payment.status == _as_value(status))
        if order_no is not None:
            stmt = stmt.where(Payment.order_no == order_no)
        if channel is not None:
            stmt = stmt.where(Payment.channel == _as_value(channel))
        return self._paginate(stmt, page=page, page_size=page_size)

    def count_for_order(self, order_id: int) -> int:
        """Used by integration tests to prove no extra attempt was created."""
        stmt = select(func.count()).select_from(Payment).where(Payment.order_id == order_id)
        return int(self._session.execute(stmt).scalar_one())

    def count_for_user(self, user_id: int) -> int:
        stmt = select(func.count()).select_from(Payment).where(Payment.user_id == user_id)
        return int(self._session.execute(stmt).scalar_one())

    # -- internals -------------------------------------------------------
    def _paginate(self, stmt: Select[Any], *, page: int, page_size: int) -> tuple[list[Payment], int]:
        # Count from the filter chain, not from a fetched page: a page-size cap is
        # not a bound on how many payments exist.
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int(self._session.execute(count_stmt).scalar_one())

        offset = max(page - 1, 0) * page_size
        rows = (
            self._session.execute(
                stmt.order_by(Payment.created_at.desc(), Payment.id.desc()).limit(page_size).offset(offset)
            )
            .scalars()
            .all()
        )
        return list(rows), total


class InsertedCallback:
    """Result of :meth:`PaymentCallbackRepository.insert_received`.

    A tiny value object rather than a tuple, because the two outcomes mean
    genuinely different things to the caller - ``inserted=True`` means "this
    delivery owns the business effect", ``inserted=False`` means "somebody else
    already does, replay your answer" - and a positional tuple is read wrong at
    exactly the moment it matters.
    """

    __slots__ = ("callback", "inserted")

    def __init__(self, callback: PaymentCallback, *, inserted: bool) -> None:
        self.callback = callback
        self.inserted = inserted

    @property
    def replayed(self) -> bool:
        """The opposite of ``inserted``, spelled out because call sites read better."""
        return not self.inserted

    def __repr__(self) -> str:
        return f"<InsertedCallback inserted={self.inserted} {self.callback!r}>"


class PaymentCallbackRepository:
    """Access to ``payment_callbacks`` - the idempotency anchor."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- the serialisation point -----------------------------------------
    def insert_received(
        self,
        *,
        provider: str,
        provider_event_id: str,
        event_type: str,
        received_at: datetime,
        signature_valid: bool,
        payload_snapshot: dict | None = None,
        payment_no: str | None = None,
        order_no: str | None = None,
        merchant_id: int | None = None,
        attempt_count: int = 1,
    ) -> InsertedCallback:
        """Claim ``(provider, provider_event_id)``; return the winner either way.

        ## Why this catches an exception instead of checking first

        The obvious implementation is ``get(event_id) or insert()``, and it is the
        defect this table exists to prevent. Two concurrent deliveries of one event
        can both find nothing, both proceed to insert, and only *one* needs to be
        correct for a test to pass while production settles the same payment twice.
        The database is the only place the decision can be made atomically, so this
        method **attempts the insert** and treats the duplicate-key error as the
        answer "you lost the race".

        ## Why a SAVEPOINT

        On MySQL a failed statement does not abort the transaction, but SQLAlchemy
        does not know that and marks the session as needing a rollback - so
        catching the ``IntegrityError`` and carrying on with the same session would
        raise ``PendingRollbackError`` on the next statement. ``begin_nested()``
        confines the failure to a ``SAVEPOINT`` that can be rolled back on its own:
        the winner's transaction stays healthy and this caller can immediately read
        the winning row.

        ## Why the row is not committed here

        The claim commits with the business effect (``PaymentSuccessWorkflow``'s
        single commit). A rollback therefore leaves no callback row behind, and a
        genuine provider retry after a real failure is allowed - which is correct,
        because the alternative (committing the claim separately) would make a
        crashed workflow permanently unretryable.

        ``signature_valid`` is passed in rather than computed: an invalid signature
        is recorded (so the rejection is auditable) but never reaches the workflow.
        """
        callback = PaymentCallback(
            provider=provider,
            provider_event_id=provider_event_id,
            event_type=event_type,
            received_at=received_at,
            signature_valid=signature_valid,
            payload_snapshot=payload_snapshot,
            payment_no=payment_no,
            order_no=order_no,
            merchant_id=merchant_id,
            process_status=CallbackProcessStatus.RECEIVED.value,
            attempt_count=attempt_count,
        )
        try:
            with self._session.begin_nested():
                self._session.add(callback)
                self._session.flush()
        except IntegrityError:
            # The unique index answered for us: this event was already delivered.
            existing = self.get_by_event(provider=provider, provider_event_id=provider_event_id)
            if existing is None:
                # The duplicate error cannot be explained by this table, so it is
                # not the duplicate this method is allowed to absorb - re-raise
                # rather than silently reporting a replay that does not exist.
                raise
            return InsertedCallback(existing, inserted=False)
        return InsertedCallback(callback, inserted=True)

    # -- writes ----------------------------------------------------------
    def mark_processed(self, callback: PaymentCallback, *, at: datetime | None = None) -> PaymentCallback:
        """Finalise a callback whose business effect committed (step 11).

        ``processed_at`` is stamped here rather than by the caller so the status
        and the timestamp cannot disagree - a ``PROCESSED`` row without a
        ``processed_at`` is unreadable in triage.
        """
        callback.process_status = CallbackProcessStatus.PROCESSED.value
        callback.processed_at = at or utc_now()
        callback.process_error = None
        self._session.flush()
        return callback

    def mark_failed(
        self,
        callback: PaymentCallback,
        *,
        error: str | None = None,
        at: datetime | None = None,
    ) -> PaymentCallback:
        """Record a refusal (bad signature, unknown payment, business rule)."""
        callback.process_status = CallbackProcessStatus.FAILED.value
        callback.process_error = None if error is None else error[:500]
        callback.processed_at = at or utc_now()
        self._session.flush()
        return callback

    def mark_ignored(
        self,
        callback: PaymentCallback,
        *,
        error: str | None = None,
        at: datetime | None = None,
    ) -> PaymentCallback:
        """Record a delivery with deliberately no business effect.

        A duplicate delivery and an unknown ``payment_no`` both land here. This is
        a **success** outcome, not an error: the provider is answered HTTP 200
        (``PAYMENT_CALLBACK_DUPLICATE``, 60004) because telling it "error" would
        make it retry an event that is already settled.
        """
        callback.process_status = CallbackProcessStatus.IGNORED.value
        callback.process_error = None if error is None else error[:500]
        callback.processed_at = at or utc_now()
        self._session.flush()
        return callback

    # -- reads -----------------------------------------------------------
    def get_by_event(self, *, provider: str, provider_event_id: str) -> PaymentCallback | None:
        """The winning row for one provider event.

        This is what makes the replay path possible: the loser of the insert race
        reads the winner's row and answers from it, so both deliveries see one
        consistent outcome.
        """
        stmt = select(PaymentCallback).where(
            PaymentCallback.provider == provider,
            PaymentCallback.provider_event_id == provider_event_id,
        )
        return self._session.execute(stmt).scalars().first()

    def list_for_payment_no(self, payment_no: str) -> list[PaymentCallback]:
        """Every delivery recorded for one payment, oldest first - the order a
        support agent reads a payment's history in."""
        stmt = (
            select(PaymentCallback)
            .where(PaymentCallback.payment_no == payment_no)
            .order_by(PaymentCallback.received_at.asc(), PaymentCallback.id.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def list_for_order_no(self, order_no: str) -> list[PaymentCallback]:
        """Every delivery recorded for one order (including unresolved ones whose
        ``order_no`` was readable but whose ``payment_no`` was not)."""
        stmt = (
            select(PaymentCallback)
            .where(PaymentCallback.order_no == order_no)
            .order_by(PaymentCallback.received_at.asc(), PaymentCallback.id.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def count_processed_for_payment_no(self, payment_no: str) -> int:
        """FG-11's "exactly one callback row in PROCESSED" assertion, as a query."""
        stmt = (
            select(func.count())
            .select_from(PaymentCallback)
            .where(
                PaymentCallback.payment_no == payment_no,
                PaymentCallback.process_status == CallbackProcessStatus.PROCESSED.value,
            )
        )
        return int(self._session.execute(stmt).scalar_one())


def _as_value(status: str | object) -> str:
    """Normalise an enum member or a raw string to the stored ``VARCHAR`` value.

    Accepts both because the API layer filters with enums while a row read from the
    database yields a plain string, and a comparison that silently fails on one of
    them is a filter that quietly returns everything.
    """
    value = getattr(status, "value", None)
    return str(value) if value is not None else str(status)
