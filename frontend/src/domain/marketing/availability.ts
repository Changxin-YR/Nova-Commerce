/**
 * Promotion action availability (§47, §99).
 *
 * WHY PURE
 *  A promotion's lifecycle (DRAFT -> ACTIVE -> ENDED) is exactly the kind of state the console can
 *  drift from: offering 下线 on a promotion that already ended produces a button whose only outcome
 *  is a server rejection. Keeping the rule here means the drift shows up as a red test.
 *
 * Server authority (§104): these predicates decide only what is OFFERED. The server re-validates and
 * answers `PROMOTION_CONFLICT` (90001), so a failure is reported rather than assumed impossible.
 *
 * NOTE: this is the frontend's reading of the promotion lifecycle. `API_CONTRACT.md` §10 does not
 * freeze the promotion SHAPE (the local `Promotion` interface in `src/api/marketing.ts` is an
 * assumption), so both the status vocabulary and these transitions are assumptions to be confirmed
 * when the backend marketing module lands. They are recorded in the migration report.
 */

export type PromotionStatus = 'DRAFT' | 'ACTIVE' | 'ENDED'

/** Only a DRAFT can go live. Re-publishing an ACTIVE promotion is a no-op the UI should not offer. */
export function canPublishPromotion(status: PromotionStatus): boolean {
  return status === 'DRAFT'
}

/** Only an ACTIVE promotion can be taken down. An ENDED one has nothing left to withdraw. */
export function canUnpublishPromotion(status: PromotionStatus): boolean {
  return status === 'ACTIVE'
}

/** ENDED is terminal: neither action is meaningful, so the row shows a hint, not a dead button. */
export function isPromotionTerminal(status: PromotionStatus): boolean {
  return status === 'ENDED'
}

export function promotionActionBlockedReason(
  action: 'publish' | 'unpublish',
  status: PromotionStatus,
): string {
  if (status === 'ENDED') return '活动已结束'
  if (action === 'publish') return status === 'ACTIVE' ? '活动已上线' : '仅草稿可上线'
  return status === 'DRAFT' ? '草稿尚未上线' : '仅进行中的活动可下线'
}

/**
 * A coupon committed to another order is LOCKED, and one already spent is USED. Neither may be
 * offered again — the server enforces this with `COUPON_ALREADY_LOCKED` (90008) and
 * `COUPON_ALREADY_USED` (90007).
 */
export function isCouponAvailable(status: string): boolean {
  return status === 'UNUSED'
}
