/**
 * Order module (§96).
 *
 * INVARIANTS THE UI MUST RESPECT
 *  - `create` sends BOTH an `Idempotency-Key` header (added by the client) and a
 *    `client_request_id` body field. A double-clicked "提交订单" must therefore
 *    produce ONE order, and a retry after a network error is safe.
 *  - `cancel` and `confirm-receipt` are task endpoints, not status patches (§99).
 *  - Amounts come from `POST /orders/preview` and are never computed client-side.
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import type {
  AdminOrderQuery,
  CancelOrderRequest,
  CreateOrderRequest,
  OrderPreview,
  OrderPreviewRequest,
} from '@/types/api-contract'
import type { PageQuery } from '@/types/api'
import type { Order, OrderDetail, OrderSummary, Paged } from '@/types/domain'
import type { Fulfillment, ShipFulfillmentRequest } from '@/types/frozen-contract'

export const orderApi = {
  async preview(payload: OrderPreviewRequest): Promise<OrderPreview> {
    return httpClient.post<OrderPreview>(API.orders.preview, payload)
  },

  async create(payload: CreateOrderRequest): Promise<Order> {
    // The client injects `Idempotency-Key` for every POST with a body, and we pin
    // the same value in the body so the server can match header and payload.
    return httpClient.post<Order>(API.orders.create, payload, {
      idempotencyKey: payload.client_request_id,
    })
  },

  /**
   * The consumer order list. Returns the frozen PAGED envelope, not a bare array
   * (API_CONTRACT.md §3): a bare array cannot carry a total, so pagination would have to be
   * dropped or the server would have to make a breaking change.
   *
   * Rows are `OrderSummary` — no `items[]`, no `shipments[]` (API_CONTRACT.md §6).
   */
  async list(params: PageQuery & { status?: string } = {}): Promise<Paged<OrderSummary>> {
    return httpClient.get<Paged<OrderSummary>>(API.orders.list, { params })
  },

  async detail(orderNo: string): Promise<Order> {
    return httpClient.get<Order>(API.orders.detail(orderNo))
  },

  async cancel(orderNo: string, payload: CancelOrderRequest = {}): Promise<Order> {
    return httpClient.post<Order>(API.orders.cancel(orderNo), payload)
  },

  async confirmReceipt(orderNo: string): Promise<Order> {
    return httpClient.post<Order>(API.orders.confirmReceipt(orderNo), {})
  },

  /** The fulfillments inside one order — how a client discovers a shippable id (§5.1). */
  async shipments(orderNo: string): Promise<Fulfillment[]> {
    return httpClient.get<Fulfillment[]>(API.fulfillment.shipments(orderNo))
  },
}

export const orderAdminApi = {
  /** List rows: `OrderSummary` carries no `items[]`/`shipments[]`/address (API_CONTRACT.md §6). */
  async list(query: AdminOrderQuery = {}): Promise<Paged<OrderSummary>> {
    return httpClient.get<Paged<OrderSummary>>(API.orders.adminList, { params: query })
  },

  async detail(orderNo: string): Promise<OrderDetail> {
    return httpClient.get<OrderDetail>(API.orders.adminDetail(orderNo))
  },
}

/**
 * Fulfillment administration.
 *
 * Shipping lives HERE, not on the order module, because the frozen task endpoint is
 * `POST /fulfillments/{id}/ship` (PROJECT_BASELINE.yaml `task_endpoints`) — it is keyed by
 * FULFILLMENT id, not order number. The console therefore resolves the order's unshipped
 * fulfillment first and passes its id in; see `canShipOrder()` in
 * `src/domain/orders/availability.ts` for why the id is mandatory.
 *
 * The body is EXACTLY `{carrier, tracking_no, item_quantities}` (API_CONTRACT.md §5, §110
 * mass-assignment guard). There is deliberately NO `idempotency_key`: the guard against a double
 * ship is the fulfillment's own state (`FULFILLMENT_ALREADY_SHIPPED`, 70 003), so adding a key
 * here would be an invented field the server does not accept.
 *
 * `carrier` is a carrier CODE (e.g. "SF"), not free text.
 */
export const fulfillmentAdminApi = {
  async ship(fulfillmentId: number | string, payload: ShipFulfillmentRequest): Promise<Fulfillment> {
    return httpClient.post<Fulfillment>(API.fulfillment.ship(fulfillmentId), payload)
  },

  /**
   * The fulfillment QUEUE (§5.2). An operator works from a queue, not by opening orders one at
   * a time, and this is also the only surface that can discover an id to ship.
   */
  async list(
    query: PageQuery & { order_no?: string; fulfillment_status?: string } = {},
  ): Promise<Paged<Fulfillment>> {
    return httpClient.get<Paged<Fulfillment>>(API.fulfillment.adminList, { params: query })
  },
}
