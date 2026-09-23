"""``PaymentSuccessWorkflow`` - the transaction body that turns a verified provider
callback into a settlement.

Spec references:
    design section 2      a payment can only become PAID from a verified provider
                          callback, and that callback is idempotent **at the
                          database**.
    design section 6.1    the step order, which is load-bearing.
    REQ-PAY-002/003/004/005/006, INV-008.
    section 27            ``decide inside the lock, not before it``.
    section 49            business rows and the (future) outbox row commit together.

## The ordering rule, and why it is the whole design

Design 6.1 states the steps in order because several of them are only correct in
that order:

1. **The callback insert is attempted first, never pre-checked.** The obvious
   implementation is ``get(event_id) or insert()``, and it is the defect this
   workflow exists to prevent: two concurrent deliveries both find nothing, both
   proceed, and only *one* needs to be wrong for the same money to be settled twice.
   ``UNIQUE (provider, provider_event_id)`` is the serialisation point, so
   ``insert_received`` attempts the insert and treats the duplicate-key error as the
   answer "you lost the race" (the same rule ``CreateOrderWorkflow`` applies to
   ``idempotency_records`` - see ``HANDOFF.md`` section 6).
2. **The claim happens before any business read.** A duplicate delivery therefore
   cannot reach the amount guard, the state guard or the deduction.
3. **The payment row is locked ``FOR UPDATE``, then the order row.** Always in that
   order, always both, so two settlements for one order cannot deadlock or
   interleave. The lock is what makes step 4's decision and step 6's write atomic
   with respect to every other delivery.
4. **The amount guard compares the provider's figure against the *payment row*.**
   Not against the order, and never against the client.
5. **Exactly one ``order_status_logs`` row per settlement.** A log written on the
   replay path would tell an operator the order moved twice, which is the single
   most misleading thing the audit trail can say during a double-settlement
   investigation.
6. **The deduction key is ``order-deduct:{order_no}:{order_item_id}``.** It is what
   makes a *retried* workflow - after a crash between the deduction and the commit -
   unable to deduct twice, because ``inventory_movements.idempotency_key`` is UNIQUE
   and ``InventoryService.deduct`` treats a repeat as a replay.
7. **The fulfillment shell is created and the order's ``fulfillment_status`` is left
   alone.** Creating a package is not shipping it (design 6.1 step 9); the ship
   endpoint moves that axis.

## One transaction, one commit

The callback row, the payment, the order, the status log, the inventory deductions
and the fulfillment shell become visible together or not at all. That is not
tidiness: a callback row committed without its business effect is a settlement the
database believes happened and no customer received - and, because the event id is
now claimed, a provider retry would be answered "duplicate" and never fix it.

To make that true the workflow commits **only on the path that applied an effect**.
A refusal (unknown payment, amount mismatch, illegal state) is recorded and then
rolled back, so the event id stays free for a corrected redelivery. Nothing
information-bearing is lost: the refusal is logged with the reason, and the provider
is told what was wrong through the HTTP status the service maps it to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.modules.inventory.enums import OperatorType as MovementOperatorType, ReferenceType
from app.modules.inventory.service import InventoryService
from app.modules.marketing.coupon_service import CouponService
from app.modules.order.enums import OperatorType as LogOperatorType, OrderStatus, PaymentStatus
from app.modules.order.models import Order, OrderItem
from app.modules.order.repository import OrderRepository, OrderStatusLogRepository
from app.modules.payment.enums import (
    CallbackProcessStatus,
    PaymentRecordStatus,
)
from app.modules.payment.models import Payment, PaymentCallback
from app.modules.payment.providers import (
    CallbackHeaders,
    filter_callback_payload,
    parse_amount_minor,
    verify_signature,
)
from app.modules.payment.repository import (
    SETTLEABLE_STATUSES,
    PaymentCallbackRepository,
    PaymentRepository,
)
from app.shared.db.base import utc_now
from app.shared.outbox import (
    OutboxAggregateType,
    OutboxEventType,
    OutboxWriter,
    payment_settled_payload,
)

logger = get_logger(__name__)

__all__ = [
    "ORDER_PROCESSING_REASON",
    "SETTLEABLE_STATUSES",
    "CallbackExecution",
    "CallbackRequest",
    "PaymentSuccessWorkflow",
    "order_deduct_key",
]


#: Human-readable reason stored on the one settlement status log (section 36). The
#: operator-facing audit trail says *why* the order moved, not merely that it did.
ORDER_PROCESSING_REASON = "payment succeeded"


def order_deduct_key(*, order_no: str, order_item_id: int) -> str:
    """The deterministic ``ORDER_DEDUCT`` idempotency key (design 6.1 step 7).

    ``order-deduct:{order_no}:{order_item_id}`` - frozen. Deterministic rather than
    random because ``inventory_movements.idempotency_key`` is UNIQUE and is the
    ledger's own duplicate guard (INV-003): the same order line can never be
    deducted twice, **even if the workflow is re-entered after a crash between the
    deduction and the commit**. A random key would throw that protection away, and
    the failure it prevents - stock deducted twice for one paid line - is invisible
    until a stock count disagrees with the ledger.

    Keyed by ``order_item_id`` rather than by ``sku_id`` because one order may hold
    two lines of the same SKU in different warehouses; a SKU-keyed movement would
    make the second line's deduction look like a replay of the first.
    """
    return f"order-deduct:{order_no}:{order_item_id}"


@dataclass(frozen=True, slots=True)
class CallbackRequest:
    """One provider delivery, exactly as it arrived.

    ``raw_body`` is the **bytes**, not a parsed dict, and that is load-bearing: the
    signature is computed over ``f"{timestamp}.{raw_body}"``, so a verifier that
    re-serialised a parsed payload would refuse every honest provider whose key
    order or spacing differs from Python's - and would accept a reordered body whose
    round-trip happened to match. The parsed ``payload`` is carried alongside purely
    for the business facts (``payment_no``, ``amount``), and is always derived from
    these same bytes by the caller.
    """

    provider: str
    event_id: str
    event_type: str
    timestamp: str
    signature: str
    raw_body: bytes
    payload: dict[str, Any]


@dataclass(slots=True)
class CallbackExecution:
    """What the workflow did with one delivery.

    A value object rather than a raised exception for the refusals, because a
    refusal is a **normal outcome of processing a callback**: the endpoint above has
    to answer the provider with an HTTP status and a business code, and turning
    "amount mismatch" into an exception would either lose the HTTP-200 duplicate
    case or force that mapping into this module.

    ``uncommitted`` is the important flag, and it means exactly what it says: the
    transaction was rolled back, so nothing the workflow read or wrote is visible
    and the event id is free again. The service uses it to decide between commit and
    rollback rather than inspecting the other flags.

    ``callback`` is ``None`` when the claim itself could not be taken (a lost insert
    race whose winning row is not yet visible) - there is then no row of ours to
    update, and creating one would defeat the unique index.
    """

    callback: PaymentCallback | None
    payment: Payment | None = None
    order: Order | None = None
    replayed: bool = False
    duplicate: bool = False
    processed: bool = False
    uncommitted: bool = False
    status: str | None = None
    error_code: str | None = None
    detail: str | None = None

    @property
    def applied(self) -> bool:
        """Whether this delivery owned the business effect and committed it."""
        return self.processed and not self.replayed and not self.uncommitted


class PaymentSuccessWorkflow:
    """Settle one payment from one verified provider callback.

    Constructed per transaction. The session is **not** committed by ``execute``:
    the caller commits, because the caller is the one that knows whether the
    transaction boundary it opened is the right one to close (an HTTP request
    handler and the FG-11 gate open different scopes). This is a deliberate
    difference from ``CreateOrderWorkflow``, which owns its own commit - there the
    whole request *is* the transaction, here a single transaction may process one
    callback among several.
    """

    def __init__(self, session: Session, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._payments = PaymentRepository(session)
        self._callbacks = PaymentCallbackRepository(session)
        self._orders = OrderRepository(session)
        self._logs = OrderStatusLogRepository(session)
        self._inventory = InventoryService(session)
        #: The outbox seam (step 10). Shares the caller's session on purpose: the
        #: event row must commit with the settlement, so it is appended to *this*
        #: transaction rather than written after the caller commits.
        self._outbox = OutboxWriter(session)

    # -- public ----------------------------------------------------------
    def execute(self, request: CallbackRequest) -> CallbackExecution:
        """Process one delivery. Commits iff it applied an effect; else rolls back.

        Returns a :class:`CallbackExecution` for every outcome, including the
        refusals. It raises only for a defect that is not a business outcome - a
        database error, or a callback row that vanished underneath us - because those
        must not be reported to a provider as if they were an answer.
        """
        execution = self._process(request)
        if execution.uncommitted:
            self._session.rollback()
        else:
            self._session.commit()
        return execution

    # -- the transaction body --------------------------------------------
    def _process(self, request: CallbackRequest) -> CallbackExecution:
        # -- step 1 & 2: claim the event, then record what we know ----------
        # The insert is attempted, never pre-checked: this is the FG-11
        # serialisation point, and it happens BEFORE any business read so a
        # duplicate can never reach the amount guard or the deduction.
        callback = self._claim(request)
        if callback is None:
            # Lost the insert race to a transaction that has not committed yet, so
            # its row is not visible to us. This is the duplicate case: the winner
            # owns the effect and our answer is the idempotent success. No row of
            # ours exists to mark, and creating one would defeat the unique index -
            # which is exactly what makes the index the serialisation point.
            logger.info(
                "payment callback lost the insert race",
                provider=request.provider,
                event_id=request.event_id,
            )
            return CallbackExecution(
                callback=None,
                replayed=True,
                duplicate=True,
                error_code="PAYMENT_CALLBACK_DUPLICATE",
                detail="another delivery of this event owns the business effect",
            )

        if callback.process_status == CallbackProcessStatus.PROCESSED.value:
            # The winner committed between our failed insert and this read. Same
            # answer as above: an idempotent success, no second effect.
            logger.info(
                "payment callback replayed",
                provider=request.provider,
                event_id=request.event_id,
                payment_no=callback.payment_no,
            )
            return CallbackExecution(
                callback=callback,
                replayed=True,
                duplicate=True,
                error_code="PAYMENT_CALLBACK_DUPLICATE",
                status=CallbackProcessStatus.PROCESSED.value,
                detail="this event was already processed",
            )

        # -- step 2b: the signature ------------------------------------------
        # Verified here rather than at the edge so that a direct caller (the
        # FG-11 gate drives the workflow in threads, and the mock-pay endpoint
        # calls it in-process) cannot bypass it by not being an HTTP handler.
        verdict = verify_signature(
            raw_body=request.raw_body,
            headers=CallbackHeaders(
                provider=request.provider,
                event_id=request.event_id,
                timestamp=request.timestamp,
                signature=request.signature,
            ),
            secret=self._secret_for(request.provider),
            max_skew_seconds=self._settings.PAYMENT_CALLBACK_MAX_SKEW_SECONDS,
        )
        if not verdict.valid:
            # Refused, and the row records *that* it was refused - an invalid
            # signature must never produce a row that claims success (design
            # section 7). Then the whole transaction is rolled back, so nothing is
            # claimed and a correctly signed redelivery of the same event id is
            # still possible.
            logger.warning(
                "payment callback signature refused",
                provider=request.provider,
                event_id=request.event_id,
                reason=verdict.reason,
            )
            return CallbackExecution(
                callback=callback,
                uncommitted=True,
                error_code="PAYMENT_CALLBACK_INVALID_SIGNATURE",
                detail=verdict.reason,
            )

        # -- step 2c: the payload snapshot, and resolving the payment --------
        amount = parse_amount_minor(request.payload.get("amount"))
        payment_no = self._resolve_payment_no(request)
        order_no = self._resolve_order_no(request)
        callback.payment_no = payment_no
        callback.order_no = order_no
        callback.payload_snapshot = filter_callback_payload(request.payload)
        callback.signature_valid = True
        callback.process_error = None

        if payment_no is None:
            # An unknown payment cannot be settled, and the honest answer is 404.
            # The row is still recorded (design 5.2 keeps ``payment_no`` nullable
            # precisely for this) so an operator can triage a provider that is
            # sending events this deployment cannot resolve.
            self._callbacks.mark_ignored(callback, error="unresolved payment_no")
            logger.warning(
                "payment callback could not resolve a payment",
                provider=request.provider,
                event_id=request.event_id,
            )
            return CallbackExecution(
                callback=callback,
                uncommitted=True,
                error_code="PAYMENT_NOT_FOUND",
                detail="the payload names no resolvable payment_no",
            )

        # -- step 3: lock the payment ---------------------------------------
        payment = self._payments.get_by_payment_no_for_update(payment_no)
        if payment is None:
            self._callbacks.mark_ignored(callback, error="payment_no not found")
            logger.warning(
                "payment callback names a payment that does not exist",
                provider=request.provider,
                event_id=request.event_id,
                payment_no=payment_no,
            )
            return CallbackExecution(
                callback=callback,
                uncommitted=True,
                error_code="PAYMENT_NOT_FOUND",
                detail="payment_no did not resolve to a row",
            )

        callback.merchant_id = payment.merchant_id
        callback.order_no = payment.order_no

        if payment.status == PaymentRecordStatus.SUCCESS.value:
            # A concurrent delivery that lost the *insert* race but reached here
            # because its counterpart has since committed is handled in step 1.
            # Reaching this branch means something else settled this payment - a
            # distinct event id delivered twice, or a second event for the same
            # payment. Either way the money is already in and there is nothing to
            # do but record it: this is a replay, not a second settlement.
            self._callbacks.mark_processed(callback)
            self._audit_replay(payment, request)
            logger.info(
                "payment already settled; callback recorded as a replay",
                provider=request.provider,
                event_id=request.event_id,
                payment_no=payment.payment_no,
            )
            return CallbackExecution(
                callback=callback,
                payment=payment,
                replayed=True,
                duplicate=True,
                error_code="PAYMENT_CALLBACK_DUPLICATE",
                status=payment.status,
                detail="this payment was already settled before this delivery",
            )

        if payment.status not in SETTLEABLE_STATUSES:
            decided = self._refuse_state(callback, payment)
            return CallbackExecution(
                callback=callback,
                payment=payment,
                replayed=False,
                uncommitted=True,
                error_code=decided.code,
                status=payment.status,
                detail=decided.detail,
            )

        # -- step 4: the amount guard ---------------------------------------
        # The provider's figure against the *payment row*: what the customer was
        # asked to pay. A mismatch refuses and the payment is NOT marked success.
        if amount is None or amount != payment.amount:
            self._callbacks.mark_failed(
                callback,
                error=f"amount mismatch: provider={amount} payment={payment.amount}",
            )
            logger.warning(
                "payment callback amount mismatch",
                provider=request.provider,
                event_id=request.event_id,
                payment_no=payment.payment_no,
                reported=amount,
                expected=payment.amount,
            )
            return CallbackExecution(
                callback=callback,
                payment=payment,
                uncommitted=True,
                error_code="PAYMENT_AMOUNT_MISMATCH",
                status=payment.status,
                detail=f"provider reported {amount}, the payment asks for {payment.amount}",
            )

        # -- step 5: lock the order -----------------------------------------
        order = self._orders.get_by_order_no_for_update(payment.order_no)
        if order is None:
            self._callbacks.mark_failed(callback, error="order not found")
            return CallbackExecution(
                callback=callback,
                payment=payment,
                uncommitted=True,
                error_code="ORDER_NOT_FOUND",
                detail="the payment names an order that does not exist",
            )

        if order.payment_status == PaymentStatus.PAID.value:
            # The order is already paid. Treat as a replay for the same reason as
            # step 3: the money is in, and the *only* thing a second effect could
            # do here is move stock that has already moved.
            self._callbacks.mark_processed(callback)
            self._audit_replay(payment, request)
            return CallbackExecution(
                callback=callback,
                payment=payment,
                order=order,
                replayed=True,
                duplicate=True,
                error_code="PAYMENT_CALLBACK_DUPLICATE",
                status=payment.status,
                detail="the order is already paid",
            )

        if order.order_status != OrderStatus.PENDING_PAYMENT.value:
            if order.order_status == OrderStatus.PROCESSING.value:
                # The duplicate-delivery case that slipped past the payment guard:
                # the order moved to PROCESSING with this payment still PAYING,
                # which only a partially applied settlement can produce.
                self._callbacks.mark_processed(callback)
                return CallbackExecution(
                    callback=callback,
                    payment=payment,
                    order=order,
                    replayed=True,
                    duplicate=True,
                    error_code="PAYMENT_CALLBACK_DUPLICATE",
                    status=payment.status,
                    detail="the order is already being processed",
                )
            self._callbacks.mark_failed(
                callback, error=f"order is {order.order_status}, not payable"
            )
            logger.warning(
                "payment callback for an order that is not payable",
                provider=request.provider,
                event_id=request.event_id,
                order_no=order.order_no,
                order_status=order.order_status,
            )
            return CallbackExecution(
                callback=callback,
                payment=payment,
                order=order,
                uncommitted=True,
                error_code="PAYMENT_ALREADY_PAID",
                status=order.order_status,
                detail=f"order_status is {order.order_status}",
            )

        # -- step 6: the settlement writes ----------------------------------
        now = utc_now()
        # `paid_amount` is written from the **verified callback's** amount, which
        # step 4 has established equals `payment.amount`. Copying the requested
        # amount instead is how a "settled" row ends up recording money that never
        # arrived.
        self._payments.record_success(
            payment,
            paid_amount=amount,
            external_transaction_no=self._resolve_external_transaction_no(request),
            paid_at=now,
        )
        order.payment_status = PaymentStatus.PAID.value
        order.paid_amount = order.payable_amount
        order.paid_at = now
        order.order_status = OrderStatus.PROCESSING.value
        order.updated_at = now
        self._orders.touch_version(order)

        # Exactly one status-log row per settlement, `operator_type = SYSTEM`
        # because no human caused this: a provider did, and the audit trail must
        # not attribute money movement to a person who was not there.
        self._logs.append(
            order=order,
            from_status=OrderStatus.PENDING_PAYMENT.value,
            to_status=OrderStatus.PROCESSING.value,
            operator_type=LogOperatorType.SYSTEM.value,
            operator_id=None,
            reason=ORDER_PROCESSING_REASON,
            trace_id=None,
        )
        CouponService(self._session).mark_used(order=order, now=now)

        # -- step 7: the deduction, one movement per line -------------------
        items = self._orders.items_for(order.id)
        if not items:
            # A paid order with no lines is a corrupt row, and silently continuing
            # would settle money against nothing. Refusing rolls the whole
            # transaction back, including the callback claim.
            self._callbacks.mark_failed(callback, error="order has no line items")
            return CallbackExecution(
                callback=callback,
                payment=payment,
                order=order,
                uncommitted=True,
                error_code="ORDER_AMOUNT_MISMATCH",
                detail="the paid order carries no order_items",
            )
        self._deduct(items, order=order)

        # -- step 8: the fulfillment shell ----------------------------------
        # One UNFULFILLED package carrying one line per order line at full
        # quantity: the "goods exist to be shipped" record. The split into
        # packages happens later, at the ship endpoint.
        self._create_shell(order=order, items=items)

        # -- step 9: the fulfillment axis is NOT touched --------------------
        # Design 6.1 step 9: creating a package is not shipping it. The order stays
        # UNFULFILLED; `POST /fulfillments/{id}/ship` is the only writer that moves
        # this axis, and a settlement that pre-set it would tell an operator the
        # goods had left when they are still on the shelf.

        # -- step 10: the outbox seam ---------------------------------------
        # §49: the event row joins *this* transaction, so it commits with the
        # settlement or not at all - that is what the position is for.
        #
        # REQ-PAY-004's fourth clause is served by TWO independent guards, and it is
        # worth naming both because either one alone looks sufficient:
        #   * the guards above make a duplicate **unreachable** - a repeat delivery is
        #     answered in step 1 (the unique index) or caught by the SUCCESS/PAID
        #     guards in steps 3 and 5, so this block runs once per settlement;
        #   * `enqueue` additionally dedups on `(event_type, aggregate_type,
        #     aggregate_id)`, so even a block moved above those guards could not emit
        #     a second row for the same payment.
        # A mutation that moves this block above the guards therefore does NOT redden
        # FG-11 on its own - `uq_event_type_aggregate` masks it, and the masking was
        # measured. That is a fact about the test's sensitivity, not a licence to move
        # it: do not move this block, and do not add a pre-check of its own, because a
        # pre-check is a check-then-act race and the unique index already decides
        # correctly.
        self._outbox.enqueue(
            event_type=OutboxEventType.PAYMENT_SETTLED.value,
            aggregate_type=OutboxAggregateType.PAYMENT.value,
            aggregate_id=payment.id,
            # The emitter's own key: the provider event that caused the settlement,
            # namespaced by provider so two providers' ids cannot be confused. It is
            # traceability; the dedup anchor is the aggregate above.
            idempotency_key=f"{request.provider}:{request.event_id}",
            payload=payment_settled_payload(
                payment_no=payment.payment_no,
                order_no=order.order_no,
                amount=payment.amount,
                provider=request.provider,
            ),
            merchant_id=payment.merchant_id,
        )

        # -- step 11: finalise the callback ---------------------------------
        self._callbacks.mark_processed(callback)

        logger.info(
            "payment settled",
            provider=request.provider,
            event_id=request.event_id,
            payment_no=payment.payment_no,
            order_no=order.order_no,
            amount=payment.amount,
            item_count=len(items),
        )
        return CallbackExecution(
            callback=callback,
            payment=payment,
            order=order,
            replayed=False,
            duplicate=False,
            processed=True,
            status=payment.status,
        )

    # -- pieces ----------------------------------------------------------
    def _claim(self, request: CallbackRequest) -> PaymentCallback | None:
        """Attempt the callback insert; ``None`` when the event is already claimed.

        ``insert_received`` wraps the insert in a SAVEPOINT, so the duplicate-key
        failure is confined and this transaction stays usable. Anything else that
        could not be explained by the unique index is re-raised by the repository -
        it is not a replay, and reporting it as one would silently swallow a real
        defect.
        """
        inserted = self._callbacks.insert_received(
            provider=request.provider,
            provider_event_id=request.event_id,
            event_type=request.event_type,
            received_at=utc_now(),
            # The verdict is not known yet at claim time; it is written in step 2.
            # Recording `False` here and `True` on success would be a lie if the
            # row were read before step 2 - so the column is left at its default and
            # set exactly once, below.
            signature_valid=False,
        )
        return inserted.callback if inserted.inserted else None

    def _secret_for(self, provider: str) -> str | None:
        from app.modules.payment.providers import callback_secret_for

        return callback_secret_for(self._settings, provider)

    def _resolve_payment_no(self, request: CallbackRequest) -> str | None:
        """Read the payment identifier out of the provider's payload.

        Tolerates the three spellings a provider actually uses - ``payment_no``,
        ``out_trade_no`` (the Alipay vocabulary) and ``payment_id`` - because a
        real integration maps a provider's field names rather than dictating them.
        Returns ``None`` rather than raising: an unresolvable delivery is recorded
        as ``IGNORED`` and answered 404, which is a business outcome, not a defect.
        """
        for key in ("payment_no", "out_trade_no", "payment_id", "merchant_payment_no"):
            value = request.payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _resolve_order_no(self, request: CallbackRequest) -> str | None:
        """Best-effort order number, denormalised onto the callback for triage."""
        for key in ("order_no", "merchant_order_no"):
            value = request.payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _resolve_external_transaction_no(self, request: CallbackRequest) -> str | None:
        """The provider's own transaction id, under whichever name it uses.

        Bounded to the column's ``VARCHAR(128)``: a provider that posts something
        longer must not make the settlement fail at flush time, because by then the
        money question has already been decided and the only effect of failing is a
        retry that settles nothing.
        """
        for key in ("transaction_no", "trade_no", "transaction_id", "external_transaction_no"):
            value = request.payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:128]
        return None

    def _refuse_state(self, callback: PaymentCallback, payment: Payment):
        """Record a payment whose state cannot be settled, and say why.

        Returns a tiny object carrying the business code and a log-safe detail so
        the caller can build one :class:`CallbackExecution` without repeating the
        branch.
        """
        detail = (
            f"payment status is {payment.status}; "
            "only INITIATED/PAYING can be settled"
        )
        self._callbacks.mark_failed(callback, error=detail)
        logger.warning(
            "payment callback refused by the state guard",
            payment_no=payment.payment_no,
            status=payment.status,
        )
        return _Refusal(code="PAYMENT_STATE_INVALID", detail=detail)

    def _deduct(self, items: list[OrderItem], *, order: Order) -> None:
        """One ``ORDER_DEDUCT`` per line, under the frozen deterministic key.

        ``reference_type=ORDER_ITEM`` with ``reference_id=order_item.id`` so INV-007
        ("the ledger explains every stock change") stays answerable without decoding
        the key string. The operator is ``SYSTEM``: a provider settled this, not a
        person, and attributing the movement to a human is an audit lie.
        """
        for line in items:
            self._inventory.deduct(
                sku_id=line.sku_id,
                quantity=line.quantity,
                warehouse_id=line.warehouse_id,
                idempotency_key=order_deduct_key(
                    order_no=order.order_no, order_item_id=line.id
                ),
                reference_type=ReferenceType.ORDER_ITEM,
                reference_id=line.id,
                operator_type=MovementOperatorType.SYSTEM,
                operator_id=None,
            )

    def _create_shell(self, *, order: Order, items: list[OrderItem]) -> None:
        """Create the fulfillment shell through the fulfillment module's service.

        Imported **inside the method** for two reasons, both structural rather than
        stylistic:

        * the payment module must not import the fulfillment module at import time,
          or a partially-implemented fulfillment module would stop the payment
          endpoints from loading - and the reverse dependency (fulfillment's ship
          path reading orders) already exists, so a module-level import on this side
          is the edge that would create a cycle;
        * the seam stays visible. Design section 6.1 step 8 names one call, and a
          reader looking for "who creates the package" finds it here.

        ``create_shell`` is idempotent per order (it returns the existing unshipped
        shell rather than appending a second), which is what makes a workflow retried
        after a crash between the deduction and the commit safe. That guard lives in
        the fulfillment service, not here, so the two callers of ``create_shell``
        (this workflow and a future admin repair path) cannot disagree about it.
        """
        from app.modules.fulfillment.service import FulfillmentService

        FulfillmentService(self._session).create_shell(
            order=order,
            items=items,
            # Every line of an order is locked from the same warehouse at create
            # time, and `order_items.warehouse_id` is the recorded truth about which
            # one (section 27). Using the first line's warehouse rather than
            # re-resolving the default is deliberate: the shell must name the
            # warehouse the stock was actually deducted from, or the package cannot
            # be reconciled against the ledger (INV-007).
            warehouse_id=items[0].warehouse_id,
        )

    def _audit_replay(self, payment: Payment, request: CallbackRequest) -> None:
        """Log a delivery that had no effect, without leaking the payload.

        Only identifiers and the outcome. The filtered snapshot is in the row; the
        log line deliberately carries none of it, because a log is read by more
        people and kept longer than a table with a retention policy.
        """
        logger.info(
            "payment callback had no business effect",
            provider=request.provider,
            event_id=request.event_id,
            payment_no=payment.payment_no,
            payment_status=payment.status,
        )


@dataclass(frozen=True, slots=True)
class _Refusal:
    """Internal: a business code plus a log-safe detail for one refusal branch."""

    code: str
    detail: str

