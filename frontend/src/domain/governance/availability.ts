/**
 * Pending action (HITL) decision availability (§101, §99).
 *
 * Only `PENDING` is decidable. The decision echoes back `payload_hash` — the hash the
 * approver actually SAW — and the server answers `PENDING_ACTION_PAYLOAD_CHANGED` (12 004)
 * if the payload moved underneath. That is a re-review, not a retry: the UI must re-render
 * the diff rather than resubmitting blindly.
 */

import type { PendingActionStatus, RiskLevel } from '@/types/domain'

export function canDecidePendingAction(status: PendingActionStatus): boolean {
  return status === 'PENDING'
}

/**
 * Risk levels that require explicit human approval.
 * NOTE: `READ` is the LOWEST level (read-only) — it is not a typo for "RED".
 */
export function riskRequiresApproval(level: RiskLevel): boolean {
  return level === 'HIGH' || level === 'CRITICAL'
}

export interface PendingActionFlags {
  approve: boolean
  reject: boolean
}

export function pendingActionFlags(action: { status: PendingActionStatus }): PendingActionFlags {
  const decidable = canDecidePendingAction(action.status)
  return { approve: decidable, reject: decidable }
}

/** Reason a decision is unavailable, for a tooltip. */
export function pendingActionBlockedReason(status: PendingActionStatus): string {
  switch (status) {
    case 'APPROVED':
      return '已批准，等待执行'
    case 'REJECTED':
      return '已拒绝'
    case 'EXECUTING':
      return '正在执行中'
    case 'SUCCEEDED':
      return '已执行成功'
    case 'FAILED':
      return '执行失败，请查看执行回执'
    case 'EXPIRED':
      return '审批已过期，需要重新发起'
    default:
      return '当前状态不允许审批'
  }
}
