/**
 * After-sales module (§98).
 *
 * REFUND CAPS ARE SERVER-ENFORCED (`REFUND_EXCEEDS_PAID_AMOUNT` /
 * `REFUND_EXCEEDS_ITEM_AMOUNT`). The UI shows `refundable_amount` from the server
 * and uses it as an input `max`, but that is a convenience: the backend rejects an
 * over-refund regardless of what the form allowed (§104 — guards are UX only).
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import type {
  AfterSaleQuery,
  ApplyAfterSaleRequest,
  ApproveAfterSaleRequest,
  RefundRequest,
} from '@/types/api-contract'
import type { AfterSale, Paged, RefundRecord } from '@/types/domain'

export const afterSaleApi = {
  async apply(payload: ApplyAfterSaleRequest): Promise<AfterSale> {
    return httpClient.post<AfterSale>(API.afterSales.apply, payload, {
      idempotencyKey: payload.client_request_id,
    })
  },

  async list(params: { page?: number; page_size?: number; status?: string } = {}): Promise<AfterSale[]> {
    return httpClient.get<AfterSale[]>(API.afterSales.list, { params })
  },

  async detail(afterSaleNo: string): Promise<AfterSale> {
    return httpClient.get<AfterSale>(API.afterSales.detail(afterSaleNo))
  },

  async cancel(afterSaleNo: string): Promise<AfterSale> {
    return httpClient.post<AfterSale>(API.afterSales.cancel(afterSaleNo), {})
  },
}

export const afterSaleAdminApi = {
  async list(query: AfterSaleQuery = {}): Promise<Paged<AfterSale>> {
    return httpClient.get<Paged<AfterSale>>(API.afterSales.adminList, { params: query })
  },

  async detail(afterSaleNo: string): Promise<AfterSale> {
    return httpClient.get<AfterSale>(API.afterSales.adminDetail(afterSaleNo))
  },

  /** Task endpoint (§99). The approved amount may be lower than requested. */
  async approve(afterSaleNo: string, payload: ApproveAfterSaleRequest): Promise<AfterSale> {
    return httpClient.post<AfterSale>(API.afterSales.approve(afterSaleNo), payload)
  },

  async reject(afterSaleNo: string, rejectReason: string): Promise<AfterSale> {
    return httpClient.post<AfterSale>(API.afterSales.reject(afterSaleNo), {
      reject_reason: rejectReason,
    })
  },

  /** Executes the refund. Amount must be <= the server's refundable cap. */
  async refund(afterSaleNo: string, payload: RefundRequest): Promise<RefundRecord> {
    return httpClient.post<RefundRecord>(API.afterSales.refund(afterSaleNo), payload, {
      idempotencyKey: payload.idempotency_key,
    })
  },
}
