"""Payment use cases - create an attempt, read one, and drive the callback.

Spec references:
    design section 4.1  creating an attempt writes ``PAYING`` on the payment record
                        and moves the **order's** axis ``UNPAID -> PAYING``.
    design section 6.1  the callback is delegated to ``PaymentSuccessWorkflow``; this
                        module owns the transaction boundary, not the transaction body.
    REQ-PAY-001/005/006, INV-008.
    section 14.6/109    ownership is applied **in the query**, never post-load.
    section 48          ``Idempotency-Key`` plus the client-request id: two guards.

## Why the service owns the transaction and the workflow does not

``PaymentSuccessWorkflow.execute`` deliberately does not commit: it returns a
:class:`~app.modules.payment.workflow.CallbackExecution` that says whether an effect
was applied, and this module decides commit-versus-rollback. The split exists
because "the transaction started here" and "the business decision was made here" are
different facts, and only the caller knows which boundary it opened. An HTTP handler
and the FG-11 concurrency gate both drive the workflow, and they do not share a
session lifetime.

## The create path has two guards, exactly like order creation

1. ``Idempotency-Key`` (required header) -> ``payments.idempotency_key``, and
2. ``client_request_id`` (required body field) -> ``payments.client_request_id``.

Both are UNIQUE and both are set to **one** payment, so a client that loses its
header still cannot create a second attempt for the same intent. The insert is
*attempted* rather than pre-checked - a ``SELECT`` before the ``INSERT`` is the
check-then-act race ``HANDOFF.md`` section 6 records - and the duplicate-key error is
turned into the replay the caller asked for.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import (
    IdempotencyKeyRequiredError,
    IdempotencyPayloadMismatchError,
    OrderExpiredError,
    OrderNotFoundError,
    OrderStateInvalidError,
    PaymentChannelUnsupportedError,
    PaymentNotFoundError,
    PaymentStateInvalidError,
)
from app.core.logging import get_logger
from app.modules.identity.service import Principal
from app.modules.order.enums import OrderStatus, PaymentStatus
from app.modules.order.models import Order
from app.modules.order.repository import OrderRepository
from app.modules.payment.enums import PaymentRecordStatus
from app.modules.payment.models import Payment
from app.modules.payment.repository import PaymentCallbackRepository, PaymentRepository
from app.modules.payment.workflow import CallbackExecution, CallbackRequest, PaymentSuccessWorkflow
from app.shared.db.base import utc_now

logger = get_logger(__name__)

__all__ = [
    "PaymentCreateResult",
    "PaymentPage",
    "PaymentService",
    "canonical_payment_request_hash",
    "is_mock_allowed",
]


def canonical_payment_request_hash(
    *, user_id: int, order_no: str, channel: str
) -> str:
    """``sha256`` of the canonical business inputs of a create attempt.

    Canonical means: JSON with sorted keys, no insignificant whitespace, every value
    a string - so the same intent written two ways hashes identically.

    ## Why ``user_id`` is part of the hash

    ``payments.idempotency_key`` is unique on ``(merchant_id, idempotency_key)`` - **not
    per customer**. Without the buyer in the hash, two customers who happened to send
    the same key for the same order+channel would produce the same hash, and the second
    one would be handed the first one's payment. That is a cross-customer read produced
    by a "safe retry" path, which is the last place anyone looks for an authorisation
    bug. Including the buyer turns it into an
    ``IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD`` conflict instead.

    ``user_id`` is a business input here - it is *who is paying* - so this is
    canonicalisation, not a smuggled authorisation check.
    """
    payload = json.dumps(
        {"channel": str(channel), "order_no": str(order_no), "user_id": int(user_id)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_mock_allowed(settings: Settings) -> bool:
    """Whether the DEV/DEMO mock surface may run at all (REQ-PAY-005, INV-008).

    The endpoint-level half of the guard. ``Settings`` carries a validator that
    refuses ``PAYMENT_MOCK_ENABLED`` in staging/prod, but a validator someone
    bypasses with ``model_construct()`` would otherwise be the *only* guard - so the
    edge asks the same question again. Two independent checks, because the thing
    being protected is a route that marks an order paid without money moving.

    Delegates to ``Settings.mock_payment_allowed`` rather than re-deriving the
    ``APP_ENV`` set here: a second spelling of ``{"dev", "test", "demo"}`` is a
    second place for the membership to be wrong, and getting it wrong in the
    permissive direction is a production settlement with no payment.
    """
    return settings.mock_payment_allowed


@dataclass(slots=True)
class PaymentCreateResult:
    """One create request's outcome.

    ``replayed`` is the whole point: a client that cannot tell "we just created this"
    from "your retry created this" has to guess whether its retry was safe
    (INV-015), and guessing is how duplicate attempts appear.
    """

    payment: Payment
    replayed: bool
    order: Order


@dataclass(slots=True)
class PaymentPage:
    """A page of payments plus the total behind the filter (section 3's envelope)."""

    rows: list[Payment]
    total: int


class PaymentService:
    """Create payment attempts, answer the reads, and settle through the workflow."""

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._payments = PaymentRepository(session)
        self._orders = OrderRepository(session)

    # -- create ----------------------------------------------------------
    def create(
        self,
        *,
        principal: Principal,
        order_no: str,
        channel: str,
        client_request_id: str,
        idempotency_key: str,
    ) -> PaymentCreateResult:
        """Create one payment attempt for one order, or replay the attempt a key made.

        The order is locked ``FOR UPDATE`` before anything is decided, because the
        decision "this order is still payable, and it is not already PAID" is what
        two concurrent creates would otherwise both make correctly and then both act
        on.
        """
        if not idempotency_key:
            # Defence in depth: the HTTP layer already refuses a missing header with
            # 10010, but the service is reachable from tests and (later) the tool
            # gateway, and a create with no key is not idempotent by construction.
            raise IdempotencyKeyRequiredError(
                "creating a payment attempt is idempotent by contract; "
                "supply an Idempotency-Key header"
            )

        self._assert_channel_supported(channel)
        order = self._orders.get_by_order_no_for_update(
            order_no, user_id=principal.user_id
        )
        if order is None:
            # A foreign order is ORDER_NOT_FOUND (50003), never 403 - distinguishing
            # them would make this an existence oracle (section 109).
            raise OrderNotFoundError(
                "no such order for this account", context={"order_no": order_no}
            )

        request_hash = canonical_payment_request_hash(
            user_id=principal.user_id, order_no=order.order_no, channel=channel
        )

        payment, replayed = self._insert_or_replay(
            principal=principal,
            order=order,
            channel=channel,
            client_request_id=client_request_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        self._session.commit()
        logger.info(
            "payment attempt served",
            payment_no=payment.payment_no,
            order_no=order.order_no,
            channel=channel,
            amount=payment.amount,
            replayed=replayed,
        )
        return PaymentCreateResult(payment=payment, replayed=replayed, order=order)

    # -- reads -----------------------------------------------------------
    def get_customer_payment(self, *, principal: Principal, payment_id: int) -> Payment:
        """One of the caller's own payments, by internal id.

        ``user_id`` is in the query, not checked afterwards. A post-load check is one
        early ``return`` away from leaking the existence of somebody else's payment.
        """
        payment = self._payments.get(payment_id)
        if payment is None or payment.user_id != principal.user_id:
            raise PaymentNotFoundError(
                "no such payment for this account", context={"payment_id": payment_id}
            )
        return payment

    def get_payment_for_order(self, *, principal: Principal, order_no: str) -> Payment:
        """The order's most recent attempt (section 15.2: one attempt per order in V1).

        Resolves through the **order**, so ownership is established by the query that
        finds the order rather than by comparing ids afterwards: a caller probing
        somebody else's ``order_no`` gets ``ORDER_NOT_FOUND``, which is the same
        answer a nonexistent one gets.
        """
        order = self._orders.get_by_order_no(order_no, user_id=principal.user_id)
        if order is None:
            raise OrderNotFoundError(
                "no such order for this account", context={"order_no": order_no}
            )
        payment = self._payments.get_latest_for_order(order.id)
        if payment is None:
            raise PaymentNotFoundError(
                "this order has no payment attempt yet", context={"order_no": order_no}
            )
        return payment

    def list_admin_payments(
        self,
        *,
        merchant_id: int | None,
        status: str | None = None,
        order_no: str | None = None,
        channel: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PaymentPage:
        """The console list (section 15.2), paged, newest first."""
        rows, total = self._payments.list_admin_payments(
            merchant_id=merchant_id,
            status=status,
            order_no=order_no,
            channel=channel,
            page=page,
            page_size=page_size,
        )
        return PaymentPage(rows=rows, total=total)

    # -- callbacks -------------------------------------------------------
    def process_callback(self, request: CallbackRequest) -> CallbackExecution:
        """Drive ``PaymentSuccessWorkflow`` and own its transaction boundary."""
        return PaymentSuccessWorkflow(self._session, self._settings).execute(request)

    def record_refused_signature(
        self, request: CallbackRequest, *, reason: str
    ) -> None:
        """Record a delivery refused *before* the workflow (the edge's fast path).

        The endpoint verifies the signature itself so that a bad signature is
        answered 401 without opening a business transaction. This method writes the
        audit row for that refusal - with ``signature_valid = False`` and
        ``process_status = FAILED`` - because "was this rejected for the right
        reason?" has to be answerable from the table, and an invalid signature must
        never produce a row that claims success (design section 7).
        """
        callbacks = PaymentCallbackRepository(self._session)
        inserted = callbacks.insert_received(
            provider=request.provider,
            provider_event_id=request.event_id,
            event_type=request.event_type,
            received_at=utc_now(),
            signature_valid=False,
        )
        if inserted.inserted:
            callbacks.mark_failed(inserted.callback, error=f"invalid signature: {reason}")
        self._session.commit()

    # -- pieces ----------------------------------------------------------
    def _assert_channel_supported(self, channel: str) -> None:
        """Refuse a channel this deployment does not accept.

        Two different refusals, deliberately distinct:

        * a channel outside the frozen vocabulary cannot even reach here - the request
          model's enum rejects it as a 422;
        * a *member* the deployment has switched off in
          ``PAYMENT_ENABLED_CHANNELS`` is ``PAYMENT_CHANNEL_UNSUPPORTED`` (60005),
          because the request is well-formed and the answer is a policy decision the
          client can act on (choose another channel).

        Storing an attempt for a channel nothing can settle would be a payment that
        can never succeed - worse than a refusal, because the customer waits.
        """
        if channel not in set(self._settings.PAYMENT_ENABLED_CHANNELS):
            raise PaymentChannelUnsupportedError(
                "this payment channel is not enabled on this deployment",
                context={
                    "channel": channel,
                    "enabled": sorted(self._settings.PAYMENT_ENABLED_CHANNELS),
                },
            )

    def _insert_or_replay(
        self,
        *,
        principal: Principal,
        order: Order,
        channel: str,
        client_request_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[Payment, bool]:
        """Attempt the insert; on a duplicate key, resolve which guard fired.

        The insert is wrapped in a SAVEPOINT so the duplicate-key failure is confined
        and this session stays usable: MySQL does not abort the transaction on a failed
        statement, but SQLAlchemy marks the session as needing a rollback, and catching
        the error without ``begin_nested`` would raise ``PendingRollbackError`` on the
        next read.
        """
        if order.order_status != OrderStatus.PENDING_PAYMENT.value:
            raise OrderStateInvalidError(
                "this order is not awaiting payment",
                context={"order_no": order.order_no, "order_status": order.order_status},
            )
        if order.payment_status in {
            PaymentStatus.PAID.value,
            PaymentStatus.PARTIAL_REFUNDED.value,
            PaymentStatus.REFUNDED.value,
        }:
            # Already settled (or settled and refunded): a new attempt would be a
            # second charge for money already taken. Refused rather than replayed,
            # because there is no attempt to replay - the client is asking for
            # something it cannot have.
            raise PaymentStateInvalidError(
                "this order is already paid and cannot be paid again",
                context={"order_no": order.order_no, "payment_status": order.payment_status},
            )

        now = utc_now()
        payment = Payment(
            # Placeholder until the id exists: `payment_no` is NOT NULL and NOT NULL
            # means "a value is required", not "the final value is required". A uuid4
            # keeps two concurrent transactions from colliding on the placeholder, and
            # it is replaced before the commit, so no client observes it. Same device
            # as `CreateOrderWorkflow._insert_order`.
            payment_no=uuid4().hex,
            order_id=order.id,
            order_no=order.order_no,
            merchant_id=order.merchant_id,
            user_id=principal.user_id,
            channel=channel,
            # Read from the order, never from the request: a client that could name
            # its own amount could settle a large order by paying one unit, and the
            # callback's amount guard compares against *this* figure.
            amount=order.payable_amount,
            status=PaymentRecordStatus.PAYING.value,
            external_transaction_no=None,
            idempotency_key=idempotency_key,
            client_request_id=client_request_id,
            request_hash=request_hash,
            paid_amount=0,
            refunded_amount=0,
            expires_at=order.expires_at,
            created_at=now,
            updated_at=now,
        )
        try:
            with self._session.begin_nested():
                self._session.add(payment)
                self._session.flush()
                if order.expires_at is not None and order.expires_at <= now:
                    raise OrderExpiredError(
                        "this order's payment window has expired",
                        context={"order_no": order.order_no},
                    )
        except IntegrityError:
            return self._resolve_conflict(
                principal=principal,
                client_request_id=client_request_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            ), True

        payment.payment_no = f"{self._settings.PAYMENT_NO_PREFIX}{now:%Y%m%d}{payment.id:06d}"
        self._session.flush()

        self._mark_order_paying(order, now=now)
        logger.info(
            "payment attempt created",
            payment_no=payment.payment_no,
            order_no=order.order_no,
            channel=channel,
            amount=payment.amount,
        )
        return payment, False

    def _resolve_conflict(
        self,
        *,
        principal: Principal,
        client_request_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> Payment:
        """Turn a duplicate-key failure into the replay the client asked for.

        Two constraints can fire and they mean different things, so both are asked
        rather than parsed out of the driver's error string (which is
        version-specific and would quietly stop matching):

        * ``uq_payments_user_client_request`` - the client lost its
          ``Idempotency-Key`` header and retried. Its own attempt already exists.
        * ``uq_payments_merchant_idempotency_key`` - the retry carries the header. The
          attempt may belong to a *different* client that reused the same key, which
          is why the request hash is compared before replaying: same key + different
          body is ``IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD``, not a replay.
        """
        existing = self._payments.get_by_client_request_id(
            user_id=principal.user_id, client_request_id=client_request_id
        )
        if existing is None and principal.merchant_id is not None:
            existing = self._payments.get_by_idempotency_key(
                merchant_id=principal.merchant_id, idempotency_key=idempotency_key
            )
        if existing is None:
            # The duplicate key is not explained by either guard, so it is not the
            # duplicate this method is allowed to absorb. Re-raising is the honest
            # answer: reporting a replay that does not exist would hide a real defect.
            raise

        if existing.request_hash != request_hash:
            raise IdempotencyPayloadMismatchError(
                "this Idempotency-Key was already used for a different payment request",
                context={
                    "payment_no": existing.payment_no,
                    "order_no": existing.order_no,
                    "channel": existing.channel,
                },
            )
        logger.info(
            "payment attempt replayed",
            payment_no=existing.payment_no,
            order_no=existing.order_no,
            user_id=principal.user_id,
        )
        return existing

    def _mark_order_paying(self, order: Order, *, now) -> None:
        """Move the order's axis ``UNPAID -> PAYING`` (design section 4.1).

        Only from ``UNPAID``: an order already ``PAYING`` has another attempt in
        flight, and re-writing the same value would bump ``version`` and make the
        optimistic-locking counter report a change that did not happen.

        No ``order_status_logs`` row is written here, and that is deliberate rather
        than an omission. ``order_status`` does not move - the order is still
        ``PENDING_PAYMENT`` - and the log table records **status transitions**, not
        column edits. The settlement path appends the one ``PENDING_PAYMENT ->
        PROCESSING`` row; writing a second row for "payment_status changed" would put
        two entries in the audit trail for one event, which is precisely the
        ambiguity the FG-11 assertion ("exactly one log") exists to rule out.
        """
        if order.payment_status != PaymentStatus.UNPAID.value:
            return
        order.payment_status = PaymentStatus.PAYING.value
        order.updated_at = now
        self._orders.touch_version(order)
