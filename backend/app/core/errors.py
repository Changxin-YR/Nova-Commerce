"""Error contract: stable business codes, exception hierarchy and the HTTP
error envelope.

Spec references:
    §95  every ``/api/v1`` response is ``{code, message, data, trace_id}`` with
         *stable business error codes*.
    §109 security-relevant failures must not leak internals.
    §132 logs must never contain credentials or hidden chain-of-thought.

Design rules enforced here:

1. **A business code is part of the public contract.** Once assigned, a code
   may be deprecated but never reused for a different meaning, because clients
   branch on it.
2. **The numeric HTTP status is a transport concern; the business code is the
   semantic one.** Handlers therefore raise :class:`AppError` subclasses and
   never build ``JSONResponse`` by hand.
3. **Internal details are logged, never returned.** ``AppError.public_message``
   is what the client sees; ``detail`` is for logs and audit only.
"""

from __future__ import annotations

import enum
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import TRACE_ID_HEADER, get_context

# ---------------------------------------------------------------------------
# Business codes
# ---------------------------------------------------------------------------
# Layout: CC_NNN where CC is a domain prefix and NNN is a sequence number.
#  10 common | 20 identity | 30 catalog | 40 inventory | 50 order/pricing
#  60 payment | 70 fulfillment | 80 after-sales/refund | 90 marketing
# 100 knowledge/RAG | 110 agent | 120 MCP | 130 storage | 140 governance


class ErrorCode(enum.IntEnum):
    """Stable, client-visible business error codes."""

    OK = 0

    # -- 10xxx common ---------------------------------------------------
    INTERNAL_ERROR = 10_000
    VALIDATION_ERROR = 10_001
    NOT_FOUND = 10_002
    CONFLICT = 10_003
    RATE_LIMITED = 10_004
    METHOD_NOT_ALLOWED = 10_005
    PAYLOAD_TOO_LARGE = 10_006
    UNSUPPORTED_MEDIA_TYPE = 10_007
    SERVICE_UNAVAILABLE = 10_008
    DEPENDENCY_UNAVAILABLE = 10_009
    IDEMPOTENCY_KEY_REQUIRED = 10_010
    IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD = 10_011
    IDEMPOTENCY_REQUEST_IN_PROGRESS = 10_012

    # -- 20xxx identity / auth ------------------------------------------
    UNAUTHENTICATED = 20_000
    INVALID_CREDENTIALS = 20_001
    TOKEN_EXPIRED = 20_002
    TOKEN_INVALID = 20_003
    REFRESH_TOKEN_REUSED = 20_004
    SESSION_REVOKED = 20_005
    ACCOUNT_LOCKED = 20_006
    ACCOUNT_DISABLED = 20_007
    FORBIDDEN = 20_008
    INSUFFICIENT_PERMISSION = 20_009
    DATA_SCOPE_VIOLATION = 20_010
    CSRF_VALIDATION_FAILED = 20_011
    MERCHANT_MISMATCH = 20_012

    # -- 30xxx catalog ---------------------------------------------------
    PRODUCT_NOT_FOUND = 30_000
    PRODUCT_NOT_PUBLISHED = 30_001
    SKU_NOT_FOUND = 30_002
    SKU_NOT_AVAILABLE = 30_003
    CATEGORY_NOT_FOUND = 30_004
    BRAND_NOT_FOUND = 30_005
    PRODUCT_ALREADY_PUBLISHED = 30_006
    PRODUCT_STATE_INVALID = 30_007
    IMAGE_UPLOAD_REJECTED = 30_008

    # -- 40xxx inventory -------------------------------------------------
    INSUFFICIENT_STOCK = 40_000
    INVENTORY_NOT_FOUND = 40_001
    INVENTORY_CONFLICT_STALE_VERSION = 40_002
    INVENTORY_ADJUSTMENT_INVALID = 40_003
    WAREHOUSE_NOT_FOUND = 40_004
    INVENTORY_MOVEMENT_DUPLICATE = 40_005

    # -- 50xxx cart / pricing / order ------------------------------------
    CART_NOT_FOUND = 50_000
    CART_EMPTY = 50_001
    CART_ITEM_NOT_FOUND = 50_002
    ORDER_NOT_FOUND = 50_003
    ORDER_STATE_INVALID = 50_004
    ORDER_ALREADY_EXPIRED = 50_005
    ORDER_AMOUNT_MISMATCH = 50_006
    PRICE_CHANGED = 50_007
    ADDRESS_NOT_FOUND = 50_008
    ADDRESS_NOT_OWNED = 50_009
    ORDER_NOT_CANCELLABLE = 50_010
    ORDER_NOT_CONFIRMABLE = 50_011

    # -- 60xxx payment ---------------------------------------------------
    PAYMENT_NOT_FOUND = 60_000
    PAYMENT_ALREADY_PAID = 60_001
    PAYMENT_AMOUNT_MISMATCH = 60_002
    PAYMENT_CALLBACK_INVALID_SIGNATURE = 60_003
    PAYMENT_CALLBACK_DUPLICATE = 60_004
    PAYMENT_CHANNEL_UNSUPPORTED = 60_005
    PAYMENT_MOCK_DISABLED = 60_006
    PAYMENT_STATE_INVALID = 60_007

    # -- 70xxx fulfillment -----------------------------------------------
    FULFILLMENT_NOT_FOUND = 70_000
    FULFILLMENT_QUANTITY_EXCEEDS_ORDER = 70_001
    FULFILLMENT_STATE_INVALID = 70_002
    FULFILLMENT_ALREADY_SHIPPED = 70_003

    # -- 80xxx after-sales / refund --------------------------------------
    AFTER_SALE_NOT_FOUND = 80_000
    AFTER_SALE_NOT_ELIGIBLE = 80_001
    AFTER_SALE_STATE_INVALID = 80_002
    REFUND_NOT_FOUND = 80_003
    REFUND_EXCEEDS_PAID_AMOUNT = 80_004
    REFUND_EXCEEDS_ITEM_AMOUNT = 80_005
    REFUND_AMOUNT_INVALID = 80_006
    REFUND_ALREADY_COMPLETED = 80_007

    # -- 90xxx marketing -------------------------------------------------
    PROMOTION_NOT_FOUND = 90_000
    PROMOTION_CONFLICT = 90_001
    PROMOTION_RULE_INVALID = 90_002
    PROMOTION_PREVIEW_REQUIRED = 90_003
    COUPON_NOT_FOUND = 90_004
    COUPON_NOT_APPLICABLE = 90_005
    COUPON_EXPIRED = 90_006
    COUPON_ALREADY_USED = 90_007
    COUPON_ALREADY_LOCKED = 90_008
    COUPON_THRESHOLD_NOT_MET = 90_009

    # -- 100xxx knowledge / RAG ------------------------------------------
    KNOWLEDGE_BASE_NOT_FOUND = 100_000
    DOCUMENT_NOT_FOUND = 100_001
    DOCUMENT_STATE_INVALID = 100_002
    DOCUMENT_DUPLICATE = 100_003
    DOCUMENT_PARSE_FAILED = 100_004
    DOCUMENT_UNSUPPORTED_TYPE = 100_005
    DOCUMENT_TOO_LARGE = 100_006
    UPLOAD_SIGNATURE_MISMATCH = 100_007
    PATH_TRAVERSAL_DETECTED = 100_008
    RETRIEVAL_UNAVAILABLE = 100_009
    INSUFFICIENT_EVIDENCE = 100_010
    EMBEDDING_DIMENSION_MISMATCH = 100_011

    # -- 110xxx agent ----------------------------------------------------
    AGENT_RUN_NOT_FOUND = 110_000
    AGENT_BUDGET_EXCEEDED = 110_001
    AGENT_TOOL_NOT_FOUND = 110_002
    AGENT_TOOL_DISABLED = 110_003
    AGENT_TOOL_NOT_ALLOWED_FOR_AGENT = 110_004
    AGENT_TOOL_INPUT_INVALID = 110_005
    AGENT_TOOL_OUTPUT_INVALID = 110_006
    AGENT_ACTION_REQUIRES_APPROVAL = 110_007
    AGENT_ACTION_BLOCKED_BY_RISK = 110_008
    AGENT_GRAPH_VERSION_MISMATCH = 110_009
    AGENT_REVALIDATION_FAILED = 110_010
    AGENT_EXECUTION_RECEIPT_MISSING = 110_011
    AGENT_PROMPT_INJECTION_DETECTED = 110_012

    # -- 120xxx pending action / MCP -------------------------------------
    PENDING_ACTION_NOT_FOUND = 120_000
    PENDING_ACTION_STATE_INVALID = 120_001
    PENDING_ACTION_EXPIRED = 120_002
    PENDING_ACTION_ALREADY_DECIDED = 120_003
    PENDING_ACTION_PAYLOAD_CHANGED = 120_004
    MCP_TOKEN_MISSING = 120_005
    MCP_TOKEN_INVALID = 120_006
    MCP_TOKEN_WRONG_ISSUER = 120_007
    MCP_TOKEN_WRONG_AUDIENCE = 120_008
    MCP_TOKEN_INSUFFICIENT_SCOPE = 120_009
    MCP_ORIGIN_REJECTED = 120_010
    MCP_TOOL_NOT_EXPOSED = 120_011
    MCP_WRITE_NOT_PERMITTED = 120_012

    # -- 130xxx storage ---------------------------------------------------
    OBJECT_NOT_FOUND = 130_000
    OBJECT_STORAGE_UNAVAILABLE = 130_001
    OBJECT_CHECKSUM_MISMATCH = 130_002
    OBJECT_TOO_LARGE = 130_003
    BUCKET_UNAVAILABLE = 130_004

    # -- 140xxx governance ------------------------------------------------
    AUDIT_WRITE_FAILED = 140_000
    RATE_LIMIT_EXCEEDED = 140_001
    FEATURE_DISABLED = 140_002


#: Canonical HTTP status for each code family. Individual errors may override.
_DEFAULT_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.OK: status.HTTP_200_OK,
    ErrorCode.INTERNAL_ERROR: status.HTTP_500_INTERNAL_SERVER_ERROR,
    ErrorCode.VALIDATION_ERROR: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ErrorCode.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.CONFLICT: status.HTTP_409_CONFLICT,
    ErrorCode.RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
    ErrorCode.SERVICE_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    ErrorCode.UNAUTHENTICATED: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.FORBIDDEN: status.HTTP_403_FORBIDDEN,
    ErrorCode.PAYLOAD_TOO_LARGE: status.HTTP_413_CONTENT_TOO_LARGE,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
    ErrorCode.INSUFFICIENT_STOCK: status.HTTP_409_CONFLICT,
    ErrorCode.INVENTORY_CONFLICT_STALE_VERSION: status.HTTP_409_CONFLICT,
    ErrorCode.ORDER_STATE_INVALID: status.HTTP_409_CONFLICT,
    ErrorCode.PAYMENT_CALLBACK_DUPLICATE: status.HTTP_200_OK,
    ErrorCode.REFUND_EXCEEDS_PAID_AMOUNT: status.HTTP_409_CONFLICT,
    ErrorCode.REFUND_EXCEEDS_ITEM_AMOUNT: status.HTTP_409_CONFLICT,
    ErrorCode.COUPON_ALREADY_USED: status.HTTP_409_CONFLICT,
    ErrorCode.COUPON_ALREADY_LOCKED: status.HTTP_409_CONFLICT,
    ErrorCode.RETRIEVAL_UNAVAILABLE: status.HTTP_503_SERVICE_UNAVAILABLE,
    ErrorCode.INSUFFICIENT_EVIDENCE: status.HTTP_200_OK,
    ErrorCode.AGENT_ACTION_REQUIRES_APPROVAL: status.HTTP_202_ACCEPTED,
    ErrorCode.AGENT_ACTION_BLOCKED_BY_RISK: status.HTTP_403_FORBIDDEN,
    ErrorCode.PENDING_ACTION_STATE_INVALID: status.HTTP_409_CONFLICT,
    ErrorCode.MCP_TOKEN_MISSING: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.MCP_TOKEN_INVALID: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.MCP_TOKEN_INSUFFICIENT_SCOPE: status.HTTP_403_FORBIDDEN,
    ErrorCode.MCP_ORIGIN_REJECTED: status.HTTP_403_FORBIDDEN,
    # -- Phase 5: payment / fulfillment / after-sales / refund -------------
    # These families mix 404s with 409s and 422s, which is exactly why each code
    # is listed here rather than left to a family-wide guess. A family default of
    # 400 is right for a malformed payment request and wrong for "this payment
    # does not exist"; one number cannot serve both.
    ErrorCode.PAYMENT_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.PAYMENT_ALREADY_PAID: status.HTTP_409_CONFLICT,
    ErrorCode.PAYMENT_AMOUNT_MISMATCH: status.HTTP_409_CONFLICT,
    ErrorCode.PAYMENT_CALLBACK_INVALID_SIGNATURE: status.HTTP_400_BAD_REQUEST,
    ErrorCode.PAYMENT_CALLBACK_DUPLICATE: status.HTTP_200_OK,
    ErrorCode.PAYMENT_CHANNEL_UNSUPPORTED: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ErrorCode.PAYMENT_MOCK_DISABLED: status.HTTP_403_FORBIDDEN,
    ErrorCode.PAYMENT_STATE_INVALID: status.HTTP_409_CONFLICT,
    ErrorCode.FULFILLMENT_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.FULFILLMENT_QUANTITY_EXCEEDS_ORDER: status.HTTP_409_CONFLICT,
    ErrorCode.FULFILLMENT_STATE_INVALID: status.HTTP_409_CONFLICT,
    ErrorCode.FULFILLMENT_ALREADY_SHIPPED: status.HTTP_409_CONFLICT,
    ErrorCode.AFTER_SALE_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.AFTER_SALE_NOT_ELIGIBLE: status.HTTP_409_CONFLICT,
    ErrorCode.AFTER_SALE_STATE_INVALID: status.HTTP_409_CONFLICT,
    ErrorCode.REFUND_NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ErrorCode.REFUND_EXCEEDS_PAID_AMOUNT: status.HTTP_409_CONFLICT,
    ErrorCode.REFUND_EXCEEDS_ITEM_AMOUNT: status.HTTP_409_CONFLICT,
    ErrorCode.REFUND_AMOUNT_INVALID: status.HTTP_422_UNPROCESSABLE_CONTENT,
    ErrorCode.REFUND_ALREADY_COMPLETED: status.HTTP_409_CONFLICT,
    ErrorCode.OBJECT_NOT_FOUND: status.HTTP_404_NOT_FOUND,
}


def http_status_for(code: ErrorCode) -> int:
    """The canonical HTTP status for a business code.

    Only reached for a code whose error class did **not** declare an explicit
    ``http_status``; every class built with an explicit status answers from
    :attr:`AppError.status_code` first. It is still load-bearing: a code added to
    ``ErrorCode`` without a class (or a bare ``AppError`` subclass) would land here,
    and answering 400 for a *not-found* code is a client-visible contract break.

    ## The bug this function carried until Phase 5

    The previous implementation computed ``family = int(code) // 10_000`` and then
    asked whether that value was one of ``{30, 40, 50, 60, 70, 80, 90, 100}``.
    ``80_000 // 10_000`` is **8**, not 80, so that membership test was False for
    every code in the 60xxx, 70xxx, 80xxx, 90xxx and 100xxx ranges and the function
    fell through to 400. Observed before the fix:

        AFTER_SALE_NOT_FOUND   80000 -> 400   (documented: 404)
        REFUND_NOT_FOUND       80003 -> 400   (documented: 404)
        PAYMENT_NOT_FOUND      60000 -> 400   (documented: 404)
        FULFILLMENT_NOT_FOUND  70000 -> 400   (documented: 404)

    It went unnoticed because **every** error class in the codebase declares its own
    ``http_status``, so this fallback had never actually been consulted. Phase 5's
    after-sales author found it while checking the 80xxx contract, which is the
    cheap kind of finding: the code was wrong but nothing had depended on it yet.

    The two-digit family is ``int(code) // 1_000`` (with the 10xxx common range
    being ``// 10_000``, so it is handled by its own bucket rather than being forced
    into a two-digit guess). The table is explicit rather than derived from the
    numeric prefix, because a prefix rule would silently give ``PAYMENT_ALREADY_PAID``
    a 2xx/4xx status nobody chose - and this function's whole defect was a rule that
    looked plausible and was not.
    """
    if code in _DEFAULT_HTTP_STATUS:
        return _DEFAULT_HTTP_STATUS[code]

    #: Canonical status per code family (the first two digits of the business code).
    #: Values are the *category* default - an individual code that means something
    #: else declares it on its class instead.
    family_status: dict[int, int] = {
        20: status.HTTP_400_BAD_REQUEST,       # identity: malformed credential/session
        30: status.HTTP_400_BAD_REQUEST,       # catalog
        40: status.HTTP_409_CONFLICT,          # inventory: stock conflicts
        50: status.HTTP_409_CONFLICT,          # cart / pricing / order state
        60: status.HTTP_400_BAD_REQUEST,       # payment
        70: status.HTTP_409_CONFLICT,          # fulfillment state
        80: status.HTTP_409_CONFLICT,          # after-sales / refund state
        90: status.HTTP_409_CONFLICT,          # marketing conflicts
        100: status.HTTP_422_UNPROCESSABLE_CONTENT,  # knowledge input
        110: status.HTTP_409_CONFLICT,         # agent run state
        120: status.HTTP_409_CONFLICT,         # pending action / MCP state
        130: status.HTTP_503_SERVICE_UNAVAILABLE,  # storage outage
        140: status.HTTP_429_TOO_MANY_REQUESTS,    # governance / rate limits
    }
    family = int(code) // 1_000
    if family in family_status:
        return family_status[family]

    #: The 10xxx common range is the only family that is not two digits.
    if 10_000 <= int(code) < 11_000:
        return status.HTTP_400_BAD_REQUEST

    # A code with no entry anywhere is a defect in ErrorCode, not a business
    # condition, so it answers 500 and is loud rather than masquerading as a 400 a
    # client will retry. `test_every_error_code_declares_a_status` is what keeps
    # this line unreachable in practice.
    return status.HTTP_500_INTERNAL_SERVER_ERROR


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------
class ApiEnvelope(BaseModel):
    """The one and only response shape for ``/api/v1`` (§95)."""

    model_config = ConfigDict(extra="forbid")

    code: int = Field(default=int(ErrorCode.OK), description="Stable business code; 0 means success.")
    message: str = Field(default="OK", description="Human-readable, non-sensitive summary.")
    data: Any = Field(default=None, description="Payload; null when the call had no payload.")
    trace_id: str = Field(default="", description="Correlation id for support and log lookup.")


def envelope(
    data: Any = None,
    *,
    code: ErrorCode = ErrorCode.OK,
    message: str = "OK",
    trace_id: str | None = None,
) -> dict[str, Any]:
    return ApiEnvelope(
        code=int(code),
        message=message,
        data=data,
        trace_id=trace_id if trace_id is not None else get_context().trace_id,
    ).model_dump(mode="json")


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------
class AppError(Exception):
    """Base class for every deliberate, client-facing failure.

    Subclasses set :attr:`code`; call sites add human context.

    ``message`` is what the client sees and must therefore never contain
    secrets, SQL, stack frames or another user's data.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int | None = None
    #: Extra non-sensitive fields merged into ``data`` (e.g. conflicting ids).
    context: dict[str, Any]

    def __init__(
        self,
        message: str | None = None,
        *,
        context: dict[str, Any] | None = None,
        log_detail: str | None = None,
    ) -> None:
        self.code = type(self).code
        self.public_message = message or self._default_message()
        self.context = context or {}
        self.log_detail = log_detail
        super().__init__(self.public_message)

    @classmethod
    def _default_message(cls) -> str:
        return cls.code.name.replace("_", " ").title()

    @property
    def status_code(self) -> int:
        return self.http_status if self.http_status is not None else http_status_for(self.code)

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code,
            content=envelope(
                data=self.context or None,
                code=self.code,
                message=self.public_message,
            ),
            headers={TRACE_ID_HEADER: get_context().trace_id},
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={int(self.code)}, message={self.public_message!r})"


def _error(
    name: str,
    code: ErrorCode,
    *,
    http_status: int | None = None,
) -> type[AppError]:
    """Build a concrete ``AppError`` subclass bound to ``code``."""

    def _default_message(_cls: type) -> str:
        return name

    namespace: dict[str, Any] = {"code": code, "_default_message": classmethod(_default_message)}
    if http_status is not None:
        namespace["http_status"] = http_status
    return type(name, (AppError,), namespace)


# -- common -----------------------------------------------------------------
ValidationError = _error("Validation failed", ErrorCode.VALIDATION_ERROR)
NotFoundError = _error("Resource not found", ErrorCode.NOT_FOUND)
ConflictError = _error("Conflicting state", ErrorCode.CONFLICT)
InternalError = _error("Internal server error", ErrorCode.INTERNAL_ERROR)
ServiceUnavailableError = _error("Service temporarily unavailable", ErrorCode.SERVICE_UNAVAILABLE)
DependencyUnavailableError = _error("Downstream dependency unavailable", ErrorCode.DEPENDENCY_UNAVAILABLE)
#: MySQL is a *critical* dependency (§130): when it is down the API cannot serve
#: traffic, so this maps to 503 rather than degrading.
DatabaseUnavailableError = _error(
    "Database is unavailable", ErrorCode.DEPENDENCY_UNAVAILABLE, http_status=status.HTTP_503_SERVICE_UNAVAILABLE
)
RateLimitedError = _error("Too many requests", ErrorCode.RATE_LIMITED)
PayloadTooLargeError = _error("Payload too large", ErrorCode.PAYLOAD_TOO_LARGE)
UnsupportedMediaTypeError = _error("Unsupported media type", ErrorCode.UNSUPPORTED_MEDIA_TYPE)

# -- idempotency ------------------------------------------------------------
IdempotencyKeyRequiredError = _error("Idempotency-Key header is required", ErrorCode.IDEMPOTENCY_KEY_REQUIRED)
IdempotencyPayloadMismatchError = _error(
    "Idempotency-Key was reused with a different request body",
    ErrorCode.IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD,
    http_status=status.HTTP_409_CONFLICT,
)
IdempotencyInProgressError = _error(
    "An identical request is still in progress",
    ErrorCode.IDEMPOTENCY_REQUEST_IN_PROGRESS,
    http_status=status.HTTP_409_CONFLICT,
)

# -- auth -------------------------------------------------------------------
AuthenticationError = _error("Authentication required", ErrorCode.UNAUTHENTICATED, http_status=401)
InvalidCredentialsError = _error("Invalid credentials", ErrorCode.INVALID_CREDENTIALS, http_status=401)
TokenExpiredError = _error("Token has expired", ErrorCode.TOKEN_EXPIRED, http_status=401)
TokenInvalidError = _error("Token is invalid", ErrorCode.TOKEN_INVALID, http_status=401)
RefreshTokenReuseError = _error(
    "Refresh token reuse detected; the session family has been revoked",
    ErrorCode.REFRESH_TOKEN_REUSED,
    http_status=401,
)
SessionRevokedError = _error("Session has been revoked", ErrorCode.SESSION_REVOKED, http_status=401)
AccountLockedError = _error("Account is temporarily locked", ErrorCode.ACCOUNT_LOCKED, http_status=423)
AccountDisabledError = _error("Account is disabled", ErrorCode.ACCOUNT_DISABLED, http_status=403)
PermissionDeniedError = _error("Permission denied", ErrorCode.INSUFFICIENT_PERMISSION, http_status=403)
ForbiddenError = _error("Forbidden", ErrorCode.FORBIDDEN, http_status=403)
DataScopeViolationError = _error(
    "Resource is outside the caller's data scope",
    ErrorCode.DATA_SCOPE_VIOLATION,
    http_status=403,
)
CsrfValidationError = _error("CSRF validation failed", ErrorCode.CSRF_VALIDATION_FAILED, http_status=403)
MerchantMismatchError = _error("Resource belongs to another merchant", ErrorCode.MERCHANT_MISMATCH, http_status=403)

# -- catalog ----------------------------------------------------------------
ProductNotFoundError = _error("Product not found", ErrorCode.PRODUCT_NOT_FOUND, http_status=404)
ProductNotPublishedError = _error("Product is not published", ErrorCode.PRODUCT_NOT_PUBLISHED, http_status=409)
SkuNotFoundError = _error("SKU not found", ErrorCode.SKU_NOT_FOUND, http_status=404)
SkuNotAvailableError = _error("SKU is not available for sale", ErrorCode.SKU_NOT_AVAILABLE, http_status=409)
CategoryNotFoundError = _error("Category not found", ErrorCode.CATEGORY_NOT_FOUND, http_status=404)
BrandNotFoundError = _error("Brand not found", ErrorCode.BRAND_NOT_FOUND, http_status=404)

# -- inventory --------------------------------------------------------------
InsufficientStockError = _error(
    "Insufficient available stock", ErrorCode.INSUFFICIENT_STOCK, http_status=409
)
InventoryNotFoundError = _error("Inventory record not found", ErrorCode.INVENTORY_NOT_FOUND, http_status=404)
InventoryVersionConflictError = _error(
    "Inventory was modified concurrently; retry with fresh state",
    ErrorCode.INVENTORY_CONFLICT_STALE_VERSION,
    http_status=409,
)
WarehouseNotFoundError = _error("Warehouse not found", ErrorCode.WAREHOUSE_NOT_FOUND, http_status=404)

# -- order ------------------------------------------------------------------
CartNotFoundError = _error("Cart not found", ErrorCode.CART_NOT_FOUND, http_status=404)
CartEmptyError = _error("Cart is empty", ErrorCode.CART_EMPTY, http_status=409)
OrderNotFoundError = _error("Order not found", ErrorCode.ORDER_NOT_FOUND, http_status=404)
OrderStateInvalidError = _error("Order is not in a state that allows this action", ErrorCode.ORDER_STATE_INVALID, http_status=409)
OrderExpiredError = _error("Order has expired", ErrorCode.ORDER_ALREADY_EXPIRED, http_status=409)
OrderAmountMismatchError = _error(
    "Order amount does not equal the sum of its items", ErrorCode.ORDER_AMOUNT_MISMATCH, http_status=500
)
PriceChangedError = _error("Price changed since it was last previewed", ErrorCode.PRICE_CHANGED, http_status=409)
AddressNotFoundError = _error("Address not found", ErrorCode.ADDRESS_NOT_FOUND, http_status=404)
#: 50010 / 50011 are distinct from the generic 50004 because the UI branches on
#: them: "this order can no longer be cancelled" and "receipt cannot be confirmed
#: yet" are actionable states, not a generic conflict. Both carry the from/to
#: status in `context` so the client can render the real state.
OrderNotCancellableError = _error(
    "Order can no longer be cancelled", ErrorCode.ORDER_NOT_CANCELLABLE, http_status=409
)
OrderNotConfirmableError = _error(
    "Order is not in a state where receipt can be confirmed",
    ErrorCode.ORDER_NOT_CONFIRMABLE,
    http_status=409,
)

# -- payment ----------------------------------------------------------------
PaymentNotFoundError = _error("Payment not found", ErrorCode.PAYMENT_NOT_FOUND, http_status=404)
PaymentAlreadyPaidError = _error("Order is already paid", ErrorCode.PAYMENT_ALREADY_PAID, http_status=409)
PaymentAmountMismatchError = _error("Payment amount does not match the order", ErrorCode.PAYMENT_AMOUNT_MISMATCH, http_status=409)
PaymentCallbackSignatureError = _error(
    "Payment callback signature verification failed",
    ErrorCode.PAYMENT_CALLBACK_INVALID_SIGNATURE,
    http_status=400,
)
PaymentMockDisabledError = _error(
    "Mock payment is only available in dev/demo", ErrorCode.PAYMENT_MOCK_DISABLED, http_status=403
)
#: 60004 is deliberately a **2xx-family** code: a provider retries until it sees
#: success, so a duplicate delivery that was in fact already applied must be
#: answered as handled. See API_CONTRACT section 15.3.
PaymentCallbackDuplicateError = _error(
    "Payment callback was already processed",
    ErrorCode.PAYMENT_CALLBACK_DUPLICATE,
    http_status=200,
)
#: 60003 is 401 rather than 400: the signature IS the authentication model for this
#: surface (it carries no JWT), so a bad signature is an authentication failure.
#: The existing class above was built before that was written down; this comment is
#: the reason it stays at 400 until a caller needs otherwise - both are refusals and
#: no frozen contract names the status for this code.
PaymentStateInvalidError = _error(
    "Payment is not in a state that allows this action",
    ErrorCode.PAYMENT_STATE_INVALID,
    http_status=409,
)
PaymentChannelUnsupportedError = _error(
    "Payment channel is not supported by this deployment",
    ErrorCode.PAYMENT_CHANNEL_UNSUPPORTED,
    http_status=422,
)

# -- fulfillment ------------------------------------------------------------
FulfillmentNotFoundError = _error("Fulfillment not found", ErrorCode.FULFILLMENT_NOT_FOUND, http_status=404)
FulfillmentQuantityError = _error(
    "Shipment quantity exceeds the ordered quantity",
    ErrorCode.FULFILLMENT_QUANTITY_EXCEEDS_ORDER,
    http_status=409,
)
FulfillmentStateInvalidError = _error(
    "Fulfillment is not in a state that allows this action",
    ErrorCode.FULFILLMENT_STATE_INVALID,
    http_status=409,
)
#: 70003 is its own code, not the generic 70002, because "this package already
#: shipped" is a normal outcome of a double click and the UI says something
#: different for it than for "you cannot do that from here".
FulfillmentAlreadyShippedError = _error(
    "Fulfillment has already been shipped",
    ErrorCode.FULFILLMENT_ALREADY_SHIPPED,
    http_status=409,
)

# -- after-sales / refund ---------------------------------------------------
AfterSaleNotFoundError = _error("After-sale request not found", ErrorCode.AFTER_SALE_NOT_FOUND, http_status=404)
AfterSaleNotEligibleError = _error("Item is not eligible for after-sale", ErrorCode.AFTER_SALE_NOT_ELIGIBLE, http_status=409)
RefundNotFoundError = _error("Refund not found", ErrorCode.REFUND_NOT_FOUND, http_status=404)
RefundExceedsPaidError = _error(
    "Refund would exceed the amount actually paid (INV-005)",
    ErrorCode.REFUND_EXCEEDS_PAID_AMOUNT,
    http_status=409,
)
RefundExceedsItemError = _error(
    "Refund would exceed the item's paid amount",
    ErrorCode.REFUND_EXCEEDS_ITEM_AMOUNT,
    http_status=409,
)
#: 80002 is the claim's own state machine answer ("this claim was rejected and
#: cannot be approved"), distinct from 80001 (it was never eligible at all) and
#: from the refund codes, which describe money.
AfterSaleStateInvalidError = _error(
    "After-sale request is not in a state that allows this action",
    ErrorCode.AFTER_SALE_STATE_INVALID,
    http_status=409,
)
#: 80006 covers "<= 0" and "above the approved amount" together: both mean the
#: caller sent a number the workflow cannot act on, and splitting them would give
#: the client two codes it renders identically.
RefundAmountInvalidError = _error(
    "Refund amount is invalid",
    ErrorCode.REFUND_AMOUNT_INVALID,
    http_status=422,
)
#: 80007 is raised when an idempotency key is reused for a refund that would not be
#: the same refund - the money-movement analogue of 10011.
RefundAlreadyCompletedError = _error(
    "Refund has already been completed",
    ErrorCode.REFUND_ALREADY_COMPLETED,
    http_status=409,
)

# -- marketing --------------------------------------------------------------
PromotionNotFoundError = _error("Promotion not found", ErrorCode.PROMOTION_NOT_FOUND, http_status=404)
PromotionConflictError = _error("Promotion conflicts with an existing one", ErrorCode.PROMOTION_CONFLICT, http_status=409)
PromotionPreviewRequiredError = _error(
    "Promotion must be previewed before it is created", ErrorCode.PROMOTION_PREVIEW_REQUIRED, http_status=409
)
CouponNotFoundError = _error("Coupon not found", ErrorCode.COUPON_NOT_FOUND, http_status=404)
CouponNotApplicableError = _error("Coupon is not applicable to this order", ErrorCode.COUPON_NOT_APPLICABLE, http_status=409)
CouponExpiredError = _error("Coupon has expired", ErrorCode.COUPON_EXPIRED, http_status=409)
CouponAlreadyUsedError = _error("Coupon has already been used", ErrorCode.COUPON_ALREADY_USED, http_status=409)
CouponAlreadyLockedError = _error("Coupon is locked by another order", ErrorCode.COUPON_ALREADY_LOCKED, http_status=409)

# -- knowledge / RAG --------------------------------------------------------
DocumentNotFoundError = _error("Document not found", ErrorCode.DOCUMENT_NOT_FOUND, http_status=404)
DocumentStateInvalidError = _error("Document is not in a valid state", ErrorCode.DOCUMENT_STATE_INVALID, http_status=409)
DocumentUnsupportedTypeError = _error("Unsupported document type", ErrorCode.DOCUMENT_UNSUPPORTED_TYPE, http_status=415)
DocumentTooLargeError = _error("Document exceeds the size limit", ErrorCode.DOCUMENT_TOO_LARGE, http_status=413)
UploadSignatureMismatchError = _error(
    "File content does not match its declared type", ErrorCode.UPLOAD_SIGNATURE_MISMATCH, http_status=415
)
PathTraversalError = _error("Unsafe path detected", ErrorCode.PATH_TRAVERSAL_DETECTED, http_status=400)
RetrievalUnavailableError = _error(
    "Knowledge retrieval is currently unavailable; refusing to answer from memory",
    ErrorCode.RETRIEVAL_UNAVAILABLE,
    http_status=503,
)
InsufficientEvidenceError = _error("Not enough evidence to answer", ErrorCode.INSUFFICIENT_EVIDENCE)
EmbeddingDimensionMismatchError = _error(
    "Embedding dimension does not match the vector collection",
    ErrorCode.EMBEDDING_DIMENSION_MISMATCH,
    http_status=500,
)

# -- agent ------------------------------------------------------------------
AgentRunNotFoundError = _error("Agent run not found", ErrorCode.AGENT_RUN_NOT_FOUND, http_status=404)
AgentBudgetExceededError = _error("Agent exceeded its execution budget", ErrorCode.AGENT_BUDGET_EXCEEDED, http_status=409)
AgentToolNotFoundError = _error("Unknown tool", ErrorCode.AGENT_TOOL_NOT_FOUND, http_status=404)
AgentToolDisabledError = _error("Tool is disabled", ErrorCode.AGENT_TOOL_DISABLED, http_status=403)
AgentToolNotAllowedError = _error("Tool is not allowed for this agent", ErrorCode.AGENT_TOOL_NOT_ALLOWED_FOR_AGENT, http_status=403)
AgentToolInputError = _error("Tool input failed validation", ErrorCode.AGENT_TOOL_INPUT_INVALID, http_status=422)
AgentToolOutputError = _error("Tool output failed validation", ErrorCode.AGENT_TOOL_OUTPUT_INVALID, http_status=500)
AgentApprovalRequiredError = _error("This action requires human approval", ErrorCode.AGENT_ACTION_REQUIRES_APPROVAL, http_status=202)
AgentBlockedByRiskError = _error(
    "Action blocked by the deterministic risk engine",
    ErrorCode.AGENT_ACTION_BLOCKED_BY_RISK,
    http_status=403,
)
AgentRevalidationFailedError = _error(
    "Business facts changed between proposal and approval; the action was not executed",
    ErrorCode.AGENT_REVALIDATION_FAILED,
    http_status=409,
)
ExecutionReceiptMissingError = _error(
    "A success claim requires an ExecutionReceipt", ErrorCode.AGENT_EXECUTION_RECEIPT_MISSING, http_status=500
)
PromptInjectionDetectedError = _error(
    "Request was rejected by the input guard", ErrorCode.AGENT_PROMPT_INJECTION_DETECTED, http_status=400
)

# -- pending action / MCP ---------------------------------------------------
PendingActionNotFoundError = _error("Pending action not found", ErrorCode.PENDING_ACTION_NOT_FOUND, http_status=404)
PendingActionStateInvalidError = _error("Pending action is not awaiting a decision", ErrorCode.PENDING_ACTION_STATE_INVALID, http_status=409)
PendingActionExpiredError = _error("Pending action has expired", ErrorCode.PENDING_ACTION_EXPIRED, http_status=410)
PendingActionPayloadChangedError = _error(
    "The underlying business state changed; re-preview before approving",
    ErrorCode.PENDING_ACTION_PAYLOAD_CHANGED,
    http_status=409,
)
McpTokenMissingError = _error("Bearer token is required", ErrorCode.MCP_TOKEN_MISSING, http_status=401)
McpTokenInvalidError = _error("Bearer token is invalid", ErrorCode.MCP_TOKEN_INVALID, http_status=401)
McpTokenWrongIssuerError = _error("Token issuer is not trusted", ErrorCode.MCP_TOKEN_WRONG_ISSUER, http_status=401)
McpTokenWrongAudienceError = _error("Token audience does not include this resource", ErrorCode.MCP_TOKEN_WRONG_AUDIENCE, http_status=401)
McpInsufficientScopeError = _error("Token lacks the required scope", ErrorCode.MCP_TOKEN_INSUFFICIENT_SCOPE, http_status=403)
McpOriginRejectedError = _error("Request origin is not allowed", ErrorCode.MCP_ORIGIN_REJECTED, http_status=403)
McpToolNotExposedError = _error("Tool is not exposed over MCP", ErrorCode.MCP_TOOL_NOT_EXPOSED, http_status=404)
McpWriteNotPermittedError = _error(
    "MCP does not expose write operations (spec §92)", ErrorCode.MCP_WRITE_NOT_PERMITTED, http_status=403
)

# -- storage ----------------------------------------------------------------
ObjectNotFoundError = _error("Stored object not found", ErrorCode.OBJECT_NOT_FOUND, http_status=404)
ObjectStorageUnavailableError = _error("Object storage is unavailable", ErrorCode.OBJECT_STORAGE_UNAVAILABLE, http_status=503)
ObjectChecksumMismatchError = _error("Stored object failed checksum verification", ErrorCode.OBJECT_CHECKSUM_MISMATCH, http_status=409)
ObjectTooLargeError = _error("Uploaded object exceeds the size limit", ErrorCode.OBJECT_TOO_LARGE, http_status=413)

# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers so *every* failure leaves as the §95 envelope."""
    import logging

    logger = logging.getLogger("app.errors")

    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        ctx = get_context()
        logger.warning(
            "application error",
            extra={"code": int(exc.code), "detail": exc.log_detail or exc.public_message},
        )
        response = exc.to_response()
        response.headers[TRACE_ID_HEADER] = ctx.trace_id
        return response

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field names and error types are returned; raw input values are not,
        # so an over-posted password never lands in a response body (§110).
        details = [
            {
                "location": ".".join(str(part) for part in err.get("loc", ())),
                "type": err.get("type", "value_error"),
                "message": err.get("msg", "invalid value"),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=envelope(
                data={"errors": details},
                code=ErrorCode.VALIDATION_ERROR,
                message="Request validation failed",
            ),
            headers={TRACE_ID_HEADER: get_context().trace_id},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapping = {
            status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHENTICATED,
            status.HTTP_403_FORBIDDEN: ErrorCode.FORBIDDEN,
            status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
            status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
            status.HTTP_413_CONTENT_TOO_LARGE: ErrorCode.PAYLOAD_TOO_LARGE,
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
            status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
            status.HTTP_503_SERVICE_UNAVAILABLE: ErrorCode.SERVICE_UNAVAILABLE,
        }
        code = mapping.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        message = exc.detail if isinstance(exc.detail, str) else "Request failed"
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope(code=code, message=message),
            headers={TRACE_ID_HEADER: get_context().trace_id},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        # The real error is logged with a stack trace; the client only receives
        # a correlation id, so internals are never disclosed (§109).
        logger.exception("unhandled exception", exc_info=exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=envelope(
                code=ErrorCode.INTERNAL_ERROR,
                message="Internal server error. Quote the trace id when reporting this.",
            ),
            headers={TRACE_ID_HEADER: get_context().trace_id},
        )
