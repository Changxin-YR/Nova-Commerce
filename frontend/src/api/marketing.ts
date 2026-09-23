/**
 * Marketing module (§98).
 *
 * Coupon lifecycle the UI must render faithfully: UNUSED -> LOCKED (an order holds
 * it) -> USED, plus EXPIRED. A LOCKED coupon is committed to another order, so the
 * UI shows it as unavailable rather than offering it again.
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import type { Paged } from '@/types/domain'
import type { Coupon } from '@/types/domain'
import type { PageQuery } from '@/types/api'

export interface CouponPayload {
  code: string
  name: string
  /** Integer minor units off. */
  discount_amount: number
  /** Integer minor units minimum spend. */
  threshold_amount: number
  valid_from: string
  valid_to: string
  total_quantity?: number
}

export interface Promotion {
  id: string
  name: string
  type: 'DISCOUNT' | 'FULL_REDUCTION' | 'BUNDLE'
  rule: Record<string, unknown>
  status: 'DRAFT' | 'ACTIVE' | 'ENDED'
  priority: number
  start_at: string
  end_at: string
}

export const marketingApi = {
  async myCoupons(status?: string): Promise<Coupon[]> {
    return httpClient.get<Coupon[]>(API.marketing.myCoupons, { params: { status } })
  },

  async claim(couponId: string): Promise<Coupon> {
    return httpClient.post<Coupon>(API.marketing.claim(couponId), {})
  },

  async promotions(): Promise<Promotion[]> {
    return httpClient.get<Promotion[]>(API.marketing.promotions)
  },
}

export const marketingAdminApi = {
  /**
   * TASK endpoints (§99, API_CONTRACT.md §4). Publishing a promotion is a named business intent,
   * never a `PATCH {status}`, and each returns the updated entity rather than 204.
   */
  async publishPromotion(id: string | number): Promise<Promotion> {
    return httpClient.post<Promotion>(API.marketing.publishPromotion(id), {})
  },

  async unpublishPromotion(id: string | number): Promise<Promotion> {
    return httpClient.post<Promotion>(API.marketing.unpublishPromotion(id), {})
  },

  async coupons(query: PageQuery = {}): Promise<Paged<Coupon>> {
    return httpClient.get<Paged<Coupon>>(API.marketing.adminCoupons, { params: query })
  },

  async createCoupon(payload: CouponPayload): Promise<Coupon> {
    return httpClient.post<Coupon>(API.marketing.adminCoupons, payload)
  },

  async promotions(query: PageQuery = {}): Promise<Paged<Promotion>> {
    return httpClient.get<Paged<Promotion>>(API.marketing.adminPromotions, { params: query })
  },
}
