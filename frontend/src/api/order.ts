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
 * `ShipRequest` carries the quantity split per order item, so a partial shipment is expressed
 * as data rather than as a separate endpoint. The server enforces
 * `FULFILLMENT_QUANTITY_EXCEEDS_ORDER` (70 001) and `FULFILLMENT_ALREADY_SHIPPED` (70 003).
 */
export const fulfillmentAdminApi = {
  async ship(fulfillmentId: string, payload: ShipRequest): Promise<Shipment> {
    // Idempotency key prevents a retried request from creating a second shipment.
    return httpClient.post<Shipment>(API.fulfillment.ship(fulfillmentId), payload, {
      idempotencyKey: payload.idempotency_key,
    })
  },
}
