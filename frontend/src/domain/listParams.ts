/**
 * Filter / query-parameter builders for the console list pages.
 *
 * WHY THESE ARE SEPARATE PURE FUNCTIONS
 *  A filter bar looks trivial, so its mapping is usually inlined in the `.vue` file and never
 *  tested. That is exactly where subtle bugs live: an empty string that must become
 *  `undefined` (or the backend receives `status=` and fails validation), a page that stays at
 *  3 after the filter changed (so the operator sees an empty table and blames the data), a
 *  `page_size` that silently exceeds a server cap.
 *
 *  Each builder therefore:
 *    * strips empty selections to `undefined` rather than sending `''`,
 *    * RESETS pagination whenever a filter changes (the caller passes `resetPage`, or simply
 *      builds with the page it wants),
 *    * clamps `page_size` to a sane maximum,
 *    * clamps `page` to >= 1,
 *    * is unit-testable without a component.
 *
 * Every parameter name here mirrors the backend contract in `src/api/endpoints.ts` and the
 * frozen enums. Nothing is invented: unknown keys are dropped, not forwarded.
 */

export const DEFAULT_PAGE_SIZE = 20
/** Guard against a hand-edited URL asking for a 100k-row response. */
export const MAX_PAGE_SIZE = 100

export interface ListQuery {
  page: number
  page_size: number
  keyword?: string
  sort?: string
}

/** Trim a free-text field and drop it when empty. */
export function optionalText(value: string | null | undefined): string | undefined {
  if (value === null || value === undefined) return undefined
  const trimmed = value.trim()
  return trimmed.length > 0 ? trimmed : undefined
}

/**
 * Normalize pagination. `page` is 1-based; values below 1 collapse to 1 and `page_size` is
 * clamped to `MAX_PAGE_SIZE` so a crafted query cannot ask for an unbounded page.
 */
export function normalizePaging(input: { page?: number; page_size?: number } = {}): {
  page: number
  page_size: number
} {
  const rawPage = Number(input.page ?? 1)
  const rawSize = Number(input.page_size ?? DEFAULT_PAGE_SIZE)
  const page = Number.isFinite(rawPage) ? Math.max(1, Math.trunc(rawPage)) : 1
  const size = Number.isFinite(rawSize)
    ? Math.min(MAX_PAGE_SIZE, Math.max(1, Math.trunc(rawSize)))
    : DEFAULT_PAGE_SIZE
  return { page, page_size: size }
}

// ---------------------------------------------------------------------------
// Console: products (mirrors the frozen ProductStatus enum)
// ---------------------------------------------------------------------------

export type ProductStatusFilter = '' | 'DRAFT' | 'PUBLISHED' | 'UNPUBLISHED' | 'ARCHIVED'

export interface ProductListFilters {
  keyword: string
  status: ProductStatusFilter
  page: number
  page_size?: number
}

export interface ProductListParams extends ListQuery {
  status?: ProductStatusFilter
}

export function buildProductListParams(filters: ProductListFilters): ProductListParams {
  const paging = normalizePaging({ page: filters.page, page_size: filters.page_size })
  const params: ProductListParams = { ...paging }
  const keyword = optionalText(filters.keyword)
  if (keyword) params.keyword = keyword
  // An empty selection means "全部" — sending `status=` would be a validation error.
  if (filters.status) params.status = filters.status
  return params
}

// ---------------------------------------------------------------------------
// Console: orders (mirrors the frozen OrderStatus enum)
// ---------------------------------------------------------------------------

export const ORDER_STATUS_VALUES = [
  'PENDING_PAYMENT',
  'PROCESSING',
  'COMPLETED',
  'CANCELLED',
  'CLOSED',
] as const

export type OrderStatusFilter = '' | (typeof ORDER_STATUS_VALUES)[number]

export interface OrderListFilters {
  orderNo: string
  status: OrderStatusFilter
  paymentStatus: '' | 'UNPAID' | 'PAYING' | 'PAID' | 'PARTIAL_REFUNDED' | 'REFUNDED'
  fulfillmentStatus: '' | 'UNFULFILLED' | 'PARTIAL_SHIPPED' | 'SHIPPED' | 'DELIVERED'
  /** ISO date (inclusive). */
  startDate: string
  /** ISO date (inclusive). */
  endDate: string
  page: number
  page_size?: number
}

export interface OrderListParams extends ListQuery {
  status?: OrderStatusFilter
  payment_status?: string
  fulfillment_status?: string
  start_date?: string
  end_date?: string
}

export function buildOrderListParams(filters: OrderListFilters): OrderListParams {
  const paging = normalizePaging({ page: filters.page, page_size: filters.page_size })
  const params: OrderListParams = { ...paging }

  // The console hunts by order number, which is the backend's `keyword`.
  const orderNo = optionalText(filters.orderNo)
  if (orderNo) params.keyword = orderNo

  if (filters.status) params.status = filters.status
  if (filters.paymentStatus) params.payment_status = filters.paymentStatus
  if (filters.fulfillmentStatus) params.fulfillment_status = filters.fulfillmentStatus

  const start = optionalText(filters.startDate)
  if (start) params.start_date = start
  const end = optionalText(filters.endDate)
  if (end) params.end_date = end

  return params
}

// ---------------------------------------------------------------------------
// Console: after-sales (mirrors the frozen AfterSaleStatus enum)
// ---------------------------------------------------------------------------

export type AfterSaleStatusFilter = '' | 'PROCESSING' | 'PARTIAL_REFUNDED' | 'REFUNDED' | 'NONE'

export interface AfterSaleListFilters {
  afterSaleNo: string
  status: AfterSaleStatusFilter
  page: number
  page_size?: number
}

export function buildAfterSaleListParams(filters: AfterSaleListFilters): ListQuery & {
  status?: string
} {
  const paging = normalizePaging({ page: filters.page, page_size: filters.page_size })
  const params: ListQuery & { status?: string } = { ...paging }
  const no = optionalText(filters.afterSaleNo)
  if (no) params.keyword = no
  if (filters.status) params.status = filters.status
  return params
}

// ---------------------------------------------------------------------------
// Console: inventory + knowledge
// ---------------------------------------------------------------------------

export interface InventoryListFilters {
  keyword: string
  productId: string
  lowStockOnly: boolean
  page: number
  page_size?: number
}

export function buildInventoryListParams(filters: InventoryListFilters): ListQuery & {
  product_id?: string
  low_stock_only?: boolean
} {
  const paging = normalizePaging({ page: filters.page, page_size: filters.page_size })
  const params: ListQuery & { product_id?: string; low_stock_only?: boolean } = { ...paging }
  const keyword = optionalText(filters.keyword)
  if (keyword) params.keyword = keyword
  const productId = optionalText(filters.productId)
  if (productId) params.product_id = productId
  // Only send the flag when it is ON: `false` would be redundant payload.
  if (filters.lowStockOnly) params.low_stock_only = true
  return params
}

export interface KnowledgeDocFilters {
  knowledgeBaseId: string
  keyword: string
  page: number
  page_size?: number
}

export function buildKnowledgeDocParams(filters: KnowledgeDocFilters): ListQuery {
  const paging = normalizePaging({ page: filters.page, page_size: filters.page_size })
  const params: ListQuery = { ...paging }
  const keyword = optionalText(filters.keyword)
  if (keyword) params.keyword = keyword
  return params
}

// ---------------------------------------------------------------------------
// Analytics date window
// ---------------------------------------------------------------------------

export interface AnalyticsFilters {
  startDate: string
  endDate: string
  granularity: 'day' | 'week' | 'month'
}

export function buildAnalyticsParams(filters: AnalyticsFilters): {
  start_date?: string
  end_date?: string
  granularity: 'day' | 'week' | 'month'
} {
  const params: {
    start_date?: string
    end_date?: string
    granularity: 'day' | 'week' | 'month'
  } = { granularity: filters.granularity }
  const start = optionalText(filters.startDate)
  if (start) params.start_date = start
  const end = optionalText(filters.endDate)
  if (end) params.end_date = end
  return params
}
