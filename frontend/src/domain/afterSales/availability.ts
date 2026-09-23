/**
 * After-sale action availability (§47–§52, §99).
 *
 * Same contract as the other availability modules: these predicates decide what the console
 * OFFERS; the backend re-validates every call and is authoritative.
 *
 * FACTS ENCODED
 *  - `AfterSaleStatus`: NONE | PROCESSING | PARTIAL_REFUNDED | REFUNDED
 *  - approve/reject are decisions on a PROCESSING request only; deciding twice conflicts
 *    (`AFTER_SALE_STATE_INVALID` 80 002).
 *  - the refund cap is `approved_amount` when the merchant approved something, otherwise
 *    `requested_amount`, minus whatever was already refunded.
 *  - the server independently enforces `REFUND_EXCEEDS_PAID_AMOUNT` (80 004),
 *    `REFUND_EXCEEDS_ITEM_AMOUNT` (80 005), `REFUND_AMOUNT_INVALID` (80 006) and
 *    `REFUND_ALREADY_COMPLETED` (80 007).
 */

import type { AfterSale, AfterSaleStatus } from '@/types/domain'

/** Remaining amount the server will still allow for this after-sale, in minor units. */
export function afterSaleRefundable(
  record: Pick<AfterSale, 'requested_amount' | 'approved_amount' | 'refunded_amount'>,
): number {
  const base = record.approved_amount > 0 ? record.approved_amount : record.requested_amount
  return Math.max(0, base - record.refunded_amount)
}

/** Approve and reject are decisions on a pending request only. */
export function canDecideAfterSale(status: AfterSaleStatus): boolean {
  return status === 'PROCESSING'
}

/**
 * A refund can be executed while money remains and the request is still open: PROCESSING may
 * be refunded for the first time, PARTIAL_REFUNDED may be refunded again up to the cap.
 * REFUNDED and NONE cannot.
 */
export function canRefundAfterSale(
  record: Pick<AfterSale, 'status' | 'requested_amount' | 'approved_amount' | 'refunded_amount'>,
): boolean {
  if (record.status !== 'PROCESSING' && record.status !== 'PARTIAL_REFUNDED') return false
  return afterSaleRefundable(record) > 0
}

export interface AfterSaleActionFlags {
  approve: boolean
  reject: boolean
  refund: boolean
  viewDetail: boolean
}

export function afterSaleActionFlags(
  record: Pick<AfterSale, 'status' | 'requested_amount' | 'approved_amount' | 'refunded_amount'>,
): AfterSaleActionFlags {
  return {
    approve: canDecideAfterSale(record.status),
    reject: canDecideAfterSale(record.status),
    refund: canRefundAfterSale(record),
    // Detail is always available: inspection is not a state transition.
    viewDetail: true,
  }
}

/** Reason a decision or refund is unavailable, for a tooltip rather than a hidden rule. */
export function afterSaleBlockedReason(status: AfterSaleStatus): string {
  switch (status) {
    case 'NONE':
      return '该订单没有售后申请'
    case 'REFUNDED':
      return '退款已完成'
    case 'PARTIAL_REFUNDED':
      return '可继续退款，但不能再次核准'
    default:
      return '当前售后单状态不允许该操作'
  }
}

/**
 * Validate an operator-entered refund before it becomes a request.
 * Amounts are integer minor units; the cap here is a UX convenience, not the enforcement.
 */
export function validateRefundAmount(
  record: Pick<AfterSale, 'requested_amount' | 'approved_amount' | 'refunded_amount'>,
  amountMinor: number,
): string | null {
  if (!Number.isInteger(amountMinor)) return '退款金额必须是整数分'
  if (amountMinor <= 0) return '退款金额必须大于 0'
  const cap = afterSaleRefundable(record)
  if (amountMinor > cap) return `超出可退上限（最多 ${cap} 分）`
  return null
}
