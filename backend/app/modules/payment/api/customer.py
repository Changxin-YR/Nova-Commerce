"""Consumer payment endpoints - the frozen paths of API_CONTRACT section 15.2.

Every handler does exactly four things: parse, take the authenticated principal, call
one service method, wrap the result in the section 95 envelope. There is no money
arithmetic, no state-machine logic and no SQL in this file.

## The paths are the frozen ones, not inventions

``frontend/src/api/endpoints.ts`` already calls
``/payments/customer/payments``, ``/{payment_id}``, ``/by-order/{order_no}`` and
``/{payment_id}/mock-pay``. Section 15.2 transcribes those rather than inventing a
parallel surface, so these are the routes the client is already written against.

## Why ``mock-pay`` lives here and its guard is asked twice

The endpoint is convenient (it lets the demo drive a real settlement from the
browser) and dangerous for exactly the same reason: it marks an order paid without
money moving. Two independent guards therefore stand in front of it:

1. this handler asks :func:`is_mock_allowed` and refuses with
   ``PAYMENT_MOCK_DISABLED (60006)`` unless ``PAYMENT_MOCK_ENABLED`` is true **and**
   ``APP_ENV`` is dev/test/demo;
2. ``Settings`` carries a validator that refuses to construct at all in
   staging/prod with the flag true.

The duplication is deliberate, not an oversight: a validator someone bypasses with
``model_construct()`` would otherwise be the only thing between a misconfigured
deployment and a forged settlement (INV-008).

The endpoint then goes through the **same** ``PaymentSuccessWorkflow`` a real
provider callback does - it does not settle anything itself - so the invariant FG-11
proves is the invariant the demo exercises.
"""

from __future__ import annotations

import json
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    IdempotencyKeyRequiredError,
    PaymentMockDisabledError,
    ValidationError,
    envelope,
)
from app.core.logging import get_logger
from app.modules.identity.dependencies import CurrentPrincipal
from app.modules.payment.enums import PaymentChannel
from app.modules.payment.providers import (
    CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED,
    sign_body,
)
from app.modules.payment.schemas import CreatePaymentRequest
from app.modules.payment.serializers import to_payment
from app.modules.payment.service import PaymentService, is_mock_allowed
from app.modules.payment.workflow import CallbackRequest
from app.shared.db.session import get_session

logger = get_logger(__name__)

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]

#: ``Idempotency-Key`` is a **header**, declared optional on purpose.
#:
#: Declaring it required would let FastAPI answer 422 with its own body shape, and
#: section 48 freezes ``IDEMPOTENCY_KEY_REQUIRED (10010)`` for this case. The explicit
#: check in the handler is what produces that code - the same pattern the order create
#: endpoint uses, and for the same reason: every payment create must be idempotent, so
#: omitting the key has to be a hard, loud failure at the edge rather than a validator
#: message the client may not map.
IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]


def _settings_dep() -> Settings:
    from app.core.config import get_settings

    return get_settings()


SettingsDep = Annotated[Settings, Depends(_settings_dep)]


@router.post(
    "/customer/payments",
    summary="Create a payment attempt for one of the caller's own orders",
    description=(
        "Requires an `Idempotency-Key` header. The amount is **never** a request "
        "input: the server reads `orders.payable_amount`, so a client cannot settle "
        "a large order by paying one unit.\n\n"
        "Creating an attempt moves the order's `payment_status` `UNPAID -> PAYING`. It "
        "does **not** move it to `PAID` - only a verified provider callback does that "
        "(section 15.1, REQ-PAY-005), and no endpoint a JWT user can reach can do it "
        "at all."
    ),
)
def create_payment(
    payload: CreatePaymentRequest,
    principal: CurrentPrincipal,
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> dict:
    if not idempotency_key:
        raise IdempotencyKeyRequiredError(
            "creating a payment attempt is idempotent by contract; "
            "supply an Idempotency-Key header"
        )

    service = PaymentService(session)
    result = service.create(
        principal=principal,
        order_no=payload.order_no,
        channel=payload.channel.value,
        client_request_id=payload.client_request_id,
        idempotency_key=idempotency_key,
    )
    logger.info(
        "payment create request served",
        payment_no=result.payment.payment_no,
        order_no=result.order.order_no,
        channel=result.payment.channel,
        user_id=principal.user_id,
    )
    body = to_payment(result.payment).model_dump(mode="json")
    body["replayed"] = result.replayed
    body["order_payment_status"] = result.order.payment_status
    return envelope(data=body)


@router.get(
    "/customer/payments/by-order/{order_no}",
    summary="The payment attempt for one of the caller's own orders",
    description=(
        "Owner only. A foreign order is `ORDER_NOT_FOUND (50003)` and a nonsensical "
        "one gets the same answer - distinguishing them would make this an existence "
        "oracle (section 109). An order with no attempt yet is "
        "`PAYMENT_NOT_FOUND (60000)`.\n\n"
        "Registered **before** `/{payment_id}` on purpose: a route registered later "
        "would never match, because the path parameter would swallow `by-order`."
    ),
)
def get_payment_by_order(order_no: str, principal: CurrentPrincipal, session: SessionDep) -> dict:
    payment = PaymentService(session).get_payment_for_order(
        principal=principal, order_no=order_no
    )
    return envelope(data=to_payment(payment).model_dump(mode="json"))


@router.get(
    "/customer/payments/{payment_id}",
    summary="One of the caller's own payment attempts",
    description=(
        "Owner only, applied in the query rather than after loading. A payment "
        "belonging to somebody else is `PAYMENT_NOT_FOUND (60000)`, never 403."
    ),
)
def get_payment(payment_id: int, principal: CurrentPrincipal, session: SessionDep) -> dict:
    payment = PaymentService(session).get_customer_payment(
        principal=principal, payment_id=payment_id
    )
    return envelope(data=to_payment(payment).model_dump(mode="json"))


@router.post(
    "/customer/payments/{payment_id}/mock-pay",
    summary="Settle a payment through the mock provider (DEV/DEMO ONLY)",
    description=(
        "Refused with `PAYMENT_MOCK_DISABLED (60006)` unless `PAYMENT_MOCK_ENABLED` is "
        "true **and** `APP_ENV` is `dev`, `test` or `demo`.\n\n"
        "This endpoint does not settle anything itself. It builds a provider payload, "
        "signs it with the same HMAC scheme a real provider uses, and calls "
        "`PaymentSuccessWorkflow` - so the invariants FG-11 proves are the invariants "
        "this route exercises. The request body has no status, no amount and no "
        "transaction id: a caller can ask for a settlement, never describe one."
    ),
)
def mock_pay(
    payment_id: int,
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: SettingsDep,
) -> dict:
    # No request body at all, deliberately. Section 15.2's table lists
    # `{client_request_id}` for this route, but a mock settlement is idempotent on
    # the payment row itself (a second call is answered as a replay by the state
    # guard), so the field would be a token the server ignores. Accepting a field
    # and ignoring it is worse than not accepting it: the client would believe it
    # controlled something. `MockPayRequest` documents the fields that must never
    # appear here (no status, no amount), which is why it stays as the frozen
    # shape even though the handler reads nothing.
    if not is_mock_allowed(settings):
        # Asked again here even though Settings validates it: one guard that can be
        # bypassed is not a guard (INV-008).
        raise PaymentMockDisabledError(
            "the mock payment surface is only available in dev/demo",
            context={"app_env": settings.APP_ENV},
        )

    service = PaymentService(session, settings)
    # Ownership first: a stranger's payment id must be PAYMENT_NOT_FOUND before any
    # settlement work happens, so this route cannot be used to probe for ids.
    payment = service.get_customer_payment(principal=principal, payment_id=payment_id)
    if payment.channel != PaymentChannel.MOCK.value:
        raise ValidationError(
            "only a MOCK-channel payment can be settled through the mock provider",
            context={"payment_no": payment.payment_no, "channel": payment.channel},
        )

    request = _build_mock_callback(
        payment_no=payment.payment_no, amount=payment.amount, settings=settings
    )
    execution = service.process_callback(request)
    if execution.uncommitted:
        # Refused inside the workflow: the state guard, the amount guard or the order
        # guard. The provider-facing route maps these to HTTP statuses; here the
        # customer is answered with the same business code, which is what the UI
        # branches on.
        return envelope(
            data=None,
            code=_error_code_for(execution.error_code),
            message=execution.detail or "the settlement was refused",
        )
    settled = execution.payment or payment
    logger.info(
        "mock payment settled",
        payment_no=settled.payment_no,
        order_no=settled.order_no,
        replayed=execution.replayed,
        user_id=principal.user_id,
    )
    return envelope(data=to_payment(settled).model_dump(mode="json"))


def _build_mock_callback(
    *, payment_no: str, amount: int, settings: Settings
) -> CallbackRequest:
    """Build the provider payload the mock channel posts to the real callback path.

    Signed with ``sign_body`` - the *same* function the verifier uses - so the mock
    exercises the frozen scheme rather than a parallel implementation of it. Two
    implementations of a signature scheme are how a suite passes against a verifier no
    provider agrees with.

    ``amount`` is passed in from the **payment row** the handler already loaded, never
    from the request and never from a fresh read: the mock provider reports what the
    row says, because the amount guard's whole job is to refuse a mismatch. A mock that
    reported a figure of its own choosing would be testing that guard with data it
    cannot disagree with, which proves nothing.
    """
    timestamp = str(int(time.time()))
    payload: dict[str, object] = {
        "payment_no": payment_no,
        "event_type": CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED,
        "transaction_no": f"MOCK{timestamp}",
        "amount": amount,
        "mock": True,
    }
    raw_body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    secret = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()
    return CallbackRequest(
        provider=PaymentChannel.MOCK.value,
        # The event id is derived from the payment and the second, so two mock-pay
        # calls are *distinct* provider events. That is deliberate: it means a second
        # mock-pay is refused by the **state guard** (the payment is already SUCCESS)
        # rather than absorbed by the unique index - which is exactly the negative
        # control FG-11 asserts, exercised here in production code instead of only in
        # the test.
        event_id=f"mock-{payment_no}-{timestamp}",
        event_type=CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED,
        timestamp=timestamp,
        signature=sign_body(secret=secret, timestamp=timestamp, raw_body=raw_body),
        raw_body=raw_body,
        payload=payload,
    )


def _error_code_for(error_code: str | None):
    """Map a workflow refusal code to the ``ErrorCode`` the envelope carries.

    The workflow reports *codes* rather than exceptions on purpose (a refusal is a
    normal outcome of processing a callback). The mapping has to live somewhere, and
    the edge is the right place: it is the only layer that knows the wire.
    """
    from app.core.errors import ErrorCode

    if error_code is None:
        return ErrorCode.INTERNAL_ERROR
    try:
        return ErrorCode[error_code]
    except KeyError:
        # An unrecognised name is a **defect in this module**, not a business outcome.
        # Mapping it to INTERNAL_ERROR rather than raising keeps a typo in a refusal code
        # from turning into a 500 for a customer whose payment was in fact untouched - the
        # settlement is unaffected either way, and that is the fact they care about. Logged
        # so the typo gets fixed.
        logger.error("unmapped payment refusal code", error_code=error_code)
        return ErrorCode.INTERNAL_ERROR
