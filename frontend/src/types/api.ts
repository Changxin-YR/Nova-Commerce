/**
 * Transport contract for `/api/v1` (§95, §106).
 *
 * Every response — success or failure — is `{ code, message, data, trace_id }`.
 * The HTTP status is a transport concern; `code` is the semantic one. Clients
 * branch on `code`, never on the HTTP status alone.
 *
 * The `ErrorCode` values below are a hand-written mirror of
 * `backend/app/core/errors.py::ErrorCode`. `src/types/__tests__/domain-mirror.spec.ts`
 * parses the Python file and FAILS if the two drift, so this comment cannot rot.
 */

export interface ApiEnvelope<T> {
  /** 0 means success; anything else is a stable business error code. */
  code: number
  /** Human-readable, non-sensitive. Safe to show to end users. */
  message: string
  /** Payload; `null` when the call had no payload. */
  data: T | null
  /** Correlation id; echoed in the `X-Trace-Id` response header. */
  trace_id: string
}

/** Stable business error codes, mirrored from the backend `ErrorCode` IntEnum. */
export const ErrorCode = {
  OK: 0,

  // -- 10xxx common ------------------------------------------------------
  INTERNAL_ERROR: 10_000,
  VALIDATION_ERROR: 10_001,
  NOT_FOUND: 10_002,
  CONFLICT: 10_003,
  RATE_LIMITED: 10_004,
  METHOD_NOT_ALLOWED: 10_005,
  PAYLOAD_TOO_LARGE: 10_006,
  UNSUPPORTED_MEDIA_TYPE: 10_007,
  SERVICE_UNAVAILABLE: 10_008,
  DEPENDENCY_UNAVAILABLE: 10_009,
  IDEMPOTENCY_KEY_REQUIRED: 10_010,
  IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD: 10_011,
  IDEMPOTENCY_REQUEST_IN_PROGRESS: 10_012,

  // -- 20xxx identity / auth --------------------------------------------
  UNAUTHENTICATED: 20_000,
  INVALID_CREDENTIALS: 20_001,
  TOKEN_EXPIRED: 20_002,
  TOKEN_INVALID: 20_003,
  REFRESH_TOKEN_REUSED: 20_004,
  SESSION_REVOKED: 20_005,
  ACCOUNT_LOCKED: 20_006,
  ACCOUNT_DISABLED: 20_007,
  FORBIDDEN: 20_008,
  INSUFFICIENT_PERMISSION: 20_009,
  DATA_SCOPE_VIOLATION: 20_010,
  CSRF_VALIDATION_FAILED: 20_011,
  MERCHANT_MISMATCH: 20_012,

  // -- 30xxx catalog -----------------------------------------------------
  PRODUCT_NOT_FOUND: 30_000,
  PRODUCT_NOT_PUBLISHED: 30_001,
  SKU_NOT_FOUND: 30_002,
  SKU_NOT_AVAILABLE: 30_003,
  CATEGORY_NOT_FOUND: 30_004,
  BRAND_NOT_FOUND: 30_005,
  PRODUCT_ALREADY_PUBLISHED: 30_006,
  PRODUCT_STATE_INVALID: 30_007,
  IMAGE_UPLOAD_REJECTED: 30_008,

  // -- 40xxx inventory ---------------------------------------------------
  INSUFFICIENT_STOCK: 40_000,
  INVENTORY_NOT_FOUND: 40_001,
  INVENTORY_CONFLICT_STALE_VERSION: 40_002,
  INVENTORY_ADJUSTMENT_INVALID: 40_003,
  WAREHOUSE_NOT_FOUND: 40_004,
  INVENTORY_MOVEMENT_DUPLICATE: 40_005,

  // -- 50xxx cart / pricing / order --------------------------------------
  CART_NOT_FOUND: 50_000,
  CART_EMPTY: 50_001,
  CART_ITEM_NOT_FOUND: 50_002,
  ORDER_NOT_FOUND: 50_003,
  ORDER_STATE_INVALID: 50_004,
  ORDER_ALREADY_EXPIRED: 50_005,
  ORDER_AMOUNT_MISMATCH: 50_006,
  PRICE_CHANGED: 50_007,
  ADDRESS_NOT_FOUND: 50_008,
  ADDRESS_NOT_OWNED: 50_009,
  ORDER_NOT_CANCELLABLE: 50_010,
  ORDER_NOT_CONFIRMABLE: 50_011,

  // -- 60xxx payment -----------------------------------------------------
  PAYMENT_NOT_FOUND: 60_000,
  PAYMENT_ALREADY_PAID: 60_001,
  PAYMENT_AMOUNT_MISMATCH: 60_002,
  PAYMENT_CALLBACK_INVALID_SIGNATURE: 60_003,
  PAYMENT_CALLBACK_DUPLICATE: 60_004,
  PAYMENT_CHANNEL_UNSUPPORTED: 60_005,
  PAYMENT_MOCK_DISABLED: 60_006,
  PAYMENT_STATE_INVALID: 60_007,

  // -- 70xxx fulfillment -------------------------------------------------
  FULFILLMENT_NOT_FOUND: 70_000,
  FULFILLMENT_QUANTITY_EXCEEDS_ORDER: 70_001,
  FULFILLMENT_STATE_INVALID: 70_002,
  FULFILLMENT_ALREADY_SHIPPED: 70_003,

  // -- 80xxx after-sales / refund ----------------------------------------
  AFTER_SALE_NOT_FOUND: 80_000,
  AFTER_SALE_NOT_ELIGIBLE: 80_001,
  AFTER_SALE_STATE_INVALID: 80_002,
  REFUND_NOT_FOUND: 80_003,
  REFUND_EXCEEDS_PAID_AMOUNT: 80_004,
  REFUND_EXCEEDS_ITEM_AMOUNT: 80_005,
  REFUND_AMOUNT_INVALID: 80_006,
  REFUND_ALREADY_COMPLETED: 80_007,

  // -- 90xxx marketing ---------------------------------------------------
  PROMOTION_NOT_FOUND: 90_000,
  PROMOTION_CONFLICT: 90_001,
  PROMOTION_RULE_INVALID: 90_002,
  PROMOTION_PREVIEW_REQUIRED: 90_003,
  COUPON_NOT_FOUND: 90_004,
  COUPON_NOT_APPLICABLE: 90_005,
  COUPON_EXPIRED: 90_006,
  COUPON_ALREADY_USED: 90_007,
  COUPON_ALREADY_LOCKED: 90_008,
  COUPON_THRESHOLD_NOT_MET: 90_009,

  // -- 100xxx knowledge / RAG --------------------------------------------
  KNOWLEDGE_BASE_NOT_FOUND: 100_000,
  DOCUMENT_NOT_FOUND: 100_001,
  DOCUMENT_STATE_INVALID: 100_002,
  DOCUMENT_DUPLICATE: 100_003,
  DOCUMENT_PARSE_FAILED: 100_004,
  DOCUMENT_UNSUPPORTED_TYPE: 100_005,
  DOCUMENT_TOO_LARGE: 100_006,
  UPLOAD_SIGNATURE_MISMATCH: 100_007,
  PATH_TRAVERSAL_DETECTED: 100_008,
  RETRIEVAL_UNAVAILABLE: 100_009,
  INSUFFICIENT_EVIDENCE: 100_010,
  EMBEDDING_DIMENSION_MISMATCH: 100_011,

  // -- 110xxx agent ------------------------------------------------------
  AGENT_RUN_NOT_FOUND: 110_000,
  AGENT_BUDGET_EXCEEDED: 110_001,
  AGENT_TOOL_NOT_FOUND: 110_002,
  AGENT_TOOL_DISABLED: 110_003,
  AGENT_TOOL_NOT_ALLOWED_FOR_AGENT: 110_004,
  AGENT_TOOL_INPUT_INVALID: 110_005,
  AGENT_TOOL_OUTPUT_INVALID: 110_006,
  AGENT_ACTION_REQUIRES_APPROVAL: 110_007,
  AGENT_ACTION_BLOCKED_BY_RISK: 110_008,
  AGENT_GRAPH_VERSION_MISMATCH: 110_009,
  AGENT_REVALIDATION_FAILED: 110_010,
  AGENT_EXECUTION_RECEIPT_MISSING: 110_011,
  AGENT_PROMPT_INJECTION_DETECTED: 110_012,

  // -- 120xxx pending action / MCP ---------------------------------------
  PENDING_ACTION_NOT_FOUND: 120_000,
  PENDING_ACTION_STATE_INVALID: 120_001,
  PENDING_ACTION_EXPIRED: 120_002,
  PENDING_ACTION_ALREADY_DECIDED: 120_003,
  PENDING_ACTION_PAYLOAD_CHANGED: 120_004,
  MCP_TOKEN_MISSING: 120_005,
  MCP_TOKEN_INVALID: 120_006,
  MCP_TOKEN_WRONG_ISSUER: 120_007,
  MCP_TOKEN_WRONG_AUDIENCE: 120_008,
  MCP_TOKEN_INSUFFICIENT_SCOPE: 120_009,
  MCP_ORIGIN_REJECTED: 120_010,
  MCP_TOOL_NOT_EXPOSED: 120_011,
  MCP_WRITE_NOT_PERMITTED: 120_012,

  // -- 130xxx storage ----------------------------------------------------
  OBJECT_NOT_FOUND: 130_000,
  OBJECT_STORAGE_UNAVAILABLE: 130_001,
  OBJECT_CHECKSUM_MISMATCH: 130_002,
  OBJECT_TOO_LARGE: 130_003,
  BUCKET_UNAVAILABLE: 130_004,

  // -- 140xxx governance -------------------------------------------------
  AUDIT_WRITE_FAILED: 140_000,
  RATE_LIMIT_EXCEEDED: 140_001,
  FEATURE_DISABLED: 140_002,
} as const

export type ErrorCodeValue = (typeof ErrorCode)[keyof typeof ErrorCode]

/** Normalized failure surfaced to the UI by `src/api/error.ts`. */
export interface NormalizedApiError {
  /** Business code, or a synthetic transport code (see TRANSPORT_CODES). */
  code: number
  /** Safe to display. Already localized/neutralized by the error mapper. */
  message: string
  /** Correlation id from the response envelope or the `X-Trace-Id` header. */
  traceId: string
  /** HTTP status, or 0 when the request never reached the server. */
  httpStatus: number
  /** True when the caller may retry (network blip, 5xx, 429). */
  retryable: boolean
  /** True when the failure was "you are not allowed" (§104 permission UI). */
  forbidden: boolean
  /** True when the session is gone and the user must log in again. */
  unauthenticated: boolean
  /** Raw code for logging; never rendered. */
  raw?: unknown
}

/** Synthetic codes for failures that never produced an envelope. */
export const TRANSPORT_CODES = {
  NETWORK_ERROR: -1,
  TIMEOUT: -2,
  CANCELLED: -3,
  MALFORMED_RESPONSE: -4,
  UNKNOWN: -5,
  /** Refresh was attempted and definitively failed. */
  SESSION_EXPIRED: -6,
} as const

export interface PageQuery {
  page?: number
  page_size?: number
  keyword?: string
  sort?: string
}

export interface PageMeta {
  page: number
  page_size: number
  total: number
  total_pages: number
}

/** Paged payload as returned inside `envelope.data`. */
export interface Paged<T> {
  items: T[]
  meta: PageMeta
}

/** Idempotent-write helper type: both fields are required by §96 order create. */
export interface IdempotentWrite {
  idempotencyKey: string
  clientRequestId: string
}
