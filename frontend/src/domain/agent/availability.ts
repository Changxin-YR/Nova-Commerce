/**
 * Agent-run action availability (§85, §99, §108).
 *
 * WHY THIS EXISTS
 *  `API_CONTRACT.md` §4 freezes `POST /api/v1/agent/runs/{run_id}/cancel`, and `agentApi.cancelRun`
 *  has existed since the scaffold — but nothing called it, so a frozen task endpoint had no entry
 *  point at all. This module is the rule that makes wiring it safe rather than a button that
 *  sometimes fails.
 *
 * THE RULE: a run can be cancelled while it is still LIVE — `RUNNING`, or `WAITING_APPROVAL`. A run
 *  parked on a human approval is still an open run holding budget and a graph position, so the
 *  operator must be able to withdraw it; that is precisely the state a reviewer is most likely to
 *  want to abandon.
 *
 * `SUCCEEDED`, `FAILED` and `CANCELLED` are terminal: offering 取消 there would produce a button whose
 * only outcome is a server rejection (`AGENT_RUN_NOT_FOUND` 110000 or a state conflict).
 *
 * §104: this decides only what is OFFERED. The server re-validates, and the view reports a rejection
 * rather than assuming it impossible.
 */

import type { AgentRunStatus } from '@/types/domain'

/** A live run — still occupying the graph and its budget. */
export function isLiveRun(status: AgentRunStatus): boolean {
  return status === 'RUNNING' || status === 'WAITING_APPROVAL'
}

export function canCancelAgentRun(status: AgentRunStatus): boolean {
  return isLiveRun(status)
}

/** Terminal states: the run has stopped and its outcome is recorded. */
export function isTerminalRun(status: AgentRunStatus): boolean {
  return status === 'SUCCEEDED' || status === 'FAILED' || status === 'CANCELLED'
}

/** A run waiting on a human is the state the §108 WaitingApproval surface renders. */
export function isWaitingApproval(status: AgentRunStatus): boolean {
  return status === 'WAITING_APPROVAL'
}

export function runActionBlockedReason(action: 'cancel', status: AgentRunStatus): string {
  if (isLiveRun(status)) return '可取消'
  if (status === 'CANCELLED') return '该运行已取消'
  return action === 'cancel' ? '运行已结束，无法取消' : '当前状态不允许该操作'
}
