/**
 * The single place where API paths live.
 *
 * Paths are grouped by the backend module prefixes declared in
 * `backend/app/api/v1/router.py`:
 *   /auth /catalog /inventory /cart /orders /payments /fulfillments
 *   /after-sales /marketing /analytics /knowledge /agent /governance /audit
 *
 * §99 requires TASK-BASED endpoints (`/publish`, `/ship`, `/approve`, ...) instead
 * of arbitrary `PATCH {status}` writes. Keep that shape when adding endpoints: a
 * status transition is always an explicit intent.
 *
 * `{param}` segments are substituted by `path()`.
 */

export const API = {
  // -- identity -----------------------------------------------------------
  auth: {
    login: '/auth/login',
    logout: '/auth/logout',
    refresh: '/auth/refresh',
    me: '/auth/users/me',
    permissions: '/auth/users/me/permissions',
  },

  // -- catalog ------------------------------------------------------------
  catalog: {
    products: '/catalog/public/products',
    productDetail: (id: string) => `/catalog/public/products/${id}`,
    categories: '/catalog/public/categories',
    brands: '/catalog/public/brands',
    adminProducts: '/catalog/admin/products',
    adminProduct: (id: string) => `/catalog/admin/products/${id}`,
    /**
     * TASK endpoint (§99). Path is the FROZEN one from PROJECT_BASELINE.yaml
     * `task_endpoints`: `POST /api/v1/products/{id}/publish` — note the absence of the
     * `/catalog/admin` module prefix, unlike the list/detail routes above.
     */
    publish: (id: string) => `/products/${id}/publish`,
    unpublish: (id: string) => `/products/${id}/unpublish`,
    skus: (productId: string) => `/catalog/admin/products/${productId}/skus`,
  },

  // -- inventory ----------------------------------------------------------
  inventory: {
    available: (skuId: string | number) => `/inventory/customer/stock/${skuId}`,
    adminStock: '/inventory/admin/stock',
    /**
     * TASK endpoint (§99, API_CONTRACT.md §7). FROZEN path is
     * `POST /api/v1/inventory/adjustments` — note it is NOT sku-keyed: `warehouse_id` and
     * `sku_id` travel in the BODY, and the body is exactly
     * `{warehouse_id, sku_id, version, delta_available, reason}`.
     */
    adjustments: '/inventory/adjustments',
    adjustmentsPreview: '/inventory/adjustments/preview',
    movements: '/inventory/admin/movements',
  },

  // -- cart ---------------------------------------------------------------
  cart: {
    current: '/cart/customer/cart',
    items: '/cart/customer/cart/items',
    item: (itemId: string) => `/cart/customer/cart/items/${itemId}`,
    select: '/cart/customer/cart/select',
    clear: '/cart/customer/cart/items',
  },

  // -- orders (§96, §99) --------------------------------------------------
  orders: {
    preview: '/orders/orders/preview',
    create: '/orders/orders',
    list: '/orders/orders',
    detail: (orderNo: string) => `/orders/orders/${orderNo}`,
    /**
     * TASK endpoints (§99), FROZEN paths from PROJECT_BASELINE.yaml `task_endpoints`:
     *   POST /api/v1/orders/{order_no}/cancel
     *   POST /api/v1/orders/{order_no}/confirm-receipt
     * They sit at the `/orders` prefix WITHOUT the `/orders/orders` module duplication the
     * list/detail routes use.
     */
    cancel: (orderNo: string) => `/orders/${orderNo}/cancel`,
    confirmReceipt: (orderNo: string) => `/orders/${orderNo}/confirm-receipt`,
    adminList: '/orders/admin',
    adminDetail: (orderNo: string) => `/orders/admin/${orderNo}`,
  },

  // -- payments -----------------------------------------------------------
  payments: {
    create: '/payments/customer/payments',
    detail: (paymentId: string) => `/payments/customer/payments/${paymentId}`,
    byOrder: (orderNo: string) => `/payments/customer/payments/by-order/${orderNo}`,
    /** Mock channel only (§100 MockPay). */
    mockPay: (paymentId: string) => `/payments/customer/payments/${paymentId}/mock-pay`,
    mockCallback: (paymentId: string) => `/payments/callbacks/mock/${paymentId}`,
  },

  // -- fulfillment --------------------------------------------------------
  fulfillment: {
    shipments: (orderNo: string) => `/fulfillments/customer/orders/${orderNo}/shipments`,
    /**
     * TASK endpoint (§99). FROZEN path from PROJECT_BASELINE.yaml `task_endpoints` and
     * API_CONTRACT.md §4: `POST /api/v1/fulfillments/{id}/ship`.
     *
     * The id is a NUMBER (API_CONTRACT.md §5) and is fulfillment-scoped, because an order may
     * ship in several packages — "ship this order" is not a well-formed instruction. The body
     * accepts EXACTLY `carrier` / `tracking_no` / `item_quantities` (§110 mass-assignment guard).
     */
    ship: (fulfillmentId: number | string) => `/fulfillments/${fulfillmentId}/ship`,
    /**
     * API_CONTRACT.md §5.2 — the fulfillment QUEUE. Added because an operator works from a
     * queue, not by opening orders one at a time, and because without it there is no interface
     * capable of discovering the id that `ship` requires. Paged envelope; filters `order_no`
     * and `fulfillment_status`.
     */
    adminList: '/fulfillments/admin',
  },

  // -- after-sales --------------------------------------------------------
  afterSales: {
    apply: '/after-sales/customer/after-sales',
    list: '/after-sales/customer/after-sales',
    detail: (afterSaleNo: string) => `/after-sales/customer/after-sales/${afterSaleNo}`,
    cancel: (afterSaleNo: string) => `/after-sales/customer/after-sales/${afterSaleNo}/cancel`,
    adminList: '/after-sales/admin/after-sales',
    adminDetail: (afterSaleNo: string) => `/after-sales/admin/after-sales/${afterSaleNo}`,
    approve: (afterSaleNo: string) => `/after-sales/admin/after-sales/${afterSaleNo}/approve`,
    reject: (afterSaleNo: string) => `/after-sales/admin/after-sales/${afterSaleNo}/reject`,
    refund: (afterSaleNo: string) => `/after-sales/admin/after-sales/${afterSaleNo}/refund`,
  },

  // -- addresses ----------------------------------------------------------
  addresses: {
    list: '/auth/users/addresses',
    create: '/auth/users/addresses',
    update: (id: string) => `/auth/users/addresses/${id}`,
    remove: (id: string) => `/auth/users/addresses/${id}`,
    setDefault: (id: string) => `/auth/users/addresses/${id}/default`,
  },

  // -- marketing ----------------------------------------------------------
  marketing: {
    coupons: '/marketing/coupons',
    myCoupons: '/marketing/coupons/mine',
    claim: (couponId: string) => `/marketing/coupons/${couponId}/claim`,
    promotions: '/marketing/promotions',
    adminCoupons: '/marketing/admin/coupons',
    adminPromotions: '/marketing/admin/promotions',
  },

  // -- analytics ----------------------------------------------------------
  analytics: {
    /**
     * Metric-keyed (§8). Every analytics endpoint returns the same envelope, so there is one
     * parameterised path rather than four near-identical ones. The five frozen metric names are
     * `sales.gmv`, `sales.order_count`, `inventory.turnover`, `product.performance`, `refund.rate`
     * (`ANALYTICS_METRICS` in `@/types/frozen-contract`).
     *
     * ASSUMPTION (API_CONTRACT.md §8 froze the response envelope and the metric NAMES, not the
     * route): a metric path segment under the existing `/analytics/admin` prefix. If the backend
     * exposes one route per metric, only this function changes.
     */
    metric: (metric: string) => `/analytics/admin/metrics/${metric}`,
  },

  // -- knowledge / RAG (§103) ---------------------------------------------
  knowledge: {
    bases: '/knowledge/admin/bases',
    base: (baseId: string) => `/knowledge/admin/bases/${baseId}`,
    documents: (baseId: string) => `/knowledge/admin/bases/${baseId}/documents`,
    document: (docId: string) => `/knowledge/admin/documents/${docId}`,
    reprocess: (docId: string) => `/knowledge/admin/documents/${docId}/reprocess`,
    archive: (docId: string) => `/knowledge/admin/documents/${docId}/archive`,
    /** Retrieval debug: rewrite -> filter -> dense -> sparse -> fusion -> rerank -> evidence. */
    retrievalDebug: '/knowledge/retrieval/debug',
    evaluation: '/knowledge/admin/evaluation',
  },

  // -- agent (§101, §102) -------------------------------------------------
  agent: {
    /** SSE event stream. Consumed via `http.rawFetch`, not the JSON client. */
    chatStream: '/agent/chat/stream',
    chat: '/agent/chat',
    threads: '/agent/chat/threads',
    messages: (threadId: string) => `/agent/chat/threads/${threadId}/messages`,
    runs: '/agent/runs',
    run: (runId: string) => `/agent/runs/${runId}`,
    cancelRun: (runId: string) => `/agent/runs/${runId}/cancel`,
    adminTools: '/agent/admin/tools',
  },

  // -- governance: pending actions / approval (§101 HITL) -----------------
  governance: {
    /**
     * TASK endpoints (§99). FROZEN paths from PROJECT_BASELINE.yaml `task_endpoints`:
     *   POST /api/v1/pending-actions/{id}/approve
     *   POST /api/v1/pending-actions/{id}/reject
     * Note the hyphenated `pending-actions` prefix sits OUTSIDE the `/governance` module
     * namespace, unlike the list/detail routes below.
     */
    pendingActions: '/governance/pending_actions',
    pendingAction: (actionId: string) => `/governance/pending_actions/${actionId}`,
    approve: (actionId: string) => `/pending-actions/${actionId}/approve`,
    reject: (actionId: string) => `/pending-actions/${actionId}/reject`,
    tools: '/governance/tools',
  },

  // -- audit --------------------------------------------------------------
  audit: {
    list: '/audit/admin/records',
  },

  // -- health (§130) — outside `/api/v1` ----------------------------------
  health: {
    live: '/health/live',
    ready: '/health/ready',
  },
} as const

/** Substitute `{param}` placeholders. Throws on a missing parameter. */
export function path(template: string, params: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (_match, key: string) => {
    const value = params[key]
    if (value === undefined) throw new Error(`Missing path parameter: ${key}`)
    return encodeURIComponent(String(value))
  })
}
