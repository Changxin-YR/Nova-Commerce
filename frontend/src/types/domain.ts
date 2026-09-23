/**
 * Frozen domain model — hand-written mirror of the backend contract (§105/§107).
 *
 * RULES
 *  1. Every enum below MUST match the backend exactly. `src/types/__tests__/domain-mirror.spec.ts`
 *     parses `backend/app/core/errors.py` for error codes; the status enums are
 *     frozen by the spec (§105) and are asserted in that same spec file.
 *  2. MONEY IS ALWAYS AN INTEGER IN MINOR UNITS (cents). Field names therefore end
 *     in `_amount` / `amount` and are typed `number`. Never format, round or store
 *     money as a float, and never divide by 100 except at the render boundary
 *     (`src/utils/money.ts`).
 *  3. Types are `const` objects + union types rather than `enum`, so they erase
 *     cleanly under Vite and remain usable in `.vue` templates.
 *
 * SEAM FOR GENERATED TYPES (§141, REQ-API-005)
 *  `src/types/generated/api.d.ts` is produced by `npm run gen:api` from the
 *  backend OpenAPI document. Request/response DTOs in `src/types/api-contract.ts`
 *  are the hand-written bridge today; when the generator is wired into the build
 *  they become `components['schemas'][...]` aliases. Nothing else has to change
 *  because components only ever import from `@/types`.
 */

// ---------------------------------------------------------------------------
// Enums (frozen)
// ---------------------------------------------------------------------------

export const ORDER_STATUSES = [
  'PENDING_PAYMENT',
  'PROCESSING',
  'COMPLETED',
  'CANCELLED',
  'CLOSED',
] as const
export type OrderStatus = (typeof ORDER_STATUSES)[number]

export const PAYMENT_STATUSES = [
  'UNPAID',
  'PAYING',
  'PAID',
  'PARTIAL_REFUNDED',
  'REFUNDED',
] as const
export type PaymentStatus = (typeof PAYMENT_STATUSES)[number]

export const FULFILLMENT_STATUSES = [
  'UNFULFILLED',
  'PARTIAL_SHIPPED',
  'SHIPPED',
  'DELIVERED',
] as const
export type FulfillmentStatus = (typeof FULFILLMENT_STATUSES)[number]

export const AFTER_SALE_STATUSES = [
  'NONE',
  'PROCESSING',
  'PARTIAL_REFUNDED',
  'REFUNDED',
] as const
export type AfterSaleStatus = (typeof AFTER_SALE_STATUSES)[number]

export const COUPON_STATUSES = ['UNUSED', 'LOCKED', 'USED', 'EXPIRED'] as const
export type CouponStatus = (typeof COUPON_STATUSES)[number]

export const KNOWLEDGE_DOC_STATUSES = [
  'UPLOADED',
  'PROCESSING',
  'READY',
  'FAILED',
  'ARCHIVED',
] as const
export type KnowledgeDocStatus = (typeof KNOWLEDGE_DOC_STATUSES)[number]

export const PENDING_ACTION_STATUSES = [
  'PENDING',
  'APPROVED',
  'REJECTED',
  'EXECUTING',
  'SUCCEEDED',
  'FAILED',
  'EXPIRED',
] as const
export type PendingActionStatus = (typeof PENDING_ACTION_STATUSES)[number]

/**
 * Deterministic risk levels from the backend risk engine.
 * NOTE: the spec lists `READ` as the first level (read-only operations); it is the
 * lowest level, not a typo for "RED".
 */
export const RISK_LEVELS = ['READ', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'] as const
export type RiskLevel = (typeof RISK_LEVELS)[number]

/** Approval is required for anything at or above this level (§101 HITL). */
export const RISK_LEVELS_REQUIRING_APPROVAL: readonly RiskLevel[] = ['HIGH', 'CRITICAL']

export const PRODUCT_STATUSES = ['DRAFT', 'PUBLISHED', 'UNPUBLISHED', 'ARCHIVED'] as const
export type ProductStatus = (typeof PRODUCT_STATUSES)[number]

export const AFTER_SALE_TYPES = ['REFUND_ONLY', 'RETURN_REFUND'] as const
export type AfterSaleType = (typeof AFTER_SALE_TYPES)[number]

export const AGENT_RUN_STATUSES = [
  'RUNNING',
  'WAITING_APPROVAL',
  'SUCCEEDED',
  'FAILED',
  'CANCELLED',
] as const
export type AgentRunStatus = (typeof AGENT_RUN_STATUSES)[number]

export const MESSAGE_ROLES = ['user', 'assistant', 'system', 'tool'] as const
export type MessageRole = (typeof MESSAGE_ROLES)[number]

// ---------------------------------------------------------------------------
// Money
// ---------------------------------------------------------------------------

/**
 * Integer minor units (cents). `type` cannot enforce "integer", so the API
 * boundary asserts it in dev builds (`assertMoney` in src/utils/money.ts).
 */
export type MoneyAmount = number

// ---------------------------------------------------------------------------
// Entities
// ---------------------------------------------------------------------------

export interface Money {
  /** ISO-4217, always 'CNY' for this deployment. */
  currency: string
  /** Integer minor units. */
  amount: MoneyAmount
}

export interface ImageRef {
  id: string
  url: string
  alt?: string
  sort_order?: number
}

export interface Category {
  id: string
  name: string
  parent_id: string | null
  level: number
  sort_order?: number
}

export interface Brand {
  id: string
  name: string
  logo_url?: string
}

export interface Sku {
  id: string
  product_id: string
  sku_code: string
  /** e.g. {"color":"深空黑","storage":"256GB"} */
  specs: Record<string, string>
  /** Integer minor units. */
  price_amount: MoneyAmount
  /** Integer minor units; may be below price on promotion. */
  original_price_amount?: MoneyAmount
  /** Display-only projection; the server is authoritative for availability. */
  available_stock?: number
  status: ProductStatus
}

export interface Product {
  id: string
  title: string
  subtitle?: string
  brand?: Brand
  category?: Category
  description?: string
  images: ImageRef[]
  skus: Sku[]
  /** Integer minor units; min across SKUs. */
  min_price_amount: MoneyAmount
  max_price_amount: MoneyAmount
  /** Integer minor units. */
  original_price_amount?: MoneyAmount
  status: ProductStatus
  sales_count?: number
  rating?: number
  tags?: string[]
  created_at: string
  updated_at?: string
}

export interface ProductSummary {
  id: string
  title: string
  cover_url?: string
  /** Integer minor units. */
  min_price_amount: MoneyAmount
  /** Integer minor units. */
  original_price_amount?: MoneyAmount
  sales_count?: number
  rating?: number
  brand_name?: string
  tags?: string[]
  /** Console lists need this; the server is still authoritative for legality. */
  status: ProductStatus
}

export interface CartItem {
  id: string
  product_id: string
  sku_id: string
  product_title: string
  sku_specs: Record<string, string>
  cover_url?: string
  quantity: number
  /** Integer minor units, price captured at the time of adding. */
  unit_price_amount: MoneyAmount
  /** Integer minor units. */
  subtotal_amount: MoneyAmount
  selected: boolean
  /** Server-computed; the UI must not assume stock is available. */
  available: boolean
  unavailable_reason?: string
}

export interface Cart {
  id: string
  items: CartItem[]
  /** Integer minor units, selected items only. */
  selected_amount: MoneyAmount
  item_count: number
}

export interface Address {
  id: string
  receiver_name: string
  receiver_phone: string
  province: string
  city: string
  district: string
  detail: string
  postal_code?: string
  is_default: boolean
  tag?: string
}

/*
 * REMOVED — the invented order shapes.
 *
 * `OrderItem`, `OrderSnapshot`, `Shipment` and `Order` used to live here as a nested,
 * string-id model (`order.snapshot.items`, `order.status`, `Shipment.id: string`) written
 * before `docs/architecture/API_CONTRACT.md` existed. The contract landed and showed those
 * shapes were WRONG, not merely unconfirmed: money is FLAT on the order, the state field is
 * `order_status`, identifiers are numbers, and the receiver arrives already masked (§94).
 *
 * They are deleted rather than kept alongside the frozen types, because two shapes for one
 * resource is precisely how a silent integration bug starts: half the app keeps compiling
 * against the old one and the mismatch only surfaces when the real API answers.
 *
 * The frozen shapes are re-exported at the bottom of this file, so
 * `import type { Order } from '@/types/domain'` still resolves to exactly ONE definition.
 */

export interface Payment {
  id: string
  order_no: string
  channel: 'MOCK' | 'ALIPAY' | 'WECHAT'
  status: PaymentStatus
  /** Integer minor units. */
  amount: MoneyAmount
  /** Integer minor units; server-verified. */
  paid_amount: MoneyAmount
  /** Mock channel only: the URL the browser is sent to (§100 MockPay). */
  pay_url?: string
  transaction_no?: string
  idempotency_key?: string
  expires_at?: string
  paid_at?: string
  created_at: string
}

export interface RefundRecord {
  id: string
  after_sale_no: string
  order_no: string
  /** Integer minor units. */
  amount: MoneyAmount
  status: 'PENDING' | 'SUCCEEDED' | 'FAILED'
  reason?: string
  operator?: string
  created_at: string
  completed_at?: string
}

export interface AfterSale {
  id: string
  after_sale_no: string
  order_no: string
  type: AfterSaleType
  status: AfterSaleStatus
  /** Integer minor units requested by the buyer. */
  requested_amount: MoneyAmount
  /** Integer minor units approved by the merchant. */
  approved_amount: MoneyAmount
  /** Integer minor units actually refunded. */
  refunded_amount: MoneyAmount
  reason: string
  description?: string
  evidence_urls?: string[]
  items: { order_item_id: string; quantity: number }[]
  refunds: RefundRecord[]
  reject_reason?: string
  created_at: string
  processed_at?: string
}

export interface Coupon {
  id: string
  code: string
  name: string
  status: CouponStatus
  /** Integer minor units off. */
  discount_amount: MoneyAmount
  /** Integer minor units minimum spend. */
  threshold_amount: MoneyAmount
  valid_from: string
  valid_to: string
  locked_order_no?: string
}

export interface KnowledgeBase {
  id: string
  name: string
  description?: string
  document_count: number
  embedding_model: string
  created_at: string
}

export interface KnowledgeDoc {
  id: string
  knowledge_base_id: string
  file_name: string
  content_type: string
  size_bytes: number
  status: KnowledgeDocStatus
  chunk_count: number
  error_message?: string
  uploaded_by: string
  created_at: string
  processed_at?: string
}

/** One stage of the retrieval pipeline shown by the retrieval-debug view (§103). */
export interface RetrievalStage {
  stage: 'rewrite' | 'filter' | 'dense' | 'sparse' | 'fusion' | 'rerank' | 'final_evidence'
  duration_ms: number
  candidates: RetrievalHit[]
}

export interface RetrievalHit {
  chunk_id: string
  doc_id: string
  doc_name: string
  text: string
  score: number
  /** Per-stage scores; dense/sparse/fusion/rerank are reported separately. */
  scores?: Record<string, number>
  metadata?: Record<string, string | number | boolean>
}

export interface Citation {
  index: number
  doc_id: string
  doc_name: string
  chunk_id?: string
  score: number
  snippet: string
}

/** A tool invocation surfaced to the user as a ToolProgress block (§101). */
export interface ToolProgress {
  tool_call_id: string
  tool_name: string
  status: 'running' | 'succeeded' | 'failed' | 'skipped'
  /** Safe, non-sensitive summary. No hidden chain-of-thought is ever emitted. */
  summary?: string
  started_at: string
  finished_at?: string
  duration_ms?: number
}

export interface AgentRun {
  id: string
  thread_id: string
  user_id: string
  /** e.g. 'assistant' | 'operations' | 'analytics'. */
  agent_name: string
  status: AgentRunStatus
  query: string
  final_answer?: string
  citations?: Citation[]
  tool_calls?: ToolProgress[]
  pending_action_id?: string
  /** LLM/tool spend accounting; the backend enforces the budget. */
  tokens_used?: number
  cost_amount?: MoneyAmount
  error_code?: number
  error_message?: string
  started_at: string
  finished_at?: string
  graph_version?: string
}

export interface AgentThread {
  id: string
  title: string
  user_id: string
  agent_name: string
  last_message_at: string
  message_count: number
}

/**
 * A write the agent wants to perform. It is ALWAYS rendered as an approval card
 * first: the backend recomputes the diff and rejects stale payloads (§101).
 */
export interface PendingAction {
  id: string
  agent_run_id: string
  action_type: string
  tool_name: string
  summary: string
  risk_level: RiskLevel
  status: PendingActionStatus
  /** Preview payload; display-only. Execution uses the server-side stored version. */
  payload: Record<string, unknown>
  payload_hash: string
  /** Human-readable before/after diff for the approval card. */
  diff?: { field: string; before: unknown; after: unknown }[]
  requested_by: string
  decided_by?: string
  decision_reason?: string
  expires_at: string
  created_at: string
  decided_at?: string
  executed_at?: string
  execution_receipt?: Record<string, unknown>
}

// ---------------------------------------------------------------------------
// Re-exports
// ---------------------------------------------------------------------------

/**
 * `Paged` and the pagination shapes live in `@/types/api` (they are transport
 * concerns), but API modules import them alongside domain entities. Re-exporting
 * here keeps that module's import statements to one source and avoids a second
 * near-identical definition drifting out of sync.
 */
export type { Paged, PageQuery, PageMeta } from '@/types/api'

/**
 * The frozen order / fulfillment / inventory shapes, re-exported so that every
 * `import type { Order } from '@/types/domain'` resolves to the SINGLE definition in
 * `@/types/frozen-contract` (a verbatim transcription of `API_CONTRACT.md`).
 *
 * `Order` is deliberately an alias of the DETAIL payload: the frozen contract splits
 * `OrderSummary` (list rows, no `items[]`/`shipments[]`) from `OrderDetail` (single order).
 * List endpoints return `Paged<OrderSummary>`; a view that needs line items holds an
 * `OrderDetail`. Aliasing `Order` to the detail payload keeps one name for "a whole order"
 * while the narrowed `OrderSummary` stays available for tables that only need the envelope.
 */
export type {
  AdjustmentPreview,
  AnalyticsDimension,
  AnalyticsEnvelope,
  AnalyticsPeriod,
  AnalyticsPoint,
  AnalyticsSummary,
  AnalyticsUnit,
  CreateAdjustmentRequest,
  Fulfillment,
  FulfillmentItem,
  Inventory,
  OrderDetail,
  OrderItem,
  OrderSummary,
  ShipFulfillmentRequest,
  StaleVersionConflict,
} from '@/types/frozen-contract'

/**
 * The §13 shapes (promotion, coupon template, role, user).
 *
 * Re-exported for the same reason as the order/inventory shapes: `import type { Promotion } from
 * '@/types/domain'` should reach ONE definition. NOTE that `src/api/marketing.ts` still carries a
 * LOCAL `Promotion` that §13 superseded — migrating onto these is a view-level job, tracked in the
 * handoff doc, and this re-export is what makes it a rename rather than a rewrite.
 */
export type {
  CouponPreview,
  CouponScope,
  CouponTemplate,
  CouponTemplateStatus,
  CouponType,
  CouponValidityType,
  DataScope,
  Promotion,
  PromotionConflict,
  PromotionImpactEstimate,
  PromotionPreview,
  PromotionRuleConfig,
  PromotionScope,
  PromotionStatus,
  PromotionType,
  Role,
  RolePermission,
  UpdateRolePermissionsRequest,
  User,
  UserRoleRef,
  UserStatus,
  UserType,
} from '@/types/frozen-contract'

/** A single order with its line items and shipments (the detail payload). */
export type { OrderDetail as Order } from '@/types/frozen-contract'

// ---------------------------------------------------------------------------
// UI-level status (§108)
// ---------------------------------------------------------------------------

/** Generic page/panel status. Every core page renders all five (§108). */
export type ViewStatus = 'loading' | 'success' | 'empty' | 'error' | 'permission_denied'

/** Additional statuses agent surfaces must render (§108). */
export type AgentViewStatus = 'streaming' | 'waiting_approval' | 'failed' | 'cancelled'

/** Additional statuses knowledge surfaces must render (§108). */
export type KnowledgeViewStatus = 'processing' | 'processing_failed'
