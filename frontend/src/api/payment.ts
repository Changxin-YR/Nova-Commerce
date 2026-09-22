/**
 * Payment module (§96, §100 MockPay).
 *
 * The mock channel exists so the demo can prove the payment invariants without a
 * real PSP: `mockPay` asks the backend to treat the payment as settled through the
 * same callback path a real provider would use. The frontend NEVER marks an order
 * paid — it only navigates and then re-reads server state.
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import type { CreatePaymentRequest } from '@/types/api-contract'
import type { Payment } from '@/types/domain'

export const paymentApi = {
  async create(payload: CreatePaymentRequest): Promise<Payment> {
    return httpClient.post<Payment>(API.payments.create, payload, {
      idempotencyKey: payload.client_request_id,
    })
  },

  async detail(paymentId: string): Promise<Payment> {
    return httpClient.get<Payment>(API.payments.detail(paymentId))
  },

  async byOrder(orderNo: string): Promise<Payment> {
    return httpClient.get<Payment>(API.payments.byOrder(orderNo))
  },

  /** Mock channel only: drives the server-side callback, then the UI re-reads. */
  async mockPay(paymentId: string): Promise<Payment> {
    return httpClient.post<Payment>(API.payments.mockPay(paymentId), {
      client_request_id: `mockpay-${paymentId}`,
    })
  },
}
