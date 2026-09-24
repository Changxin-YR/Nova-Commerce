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
    productDetail: (id: string | number) => `/catalog/public/products/${id}`,
    categories: '/catalog/public/categories',
    brands: '/catalog/public/brands',
    adminProducts: '/catalog/admin/products',
    adminProduct: (id: string | number) => `/catalog/admin/products/${id}`,
    /**
     * TASK endpoint (§99). Path is the FROZEN one from PROJECT_BASELINE.yaml
     * `task_endpoints`: `POST /api/v1/products/{id}/publish` — note the absence of the
     * `/catalog/admin` module prefix, unlike the list/detail routes above.
     */
    publish: (id: string | number) => `/products/${id}/publish`,
    unpublish: (id: string | number) => `/products/${id}/unpublish`,
    skus: (productId: string | number) => `/catalog/admin/products/${productId}/skus`,
    uploadImage: (productId: string | number) => `/catalog/admin/products/${productId}/images`,
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

  // -- orders (§96, §99) --------------------------------------------------
  orders: {
    preview: '/orders/preview',
    create: '/orders',
    list: '/orders',
    detail: (orderNo: string) => `/orders/${orderNo}`,
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
    update: (id: string | number) => `/auth/users/addresses/${id}`,
    remove: (id: string | number) => `/auth/users/addresses/${id}`,
    setDefault: (id: string | number) => `/auth/users/addresses/${id}/default`,
  },

  // -- marketing ----------------------------------------------------------
  marketing: {
    coupons: '/marketing/coupons',
    myCoupons: '/marketing/coupons/mine',
    claim: (couponId: string) => `/marketing/coupons/${couponId}/claim`,
    promotions: '/marketing/promotions',
    adminCoupons: '/marketing/admin/coupons',
    adminPromotions: '/marketing/admin/promotions',
    /**
     * TASK endpoints (§99, API_CONTRACT.md §4). FROZEN paths:
     *   POST /api/v1/marketing/promotions/{id}/publish
     *   POST /api/v1/marketing/promotions/{id}/unpublish
     * They sit at the `/marketing/promotions` prefix, NOT the `/marketing/admin` module prefix the
     * list route uses — same shape as the product publish/unpublish split.
     */
    publishPromotion: (id: string | number) => `/marketing/promotions/${id}/publish`,
    unpublishPromotion: (id: string | number) => `/marketing/promotions/${id}/unpublish`,
    /**
     * §12.1 addendum. Creation is TWO calls because §47 requires preview-before-create and
     * `PROMOTION_PREVIEW_REQUIRED` (90003) is a business code the server enforces: the preview
     * returns a `preview_token`, and the create call must carry the SAME token back so the server can
     * prove the operator approved the thing that is being written.
     *
     * The create paths are `/marketing/promotions` and `/marketing/coupons` — NOT the
     * `/marketing/admin/...` prefix the LIST routes use (same split as the product task endpoints).
     */
    promotionsPreview: '/marketing/promotions/preview',
    promotionsCreate: '/marketing/promotions',
    couponsPreview: '/marketing/coupons/preview',
    couponsCreate: '/marketing/coupons',
  },

  // -- system management (§12.2) ------------------------------------------
  system: {
    /**
     * §12.2. Deliberately NOT a generic `PUT /system/roles/{id}`: that would make "downgrade a
     * CRITICAL write tool to READ" one checkbox away, which §65 forbids. The body carries the
     * COMPLETE explicit permission set (a delta invites a caller to omit a permission it did not know
     * about and silently revoke it).
     */
    rolePermissions: (roleId: string | number) => `/system/roles/${roleId}/permissions`,
    /** Role assignment is an authorization change, so it is explicit and audited (§133). */
    userRoles: (userId: string | number) => `/system/users/${userId}/roles`,
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
    /**
     * TASK endpoints (§99, API_CONTRACT.md §4). FROZEN paths are
     * `POST /api/v1/knowledge/documents/{id}/reprocess` and `.../archive` — note the
     * `documents/` segment, NOT the `/knowledge/admin/documents/` module path the read routes use.
     */
    reprocess: (docId: string) => `/knowledge/documents/${docId}/reprocess`,
    archive: (docId: string) => `/knowledge/documents/${docId}/archive`,
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
