/**
 * Request/response DTOs for the API modules (§107).
 *
 * These are the hand-written bridge to the backend OpenAPI document. When
 * `npm run gen:api` is wired into the build, the aliases below are replaced by
 * `components['schemas'][...]` lookups from `@/types/generated/api` and nothing
 * else in the app has to change.
 */

import type {
  Address,
  Coupon,
  Citation,
  RetrievalStage,
  ToolProgress,
} from '@/types/domain'
import type { PageQuery } from '@/types/api'

// ---------------------------------------------------------------------------
// identity
// ---------------------------------------------------------------------------

export interface LoginRequest {
  username: string
  password: string
}

export interface LoginResponse {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
  user: CurrentUser
}

export interface CurrentUser {
  id: string
  username: string
  display_name: string
  email?: string
  phone?: string
  avatar_url?: string
  /** Server-owned. The client only mirrors these for UX (§104). */
  roles: string[]
  merchant_id?: string
}

export interface PermissionResponse {
  roles: string[]
  /** Flat permission codes, e.g. ['product:write', 'order:read']. */
  permissions: string[]
}

export interface AddressPayload {
  receiver_name: string
  receiver_phone: string
  province: string
  city: string
  district: string
  detail: string
  postal_code?: string
  is_default?: boolean
  tag?: string
}

// ---------------------------------------------------------------------------
// catalog
// ---------------------------------------------------------------------------

export interface ProductQuery extends PageQuery {
  category_id?: string
  brand_id?: string
  min_price_amount?: number
  max_price_amount?: number
  status?: string
}

export interface ProductPayload {
  title: string
  subtitle?: string
  category_id: string
  brand_id?: string
  description?: string
  images: { url: string; alt?: string; sort_order?: number }[]
  skus: {
    sku_code: string
    specs: Record<string, string>
    price_amount: number
    original_price_amount?: number
    stock?: number
  }[]
}

export interface SkuPayload {
  sku_code: string
  specs: Record<string, string>
  price_amount: number
  original_price_amount?: number
}

// ---------------------------------------------------------------------------
// cart
// ---------------------------------------------------------------------------

export interface AddCartItemRequest {
  product_id: string
  sku_id: string
  quantity: number
}

export interface UpdateCartItemRequest {
  quantity: number
}

export interface SelectCartItemsRequest {
  item_ids: string[]
  selected: boolean
}

// ---------------------------------------------------------------------------
// order
// ---------------------------------------------------------------------------

export interface OrderPreviewRequest {
  /** Source of the line items. Checkout can preview from cart or from a buy-now list. */
  source: 'cart' | 'direct'
  item_ids?: string[]
  direct_items?: { sku_id: string; quantity: number }[]
  address_id?: string
  coupon_code?: string
}

/**
 * Server-computed numbers for the checkout screen. The UI never recomputes these;
 * it only renders them (§106 "the backend is authoritative").
 */
export interface OrderPreview {
  items: {
    sku_id: string
    product_title: string
    sku_specs: Record<string, string>
    cover_url?: string
    quantity: number
    unit_price_amount: number
    subtotal_amount: number
  }[]
  /** Integer minor units. */
  items_amount: number
  /** Integer minor units. */
  discount_amount: number
  /** Integer minor units. */
  shipping_amount: number
  /** Integer minor units. */
  payable_amount: number
  coupon?: Coupon | null
  address?: Address | null
  /** Non-fatal warnings, e.g. a SKU went unavailable. */
  warnings?: string[]
}

export interface CreateOrderRequest {
  address_id: string
  /** Both required: Idempotency-Key header + this field (§96). */
  client_request_id: string
  source: 'cart' | 'direct'
  item_ids?: string[]
  direct_items?: { sku_id: string; quantity: number }[]
  coupon_code?: string
  remark?: string
}

export interface CancelOrderRequest {
  reason?: string
}

export interface AdminOrderQuery extends PageQuery {
  status?: string
  payment_status?: string
  fulfillment_status?: string
}

export interface ShipRequest {
  carrier: string
  tracking_no: string
  items?: { order_item_id: string; quantity: number }[]
  idempotency_key: string
}

// ---------------------------------------------------------------------------
// payment
// ---------------------------------------------------------------------------

export interface CreatePaymentRequest {
  order_no: string
  channel: 'MOCK' | 'ALIPAY' | 'WECHAT'
  client_request_id: string
}

// ---------------------------------------------------------------------------
// after-sales
// ---------------------------------------------------------------------------

export interface ApplyAfterSaleRequest {
  order_no: string
  type: 'REFUND_ONLY' | 'RETURN_REFUND'
  items: { order_item_id: string; quantity: number }[]
  /** Integer minor units. */
  requested_amount: number
  reason: string
  description?: string
  evidence_urls?: string[]
  client_request_id: string
}

export interface ApproveAfterSaleRequest {
  /** Integer minor units; may be lower than requested. */
  approved_amount: number
  remark?: string
}

export interface RefundRequest {
  /** Integer minor units. */
  amount: number
  reason?: string
  idempotency_key: string
}

export interface AfterSaleQuery extends PageQuery {
  status?: string
  order_no?: string
}

// ---------------------------------------------------------------------------
// analytics
// ---------------------------------------------------------------------------

export interface AnalyticsOverview {
  /** Integer minor units. */
  gmv_amount: number
  order_count: number
  paid_order_count: number
  /** Integer minor units. */
  refund_amount: number
  after_sale_count: number
  new_user_count: number
  /** 0..1 */
  payment_conversion_rate: number
  /** 0..1 */
  refund_rate: number
}

export interface SeriesPoint {
  /** ISO date, day granularity. */
  date: string
  /** Integer minor units for money series. */
  value: number
}

export interface SalesTrend {
  points: SeriesPoint[]
  money: boolean
}

export interface TopProduct {
  product_id: string
  title: string
  sold_quantity: number
  /** Integer minor units. */
  sales_amount: number
}

export interface OrderFunnelStage {
  stage: 'created' | 'paid' | 'shipped' | 'completed'
  count: number
}

// ---------------------------------------------------------------------------
// knowledge
// ---------------------------------------------------------------------------

export interface CreateKnowledgeBaseRequest {
  name: string
  description?: string
  embedding_model?: string
}

export interface RetrievalDebugRequest {
  query: string
  knowledge_base_id: string
  top_k?: number
  use_rerank?: boolean
  filters?: Record<string, string | number | boolean>
}

export interface RetrievalDebugResponse {
  /** The query after rewrite; may differ from the input. */
  rewritten_query: string
  stages: RetrievalStage[]
  final_evidence: Citation[]
  total_duration_ms: number
}

// ---------------------------------------------------------------------------
// agent (§101, §102)
// ---------------------------------------------------------------------------

export interface ChatRequest {
  thread_id?: string
  message: string
  /** 'assistant' | 'operations' | 'analytics' — selects the graph entry point. */
  agent_name: string
  knowledge_base_id?: string
  client_request_id: string
}

export interface ChatResponse {
  thread_id: string
  run_id: string
  final_answer: string
  citations: Citation[]
  tool_calls: ToolProgress[]
  pending_action_id?: string
  tokens_used?: number
}

export interface AgentRunQuery extends PageQuery {
  status?: string
  agent_name?: string
  thread_id?: string
}

export interface DecidePendingActionRequest {
  decision_reason?: string
  /** Echo of the hash the approver saw; the server rejects a changed payload. */
  payload_hash: string
}
