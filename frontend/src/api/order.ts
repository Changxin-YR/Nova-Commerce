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
  ShipRequest,
} from '@/types/api-contract'
import type { Order, Paged, Shipment } from '@/types/domain'

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

  async list(params: { page?: number; page_size?: number; status?: string } = {}): Promise<Order[]> {
    return httpClient.get<Order[]>(API.orders.list, { params })
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

  async shipments(orderNo: string): Promise<Shipment[]> {
    return httpClient.get<Shipment[]>(API.fulfillment.shipments(orderNo))
  },
}

export const orderAdminApi = {
  async list(query: AdminOrderQuery = {}): Promise<Paged<Order>> {
    return httpClient.get<Paged<Order>>(API.orders.adminList, { params: query })
  },

  async detail(orderNo: string): Promise<Order> {
    return httpClient.get<Order>(API.orders.adminDetail(orderNo))
  },

  /** Task endpoint (§99). Requires an idempotency key so a retry cannot double-ship. */
  async ship(orderNo: string, payload: ShipRequest): Promise<Shipment> {
    return httpClient.post<Shipment>(API.orders.adminShip(orderNo), payload, {
      idempotencyKey: payload.idempotency_key,
    })
  },
}
