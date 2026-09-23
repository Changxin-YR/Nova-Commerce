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
  /**
   * Integer minor units still refundable (`paid_amount - refunded_amount`, floored at 0),
   * SERVER-OWNED because INV-005 is (§6 / §11 addendum).
   *
   * On EVERY order payload, list rows included. The consumer order list renders its refund
   * control from this number, and `undefined > 0` is `false` — a missing field would silently
   * hide the control instead of throwing, which is the failure mode the addendum was written
   * for. The client-side `paid - refunded` fallback this field replaced is deleted.
   */
  refundable_amount: number
  /** Already masked by the server (§94) — the client must NOT try to un-mask it. */
  receiver_name: string
  /** Already masked by the server (§94). */
  receiver_phone: string
  created_at: string
  paid_at: string | null
  expires_at: string | null
}

/**
 * List rows. Deliberately has NO `items[]`, `shipments[]` or address.
 *
 * The two summary fields are the §11 addendum: a list endpoint must not hydrate every order's
 * goods, but a list that cannot name a single product forced either an N+1 detail fetch per row or
 * a blank column. Both are BACKEND-OWNED — computing them client-side is impossible, which is
 * exactly why this frontend refused to invent them and reported the gap instead.
 */
export type OrderSummary = OrderBase & {
  /** Total number of UNITS in the order (not the number of distinct lines). */
  item_count: number
  /** Display name of the first line (`product_name` + `sku_name`), truncated to 200 chars. */
  first_item_name: string
}

/** Detail adds `items[]`, `shipments[]`, the address snapshot and the two server-owned extras. */
export interface OrderDetail extends OrderBase {
  /** The full address line, when the detail payload provides it. */
  full_address?: string
  /**
   * `null` unless `order_status` is `CANCELLED`/`CLOSED` (§11 addendum). Recorded by the cancel
   * workflow, so the client cannot infer it — the cancel endpoint accepts the reason as its writer.
   */
  cancel_reason: string | null
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

// ---------------------------------------------------------------------------
// §13 Promotion / coupon templates / roles / users
// ---------------------------------------------------------------------------

/**
 * WHY THESE LIVE HERE AND ARE ADDITIVE.
 *
 * `src/api/marketing.ts` still carries a LOCAL, invented `Promotion`
 * (`type: 'DISCOUNT' | 'FULL_REDUCTION' | 'BUNDLE'`, `start_at`/`end_at`, `rule:
 * Record<string, unknown>`). §13 has now frozen the real shape, so the local one is superseded — but
 * replacing it is a migration that touches `MarketingView` and its tests, not a type edit. These
 * definitions are therefore added WITHOUT consuming them yet: the tree stays green and the next agent
 * has the frozen shapes to migrate onto. Two shapes for one resource is the defect this codebase
 * already removed once for `Order`; this note exists so the same thing does not quietly re-form.
 *
 * §13.5 lists what is STILL unfrozen after this batch: the full `AgentRun`/`PendingAction` shapes,
 * sort parameters, export formats, employee/warehouse/brand/category CRUD, promotion lifecycle beyond
 * the three states below, and coupon issuance to users. Absence there is not permission.
 */

/** §39. `rule_config` is DISCRIMINATED by this field, not a bag of nullable keys. */
export type PromotionType = 'DIRECT_DISCOUNT' | 'PERCENT_DISCOUNT' | 'FULL_REDUCTION'

export type PromotionStatus = 'DRAFT' | 'ACTIVE' | 'ENDED'

/**
 * The three variants, discriminated so a caller cannot read `discount_bps` off a fixed-amount rule.
 *
 * RATES ARE BASIS POINTS, NEVER FLOATS: 12.5% is exactly 1250 bps, while `0.125` is not exactly
 * representable — and a discount computed from a float is a ledger that does not add up. Same reason
 * money is integer minor units.
 */
export type PromotionRuleConfig =
  | { discount_amount: number }
  | { discount_bps: number; max_discount_amount: number | null }
  | { threshold_amount: number; reduction_amount: number; max_discount_amount: number | null }

/**
 * `scope` is EXPLICIT rather than an implicit "everything": an all-products promotion says so
 * (`all_products: true`) instead of being inferred from empty arrays, because inferring it is how a
 * promotion accidentally covers the whole catalogue.
 */
export interface PromotionScope {
  all_products: boolean
  product_ids: number[]
  category_ids: number[]
  brand_ids: number[]
}

export interface Promotion {
  id: number
  promotion_no: string
  merchant_id: number
  name: string
  description?: string
  promotion_type: PromotionType
  status: PromotionStatus
  priority: number
  stackable: boolean
  rule_config: PromotionRuleConfig
  scope: PromotionScope
  starts_at: string
  ends_at: string
  total_quota: number
  used_quota: number
  created_at: string
  updated_at: string
}

/** §13.2. `conflicts` is non-empty rather than a 409: a conflict is information to decide with. */
export interface PromotionConflict {
  promotion_id: number
  promotion_no: string
  reason: string
}

export interface PromotionImpactEstimate {
  affected_sku_count: number
  affected_order_count_30d: number
  /** Integer minor units. */
  estimated_discount_amount_30d: number
}

/**
 * The preview response IS the exact body `POST /marketing/promotions` accepts, plus these fields.
 *
 * `preview_token` is what makes §47 ENFORCEABLE rather than advisory: the create call must carry it
 * and the server rejects a token whose payload changed. Without it "preview first" is a convention a
 * client can skip; with it, a single-submit create flow cannot be written — the same compile-time
 * property `CouponCreatePayload` already has in `src/api/marketing.ts`.
 */
export interface PromotionPreview {
  preview_token: string
  expires_at: string
  /** Same shape, with `id` / `promotion_no` null before the write. */
  promotion: Omit<Promotion, 'id' | 'promotion_no'> & {
    id: number | null
    promotion_no: string | null
  }
  estimated_impact: PromotionImpactEstimate
  conflicts: PromotionConflict[]
  warnings: string[]
}

/** §13.3. A discriminator the client can switch on, rather than a nullable pair to guess about. */
export type CouponType = 'FIXED_AMOUNT' | 'PERCENT_DISCOUNT'
export type CouponTemplateStatus = 'DRAFT' | 'ACTIVE' | 'ENDED'
export type CouponValidityType = 'RELATIVE' | 'ABSOLUTE'

export interface CouponScope {
  all_products: boolean
  product_ids: number[]
  category_ids: number[]
}

export interface CouponTemplate {
  id: number
  template_no: string
  merchant_id: number
  name: string
  coupon_type: CouponType
  status: CouponTemplateStatus
  /** Set for `FIXED_AMOUNT`; null for `PERCENT_DISCOUNT`. */
  face_value_amount: number | null
  /** Set for `PERCENT_DISCOUNT`; null for `FIXED_AMOUNT`. Basis points. */
  discount_bps: number | null
  threshold_amount: number
  max_discount_amount: number | null
  total_quota: number
  issued_count: number
  per_user_limit: number
  validity_type: CouponValidityType
  valid_days: number | null
  valid_from: string | null
  valid_to: string | null
  applicable_scope: CouponScope
  created_at: string
}

/** Mirrors `PromotionPreview`. */
export interface CouponPreview {
  preview_token: string
  expires_at: string
  coupon: Omit<CouponTemplate, 'id' | 'template_no'> & {
    id: number | null
    template_no: string | null
  }
  estimated_impact: {
    estimated_issue_count: number
    /** Integer minor units. */
    estimated_discount_amount: number
  }
  warnings: string[]
}

/* -- §13.4 Role / User ----------------------------------------------------- */

/** The `is_grantable` flag is what a checkbox UI cannot express on its own. */
export interface RolePermission {
  code: string
  resource: string
  action: string
  is_grantable: boolean
}

export type DataScope = 'ALL' | 'MERCHANT' | 'SELF'

export interface Role {
  id: number
  code: string
  name: string
  description?: string
  /** System roles are not editable by an ordinary console user. */
  is_system: boolean
  data_scope: DataScope
  permissions: RolePermission[]
  user_count: number
  created_at: string
  updated_at: string
}

export interface UserRoleRef {
  id: number
  code: string
  name: string
}

export type UserType = 'STAFF' | 'CUSTOMER'
export type UserStatus = 'ACTIVE' | 'DISABLED' | 'LOCKED'

export interface User {
  id: number
  username: string
  /** Masked by the server (§94) on list AND detail — never un-mask. */
  email: string
  /** Masked by the server (§94) on list AND detail — never un-mask. */
  phone: string
  display_name: string
  user_type: UserType
  status: UserStatus
  merchant_id: number | null
  data_scope: DataScope
  data_scope_override: DataScope | null
  roles: UserRoleRef[]
  last_login_at: string | null
  created_at: string
}

/**
 * `PUT /system/roles/{id}/permissions` body — the COMPLETE explicit set, never a delta (§12.2).
 *
 * A delta invites a caller to omit a permission it did not know about and silently revoke it. The
 * server additionally owns four rules a checkbox UI cannot express (§13.4):
 *  1. replace the set wholesale;
 *  2. REFUSE any change lowering the `risk_level` of an `is_write` tool without separate audited
 *     approval (§65) — the rule this whole section exists for;
 *  3. reject any permission whose `is_grantable` is false, whatever was sent;
 *  4. audit the before/after set with actor and reason — a permission change is an audit event, not a
 *     settings save.
 *
 * The client therefore renders a REVIEW step (like the coupon flow), and must not present this as a
 * casual toggle.
 */
export interface UpdateRolePermissionsRequest {
  permissions: string[]
  reason: string
}
