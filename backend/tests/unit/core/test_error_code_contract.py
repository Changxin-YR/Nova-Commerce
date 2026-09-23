"""The business-code contract: a code's HTTP status must be declared, not guessed.

Spec section 95 makes the business code the stable, client-visible identity and the
HTTP status a transport concern. That split only works if a code never changes its
status by accident, so these tests pin the answer for every code Phase 5 added.

## The defect these exist for

``http_status_for`` computed ``family = int(code) // 10_000`` and then asked whether
that value was in ``{30, 40, 50, 60, 70, 80, 90, 100}``. ``80_000 // 10_000`` is
**8**, not 80, so the test was False for every code in the 60xxx, 70xxx, 80xxx, 90xxx
and 100xxx ranges and the function fell through to 400. Observed before the fix:

    PAYMENT_NOT_FOUND      60000 -> 400   (documented: 404)
    FULFILLMENT_NOT_FOUND  70000 -> 400   (documented: 404)
    AFTER_SALE_NOT_FOUND   80000 -> 400   (documented: 404)
    REFUND_NOT_FOUND       80003 -> 400   (documented: 404)

It was invisible because **every** error class in the codebase declares its own
``http_status``, so the fallback had never been consulted. A test that asked "does
this code have a status?" would have passed; the property that was actually broken is
"does the fallback agree with the declared answer?", and that is what is asserted here.

## The known, deliberate gap

Error classes are added per phase, and Phase 5 added the twenty codes it needs. Codes
belonging to modules that do not exist yet (agent, MCP, governance, knowledge) have
no class, by design: a class nobody can raise is speculative code. ``PHASE5_CODES``
below is the set that must be complete now, and the explicit
``CODES_WITHOUT_A_CLASS_YET`` list records the rest so that this file never pretends
the gap is smaller than it is.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest

import app.core.errors as errors_module
from app.core.errors import AppError, ErrorCode, http_status_for

#: Every concrete error class, keyed by the code it carries.
_CLASS_FOR_CODE: dict[ErrorCode, type[AppError]] = {}
for _obj in vars(errors_module).values():
    if isinstance(_obj, type) and issubclass(_obj, AppError) and _obj is not AppError:
        _CLASS_FOR_CODE.setdefault(_obj.code, _obj)

#: The codes whose class Phase 5 is responsible for (sections 42-46). Every one of
#: these must exist: they are raised by code that shipped in this phase.
PHASE5_CODES = frozenset(
    code
    for code in ErrorCode
    if 60_000 <= int(code) < 90_000
)

#: The exact set of codes with no exception class, observed after Phase 5.
#:
#: Captured as an inventory for one reason: a code with no class can only be raised
#: as a generic ``ValidationError`` (wrong, stable code lost) or through an ad-hoc
#: class built in the raising module (a second implementation of the error
#: contract). Phase 5's author hit exactly that with five 60xxx-80xxx codes and had
#: to stop and ask, because the design document had said they were "already
#: defined". This list makes the gap countable instead of discoverable.
#:
#: It may only shrink. Adding a class means deleting the name here; adding a code
#: without a class fails the test below.
CLASSES_NOT_YET_DEFINED = frozenset(
    {
        "OK",  # 0: success, never raised
        "METHOD_NOT_ALLOWED",
        # catalog edges
        "PRODUCT_ALREADY_PUBLISHED",
        "PRODUCT_STATE_INVALID",
        "IMAGE_UPLOAD_REJECTED",
        # inventory edges
        "INVENTORY_ADJUSTMENT_INVALID",
        "INVENTORY_MOVEMENT_DUPLICATE",
        # cart (there is no server-side cart resource in V1)
        "CART_ITEM_NOT_FOUND",
        # addresses
        "ADDRESS_NOT_OWNED",
        # marketing
        "PROMOTION_RULE_INVALID",
        "COUPON_THRESHOLD_NOT_MET",
        # knowledge / RAG (Phase 11)
        "KNOWLEDGE_BASE_NOT_FOUND",
        "DOCUMENT_DUPLICATE",
        "DOCUMENT_PARSE_FAILED",
        # agent (Phase 10)
        "AGENT_GRAPH_VERSION_MISMATCH",
        # pending action (Phase 10/13)
        "PENDING_ACTION_ALREADY_DECIDED",
        # storage
        "BUCKET_UNAVAILABLE",
        # governance (Phase 13)
        "AUDIT_WRITE_FAILED",
        "RATE_LIMIT_EXCEEDED",
        "FEATURE_DISABLED",
    }
)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        # The four codes the divisor defect sent to 400. Regression guards: each was
        # observed at 400 before the fix.
        (ErrorCode.PAYMENT_NOT_FOUND, HTTPStatus.NOT_FOUND),
        (ErrorCode.FULFILLMENT_NOT_FOUND, HTTPStatus.NOT_FOUND),
        (ErrorCode.AFTER_SALE_NOT_FOUND, HTTPStatus.NOT_FOUND),
        (ErrorCode.REFUND_NOT_FOUND, HTTPStatus.NOT_FOUND),
        # The state conflicts.
        (ErrorCode.PAYMENT_STATE_INVALID, HTTPStatus.CONFLICT),
        (ErrorCode.FULFILLMENT_STATE_INVALID, HTTPStatus.CONFLICT),
        (ErrorCode.FULFILLMENT_ALREADY_SHIPPED, HTTPStatus.CONFLICT),
        (ErrorCode.AFTER_SALE_STATE_INVALID, HTTPStatus.CONFLICT),
        (ErrorCode.AFTER_SALE_NOT_ELIGIBLE, HTTPStatus.CONFLICT),
        (ErrorCode.REFUND_EXCEEDS_PAID_AMOUNT, HTTPStatus.CONFLICT),
        (ErrorCode.REFUND_EXCEEDS_ITEM_AMOUNT, HTTPStatus.CONFLICT),
        (ErrorCode.REFUND_ALREADY_COMPLETED, HTTPStatus.CONFLICT),
        (ErrorCode.PAYMENT_ALREADY_PAID, HTTPStatus.CONFLICT),
        (ErrorCode.PAYMENT_AMOUNT_MISMATCH, HTTPStatus.CONFLICT),
        # The 422 input refusals.
        (ErrorCode.PAYMENT_CHANNEL_UNSUPPORTED, HTTPStatus.UNPROCESSABLE_ENTITY),
        (ErrorCode.REFUND_AMOUNT_INVALID, HTTPStatus.UNPROCESSABLE_ENTITY),
        # The 403 refusal.
        (ErrorCode.PAYMENT_MOCK_DISABLED, HTTPStatus.FORBIDDEN),
        # The deliberate 200: a provider retries until it sees success, so a
        # duplicate delivery that was already applied must answer as handled
        # (API_CONTRACT section 15.3).
        (ErrorCode.PAYMENT_CALLBACK_DUPLICATE, HTTPStatus.OK),
    ],
)
def test_phase_5_codes_resolve_to_their_documented_status(
    code: ErrorCode, expected: HTTPStatus
) -> None:
    cls = _CLASS_FOR_CODE[code]
    assert cls().status_code == int(expected), f"{cls.__name__} answered {cls().status_code}"
    # And the fallback agrees, so a future class that forgets its explicit status
    # still produces the documented answer instead of a plausible-looking other.
    assert http_status_for(code) == int(expected)


def test_the_duplicate_payment_callback_is_a_success_not_an_error() -> None:
    """The one 2xx code in an error family, and why it is not a mistake.

    A provider retries a callback until it sees a success response. Answering an
    error to a delivery that was in fact already applied makes it retry forever.
    """
    from app.core.errors import PaymentCallbackDuplicateError

    assert PaymentCallbackDuplicateError().status_code == 200


def test_the_classless_codes_are_a_closed_inventory() -> None:
    """The gap is exact, and it may only shrink.

    * a code that is classless but unrecorded fails - that is the early warning;
    * a recorded name that now has a class fails - that keeps the list believable,
      because a stale list is a list nobody trusts.

    Phase 5's own codes are covered by the tests above; this one covers everything
    else and deliberately does not claim they are fine. They belong to the phases
    that will raise them.
    """
    classless = {code.name for code in ErrorCode if code not in _CLASS_FOR_CODE}

    unrecorded = sorted(classless - CLASSES_NOT_YET_DEFINED)
    assert not unrecorded, (
        "these codes have no exception class and are not in the recorded inventory, "
        f"so the gap is growing silently: {unrecorded}"
    )

    closed = sorted(CLASSES_NOT_YET_DEFINED - classless)
    assert not closed, (
        "these codes now have a class but are still listed as missing - delete them "
        f"from CLASSES_NOT_YET_DEFINED: {closed}"
    )
