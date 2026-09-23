"""Provider callback endpoint - the only route in this application with **no JWT**.

API_CONTRACT section 15.3, REQ-PAY-005, design section 7.

    POST /api/v1/payments/callbacks/{provider}

## Why this route does not use the bearer token

A payment provider cannot hold a user's bearer token, and section 15.3 is explicit
that it must not be able to act as one. The provider's authentication model is
therefore a **signature over the raw request body**:

    X-Provider-Event-Id:  <the provider's own event id>   # the idempotency key
    X-Provider-Timestamp: <unix seconds>
    X-Provider-Signature: <hex HMAC-SHA256(secret, f"{timestamp}.{raw_body}")>

The timestamp is inside the signed material rather than checked separately, because a
signature over the body alone authenticates *content* and not *freshness*: a captured
body would stay valid forever and could be replayed. Binding it means a captured
request is useless once the skew window closes, and forging a new one requires the
secret. A body older (or newer) than ``PAYMENT_CALLBACK_MAX_SKEW_SECONDS`` is refused
even when its signature verifies - that is the attack this exists for.

The provider is a **path** parameter rather than a header, because the path is what
the route is mounted on and a redundant header would be a second place for the two to
disagree.

## The raw body is read as bytes

``await request.body()``, never a Pydantic model. A verifier that parsed the JSON and
re-serialised it would refuse every honest provider whose key order or spacing differs
from Python's - and, worse, would accept a *reordered* payload whose round-trip
happened to match. The signature is over bytes, so bytes are what this handler reads.

## Why the decorator spells the whole path

``app/api/v1/router.py`` mounts every submodule at the module prefix (``/payments``) and
uses the submodule name only as the OpenAPI tag, so the sub-path is the router file's
responsibility. ``fulfillment/api/admin.py`` and ``aftersales/api/admin.py`` do the same -
``@router.get("/admin")`` and ``@router.get("/admin/after-sales")`` respectively - and the
consequence of forgetting it is a route that exists at a plausible-but-wrong URL:
``/{provider}`` put this handler at ``POST /api/v1/payments/MOCK``, which would have passed
a "is it registered?" check and 404'd every real callback.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import (
    ErrorCode,
    PaymentAmountMismatchError,
    PaymentCallbackDuplicateError,
    PaymentCallbackSignatureError,
    PaymentNotFoundError,
    PaymentStateInvalidError,
    envelope,
)
from app.core.logging import get_logger
from app.modules.payment.providers import (
    CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED,
    CallbackHeaders,
    callback_secret_for,
    verify_signature,
)
from app.modules.payment.serializers import to_callback_ack, to_payment
from app.modules.payment.service import PaymentService
from app.modules.payment.workflow import CallbackExecution, CallbackRequest
from app.shared.db.session import get_session

logger = get_logger(__name__)

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]


def _settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(_settings_dep)]

EventIdHeader = Annotated[str, Header(alias="X-Provider-Event-Id", max_length=128)]
TimestampHeader = Annotated[str, Header(alias="X-Provider-Timestamp", max_length=32)]
SignatureHeader = Annotated[str, Header(alias="X-Provider-Signature", max_length=256)]

#: A callback body is a provider's document, bounded so a hostile or broken sender
#: cannot make the process hold an unbounded buffer before the signature is even
#: checked. 64 KiB is far beyond any real settlement notification.
MAX_CALLBACK_BODY_BYTES = 64 * 1024


@router.post(
    "/callbacks/{provider}",
    summary="Receive a signed settlement notification from a payment provider",
    description=(
        "No JWT: the provider authenticates with `X-Provider-Signature`, an "
        "HMAC-SHA256 over `f\"{timestamp}.{raw_body}\"` (section 15.3).\n\n"
        "Outcomes: a first verified delivery returns **200** with the settled "
        "payment; a duplicate `(provider, provider_event_id)` returns **200** with "
        "`code = 60004`, because a provider retries until it sees success; a bad, "
        "missing or stale signature is refused with no row claiming success; an "
        "amount mismatch and an unknown payment are refusals, not settlements."
    ),
)
async def receive_callback(
    provider: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    event_id: EventIdHeader,
    timestamp: TimestampHeader,
    signature: SignatureHeader,
) -> dict:
    raw_body = await request.body()
    if len(raw_body) > MAX_CALLBACK_BODY_BYTES:
        raise PaymentCallbackSignatureError(
            "the callback body exceeds the accepted size",
            context={"bytes": len(raw_body), "limit": MAX_CALLBACK_BODY_BYTES},
        )

    verdict = verify_signature(
        raw_body=raw_body,
        headers=CallbackHeaders(
            provider=provider,
            event_id=event_id,
            timestamp=timestamp,
            signature=signature,
        ),
        secret=callback_secret_for(settings, provider),
        max_skew_seconds=settings.PAYMENT_CALLBACK_MAX_SKEW_SECONDS,
    )

    payload = _parse_body(raw_body)
    callback_request = CallbackRequest(
        provider=provider,
        event_id=event_id,
        event_type=_event_type(payload),
        timestamp=timestamp,
        signature=signature,
        raw_body=raw_body,
        payload=payload,
    )

    if not verdict.valid:
        # The audit row is written (so "was this rejected for the right reason?" is
        # answerable from the table) and then the refusal is raised. An invalid
        # signature must never produce a row that claims success - design section 7.
        PaymentService(session, settings).record_refused_signature(
            callback_request, reason=verdict.reason
        )
        logger.warning(
            "payment callback refused",
            provider=provider,
            event_id=event_id,
            reason=verdict.reason,
        )
        raise PaymentCallbackSignatureError(
            "the callback signature could not be verified",
            context={"provider": provider, "event_id": event_id, "reason": verdict.reason},
        )

    execution = PaymentService(session, settings).process_callback(callback_request)
    return _respond(execution, provider=provider, event_id=event_id)


def _respond(execution: CallbackExecution, *, provider: str, event_id: str) -> dict:
    """Map a workflow outcome onto the frozen outcome table of section 15.3.

    The mapping is here, at the edge, rather than inside the workflow, because the
    workflow answers business questions ("was an effect applied?") and the edge answers
    transport ones ("what status does the provider see?"). Keeping them apart is what
    lets the FG-11 gate drive the workflow directly without an HTTP client.
    """
    if execution.replayed or execution.duplicate:
        # 200 + 60004. The one honest use of "200 = your request was already handled":
        # answering an error would make a provider retry an event that is already
        # applied, forever.
        return _duplicate_response(execution, event_id=event_id)

    if execution.uncommitted:
        raise _refusal_for(execution)

    if execution.payment is None:  # pragma: no cover - defensive
        raise PaymentNotFoundError(
            "the callback resolved no payment",
            context={"provider": provider, "event_id": event_id},
        )

    return envelope(data=to_payment(execution.payment).model_dump(mode="json"))


def _duplicate_response(
    execution: CallbackExecution, *, event_id: str
) -> dict:
    """The 200/60004 answer, carrying the *existing* state rather than a new effect.

    Built as an envelope rather than by raising, so the handler returns HTTP 200 with a
    body a provider can log. The envelope's ``code`` is the 60004 business code, which
    is what tells a human reading the provider's records that the delivery was
    deduplicated rather than that a *new* settlement happened.
    """
    payment = execution.payment
    return envelope(
        data=to_callback_ack(
            event_id=event_id,
            payment_no=None if payment is None else payment.payment_no,
            processed=True,
            replayed=True,
            duplicate=True,
            status=execution.status,
        ).model_dump(mode="json"),
        code=ErrorCode.PAYMENT_CALLBACK_DUPLICATE,
        message="this provider event was already processed",
    )


def _refusal_for(execution: CallbackExecution) -> Exception:
    """Translate a refusal code into the exception that carries its HTTP status.

    Every one of these already has a canonical status in
    ``app/core/errors.py::_DEFAULT_HTTP_STATUS``, so nothing here hand-picks one.
    """
    context = {"reason": execution.detail} if execution.detail else None
    match execution.error_code:
        case "PAYMENT_AMOUNT_MISMATCH":
            return PaymentAmountMismatchError(
                "the provider reported an amount that does not match the payment",
                context=context,
            )
        case "PAYMENT_NOT_FOUND":
            return PaymentNotFoundError(
                "the callback names a payment this deployment cannot resolve",
                context=context,
            )
        case "PAYMENT_STATE_INVALID":
            return PaymentStateInvalidError(
                "the payment is not in a state that can be settled",
                context=context,
            )
        case "PAYMENT_ALREADY_PAID":
            return PaymentCallbackDuplicateError(
                "the order for this callback is no longer payable",
                context=context,
            )
        case _:
            # An unmapped code is a defect in this module, not a business outcome.
            # Answering the provider with something generic is still better than a
            # 500 with a stack trace, but the code is logged so it gets fixed.
            logger.error(
                "unmapped callback refusal code",
                error_code=execution.error_code,
                detail=execution.detail,
            )
            return PaymentStateInvalidError(
                "the callback could not be applied", context=context
            )


def _parse_body(raw_body: bytes) -> dict[str, Any]:
    """Parse the provider's JSON body, tolerating anything that is not an object.

    Parsing happens **after** verification and never affects it: the signature is over
    the bytes above. A body that is not a JSON object becomes an empty payload, which
    the workflow reports as "no resolvable payment_no" - a 404 the provider can act on
    - rather than a 500 from this handler.
    """
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _event_type(payload: dict[str, Any]) -> str:
    """The provider's event type, defaulting to the settlement vocabulary's value.

    Kept as a free string in the table because it is the *provider's* vocabulary, not
    ours: a new event type must be storable without a migration. The default exists
    because this route currently accepts only settlement notifications, so a body that
    does not name its type is read as one.
    """
    value = payload.get("event_type")
    if isinstance(value, str) and value.strip():
        return value.strip()[:32]
    return CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED
