/**
 * Promotion / coupon availability — the marketing state-machine drift guard (§47, §99).
 *
 * The rule worth defending here: a promotion that has ENDED must not offer either action, and a
 * coupon that is LOCKED (held by another order) or USED must never be presented as available. Both
 * are cases where a plausible-looking button would only ever produce a server rejection.
 */

import { describe, expect, it } from 'vitest'
import {
  canPublishPromotion,
  canUnpublishPromotion,
  isCouponAvailable,
  isPromotionTerminal,
  promotionActionBlockedReason,
  type PromotionStatus,
} from '@/domain/marketing/availability'
import { COUPON_STATUSES, type CouponStatus } from '@/types/domain'

const PROMOTION_STATUSES: PromotionStatus[] = ['DRAFT', 'ACTIVE', 'ENDED']

describe('promotion lifecycle', () => {
  it('offers 上线 only on a DRAFT', () => {
    expect(canPublishPromotion('DRAFT')).toBe(true)
    expect(canPublishPromotion('ACTIVE')).toBe(false)
    expect(canPublishPromotion('ENDED')).toBe(false)
  })

  it('offers 下线 only on an ACTIVE promotion', () => {
    expect(canUnpublishPromotion('ACTIVE')).toBe(true)
    expect(canUnpublishPromotion('DRAFT')).toBe(false)
    expect(canUnpublishPromotion('ENDED')).toBe(false)
  })

  it('treats ENDED as terminal, offering neither action', () => {
    expect(isPromotionTerminal('ENDED')).toBe(true)
    expect(canPublishPromotion('ENDED') || canUnpublishPromotion('ENDED')).toBe(false)
  })

  it('exactly one action is available for each non-terminal state', () => {
    for (const status of PROMOTION_STATUSES) {
      if (isPromotionTerminal(status)) continue
      const offered = [canPublishPromotion(status), canUnpublishPromotion(status)].filter(Boolean)
      expect(offered, `${status} must offer exactly one transition`).toHaveLength(1)
    }
  })

  it('explains every blocked transition instead of hiding the rule', () => {
    for (const status of PROMOTION_STATUSES) {
      for (const action of ['publish', 'unpublish'] as const) {
        expect(promotionActionBlockedReason(action, status).length).toBeGreaterThan(0)
      }
    }
    expect(promotionActionBlockedReason('publish', 'ENDED')).toContain('结束')
  })
})

describe('coupon availability', () => {
  it('offers only UNUSED coupons as available', () => {
    expect(isCouponAvailable('UNUSED')).toBe(true)
  })

  it('never presents a LOCKED coupon as available — it is committed to another order', () => {
    // `COUPON_ALREADY_LOCKED` (90008) is what the server answers if the UI gets this wrong.
    expect(isCouponAvailable('LOCKED')).toBe(false)
  })

  it('never presents a USED or EXPIRED coupon as available', () => {
    expect(isCouponAvailable('USED')).toBe(false)
    expect(isCouponAvailable('EXPIRED')).toBe(false)
  })

  it('covers the whole frozen coupon vocabulary, so a new status cannot slip through', () => {
    for (const status of COUPON_STATUSES as readonly CouponStatus[]) {
      expect(isCouponAvailable(status), `unexpected availability for ${status}`).toBe(status === 'UNUSED')
    }
  })
})
