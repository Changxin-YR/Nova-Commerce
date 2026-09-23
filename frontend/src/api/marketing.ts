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

/**
 * §12.1 freezes the ENDPOINTS for preview/create, not the `CouponPreview` SHAPE. This is therefore a
 * narrow local view model, deliberately containing only what the flow uses, and it is an ASSUMPTION
 * to confirm when the backend marketing module lands.
 *
 * The one field that is certain is `preview_token`: §12.1 states the create call must carry the same
 * token the preview returned, so the server can prove the operator approved what is being written.
 */
export interface CouponPreviewResult {
  preview_token: string
  /**
   * The contract says the preview response IS the exact payload the create call accepts, so the
   * previewed values are echoed back here. They are optional in the TYPE because their names come
   * from the request shape rather than from a frozen response shape.
   */
  code?: string
  name?: string
  discount_amount?: number
  threshold_amount?: number
  valid_from?: string
  valid_to?: string
  /** Server-side findings, e.g. an overlapping promotion or an unusable validity window. */
  warnings?: string[]
}

/**
 * A coupon create MUST carry the preview token (§12.1), and the server enforces it with
 * `PROMOTION_PREVIEW_REQUIRED` (90003).
 *
 * The token is required in the TYPE on purpose: it makes "preview first" a compile-time property
 * rather than a convention somebody has to remember. A single-submit create flow cannot be written
 * against this signature, which is exactly what §47 wants.
 */
export type CouponCreatePayload = CouponPayload & { preview_token: string }

export const marketingAdminApi = {
  /**
   * Step 1 of 2 (§47, §12.1). NEVER writes anything — it returns the previewed payload plus the
   * token the create call must echo.
   */
  async previewCoupon(payload: CouponPayload): Promise<CouponPreviewResult> {
    return httpClient.post<CouponPreviewResult>(API.marketing.couponsPreview, payload)
  },

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

  /**
   * Step 2 of 2: the ONLY place a coupon is written, and it requires the token from step 1 so the
   * values written are the values the operator approved (§12.1).
   *
   * PATH NOTE: this posts to `/marketing/coupons` — the frozen §12.1 path — NOT to
   * `/marketing/admin/coupons`, which is only the LIST route. Pointing create at the admin path was a
   * real mismatch, the same class of defect as the knowledge task endpoints.
   */
  async createCoupon(payload: CouponCreatePayload): Promise<Coupon> {
    return httpClient.post<Coupon>(API.marketing.couponsCreate, payload)
  },

  async promotions(query: PageQuery = {}): Promise<Paged<Promotion>> {
    return httpClient.get<Paged<Promotion>>(API.marketing.adminPromotions, { params: query })
  },
}
