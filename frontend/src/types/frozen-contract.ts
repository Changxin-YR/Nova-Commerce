/** Frozen API contract shapes (docs/architecture/API_CONTRACT.md).
 *
 * Every type here mirrors that document EXACTLY. Where my earlier scaffold invented a shape
 * (a nested `snapshot`, a `Shipment` with a string id, `on_hand`/`reserved` columns), the
 * invented version is replaced rather than kept alongside — two shapes for one resource is
 * how silent integration bugs start.
 *
 * Naming rules taken from the document:
 *   * numeric ids (`id: number`), not strings;
 *   * order state is `order_status` (the document is explicit that all four status fields are
 *     always present, because the UI must never infer one from another);
 *   * amounts are flat `*_amount` integers in MINOR UNITS;
 *   * money-ish fields are never percentages — see `AnalyticsUnit`.
 */

import type {
  AfterSaleStatus,
  FulfillmentStatus,
  OrderStatus,
  PaymentStatus,
} from '@/types/domain'

// ---------------------------------------------------------------------------
// §3 List envelope — frozen for EVERY list endpoint, never a bare array
// ---------------------------------------------------------------------------

export interface PageMeta {
  page: number
  page_size: number
  total: number
  /** `ceil(total / page_size)`, and `0` when total is 0. */
  total_pages: number
}

export interface Paged<T> {
  items: T[]
  /** Always present, even for a single page, so the client never branches on existence. */
  meta: PageMeta
}

/**
 * An empty page is `{items: [], meta: {..., total: 0, total_pages: 0}}` — never 404, never
 * null. The §108 Empty state is driven by `items.length === 0`.
 */
export function emptyPage<T>(pageSize = 20): Paged<T> {
  return { items: [], meta: { page: 1, page_size: pageSize, total: 0, total_pages: 0 } }
}

// ---------------------------------------------------------------------------
// §5 Fulfillment — the id that `POST /fulfillments/{id}/ship` takes
// ---------------------------------------------------------------------------

export interface FulfillmentItem {
  id: number
  order_item_id: number
  sku_id: number
  product_name: string
  sku_name: string
  quantity: number
}

export interface Fulfillment {
  /** The identifier `POST /fulfillments/{id}/ship` takes. A NUMBER. */
  id: number
  order_id: number
  order_no: string
  fulfillment_no: string
  fulfillment_status: FulfillmentStatus
  /** `null` until shipped. A carrier CODE (e.g. "SF"), not free text. */
  carrier: string | null
  tracking_no: string | null
  shipped_at: string | null
  delivered_at: string | null
  created_at: string
  items: FulfillmentItem[]
}

/**
 * §5 ship request — **exactly three fields**, nothing else is accepted (§110
 * mass-assignment guard). Note there is no `idempotency_key` here: the guard is the
 * fulfillment's own state (`FULFILLMENT_ALREADY_SHIPPED`, 70 003).
 */
export interface ShipFulfillmentRequest {
  carrier: string
  tracking_no: string
  item_quantities: { order_item_id: number; quantity: number }[]
}

// ---------------------------------------------------------------------------
// §6 Order
// ---------------------------------------------------------------------------

export interface OrderItem {
  id: number
  product_id: number
  sku_id: number
  product_name: string
  sku_name: string
  image_url: string | null
  unit_price: number
  quantity: number
  original_amount: number
  promotion_discount_amount: number
  coupon_discount_amount: number
  /** The discount actually attributed to this line. */
  allocated_discount_amount: number
  payable_amount: number
  refunded_amount: number
  after_sale_status: AfterSaleStatus
}

/** Base fields shared by the list row and the detail payload. */
export interface OrderBase {
  id: number
  order_no: string
  /** All four status fields are always present (§31–§34). */
  order_status: OrderStatus
  payment_status: PaymentStatus
  fulfillment_status: FulfillmentStatus
  after_sale_status: AfterSaleStatus
  original_amount: number
  promotion_discount_amount: number
  coupon_discount_amount: number
  shipping_amount: number
  payable_amount: number
  paid_amount: number
  refunded_amount: number
  /** Already masked by the server (§94) — the client must NOT try to un-mask it. */
  receiver_name: string
  /** Already masked by the server (§94). */
  receiver_phone: string
  created_at: string
  paid_at: string | null
  expires_at: string | null
}

/** List rows. Deliberately has NO `items[]`, `shipments[]` or address. */
export type OrderSummary = OrderBase

/** Detail adds `items[]`, `shipments[]` and the address/full-order extras. */
export interface OrderDetail extends OrderBase {
  /** The full address line, when the detail payload provides it. */
  full_address?: string
  items: OrderItem[]
  /** Fulfillment objects that can be shipped. This is where the ship id comes from. */
  shipments: Fulfillment[]
}

// ---------------------------------------------------------------------------
// §7 Inventory
// ---------------------------------------------------------------------------

export interface Inventory {
  id: number
  warehouse_id: number
  sku_id: number
  sku_no: string
  product_name: string
  sku_name: string
  available_qty: number
  /** Reserved by unpaid/unshipped orders. */
  locked_qty: number
  safety_stock: number
  /** Sellable units as the SERVER computes them; do not recompute in the UI. */
  sellable_qty: number
  on_hand_qty: number
  /** Optimistic-lock token. Required on adjust; stale => 409 / code 40002. */
  version: number
  updated_at: string
}

export interface AdjustmentPreview {
  before: Inventory
  after: Inventory
  /** Human-readable effect, when the server supplies one. */
  summary?: string
}

export interface CreateAdjustmentRequest {
  warehouse_id: number
  sku_id: number
  /** REQUIRED: the version the operator read (optimistic lock, §27). */
  version: number
  delta_available: number
  reason: string
}

/** 409 / code 40002 payload: `data` carries the CURRENT inventory so the UI can offer a refresh. */
export interface StaleVersionConflict {
  inventory: Inventory
}

// ---------------------------------------------------------------------------
// §8 Analytics — one envelope for all five metrics
// ---------------------------------------------------------------------------

/**
 * CRITICAL: the client MUST read `unit` rather than assume money.
 * `ratio` rendered as `¥` is a plausible-looking lie, and `count` as `¥` is worse than
 * showing nothing at all.
 */
export type AnalyticsUnit = 'minor_currency' | 'count' | 'ratio'

export const ANALYTICS_METRICS = [
  'sales.gmv',
  'sales.order_count',
  'inventory.turnover',
  'product.performance',
  'refund.rate',
] as const
export type AnalyticsMetric = (typeof ANALYTICS_METRICS)[number]

export interface AnalyticsPeriod {
  from: string
  to: string
  granularity: 'day' | 'week' | 'month'
}

export interface AnalyticsPoint {
  /** `YYYY-MM-DD` for day granularity, `YYYY-MM` for month. */
  bucket: string
  value: number
}

export interface AnalyticsSummary {
  total: number
  average: number
  /** A RATIO (e.g. 0.12), never a percentage in minor units. */
  change_ratio: number
}

export interface AnalyticsDimension {
  key: string
  label: string
  value: string
}

export interface AnalyticsEnvelope {
  metric: AnalyticsMetric | string
  unit: AnalyticsUnit
  period: AnalyticsPeriod
  /** Empty result is `series: []` with a zeroed `summary`, never null. */
  series: AnalyticsPoint[]
  summary: AnalyticsSummary
  dimensions?: AnalyticsDimension[]
}
