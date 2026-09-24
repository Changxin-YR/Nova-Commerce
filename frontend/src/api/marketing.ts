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
import type { PageQuery } from '@/types/api'
import type { CouponPreview, CouponTemplate, Promotion, PromotionPreview } from '@/types/frozen-contract'

export interface OwnedCoupon {
  id: number
  template_id: number
  merchant_id: number
  status: 'UNUSED' | 'LOCKED' | 'USED' | 'EXPIRED'
  valid_from: string
  valid_to: string
  order_id: number | null
  locked_at: string | null
  used_at: string | null
}

export type CouponPayload = Omit<
  CouponTemplate,
  'id' | 'template_no' | 'merchant_id' | 'status' | 'issued_count' | 'created_at'
>

/**
 * §13.1 froze the real promotion shape, which REPLACES the local invented one that used to live here
 * (`type: 'DISCOUNT' | 'FULL_REDUCTION' | 'BUNDLE'`, `rule: Record<string, unknown>`,
 * `start_at`/`end_at`, string id).
 *
 * The local version is DELETED rather than kept alongside. Two shapes for one resource is exactly the
 * defect this codebase already removed once for `Order`, and keeping both is how half the app ends up
 * compiling against the wrong one. Consumers importing `Promotion` from here still work — this is now
 * a re-export, so there is exactly ONE definition.
 *
 * Field renames the view had to absorb: `type` -> `promotion_type`, `start_at` -> `starts_at`,
 * `end_at` -> `ends_at`, and the opaque `rule` bag -> the discriminated `rule_config`.
 */
export type { Promotion, PromotionPreview } from '@/types/frozen-contract'

/** The create body: everything the server owns is omitted, so a form cannot send it by accident. */
export type PromotionDraft = Omit<
  Promotion,
  'id' | 'promotion_no' | 'merchant_id' | 'status' | 'used_quota' | 'created_at' | 'updated_at'
>

/**
 * A promotion create MUST carry the preview token (§13.2), exactly like the coupon equivalent.
 *
 * Required in the TYPE on purpose: it makes "preview first" (section 47) a compile-time property
 * rather than a convention somebody has to remember, so a single-submit create flow cannot be written
 * against this signature.
 */
export type PromotionCreatePayload = PromotionDraft & { preview_token: string }

export const marketingApi = {
  async availableCoupons(query: PageQuery = {}): Promise<Paged<CouponTemplate>> {
    return httpClient.get<Paged<CouponTemplate>>(API.marketing.availableCoupons, { params: query })
  },

  async myCoupons(): Promise<OwnedCoupon[]> {
    return httpClient.get<OwnedCoupon[]>(API.marketing.myCoupons)
  },

  async claim(templateId: string | number): Promise<OwnedCoupon> {
    return httpClient.post<OwnedCoupon>(API.marketing.claim(String(templateId)), {})
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
export type CouponPreviewResult = CouponPreview

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

  async coupons(query: PageQuery = {}): Promise<Paged<CouponTemplate>> {
    return httpClient.get<Paged<CouponTemplate>>(API.marketing.adminCoupons, { params: query })
  },

  /**
   * PREVIEW a promotion (§13.2). Returns the exact promotion payload the create call accepts, plus a
   * `preview_token`, an impact estimate, and a POPULATED `conflicts` list.
   *
   * `conflicts` is a list rather than a 409 on purpose: a conflict is information the operator needs in
   * order to decide, not an error that stops them looking.
   */
  async previewPromotion(payload: PromotionDraft): Promise<PromotionPreview> {
    return httpClient.post<PromotionPreview>(API.marketing.promotionsPreview, payload)
  },

  /**
   * CREATE a promotion. Carries the `preview_token` from the preview so the server can prove the
   * operator approved the payload being written (§13.2).
   */
  async createPromotion(payload: PromotionCreatePayload): Promise<Promotion> {
    return httpClient.post<Promotion>(API.marketing.promotionsCreate, payload)
  },

  /**
   * Step 2 of 2: the ONLY place a coupon is written, and it requires the token from step 1 so the
   * values written are the values the operator approved (§12.1).
   *
   * PATH NOTE: this posts to `/marketing/coupons` — the frozen §12.1 path — NOT to
   * `/marketing/admin/coupons`, which is only the LIST route. Pointing create at the admin path was a
   * real mismatch, the same class of defect as the knowledge task endpoints.
   */
  async createCoupon(payload: CouponCreatePayload): Promise<CouponTemplate> {
    return httpClient.post<CouponTemplate>(API.marketing.couponsCreate, payload)
  },

  async publishCoupon(id: number): Promise<CouponTemplate> {
    return httpClient.post<CouponTemplate>(`${API.marketing.coupons}/${id}/publish`, {})
  },

  async unpublishCoupon(id: number): Promise<CouponTemplate> {
    return httpClient.post<CouponTemplate>(`${API.marketing.coupons}/${id}/unpublish`, {})
  },

  async promotions(query: PageQuery = {}): Promise<Paged<Promotion>> {
    return httpClient.get<Paged<Promotion>>(API.marketing.adminPromotions, { params: query })
  },
}
