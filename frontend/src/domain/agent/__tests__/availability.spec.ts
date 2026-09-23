/**
 * Agent-run cancellation availability (§85, §99).
 *
 * This module exists because a FROZEN task endpoint (`POST /agent/runs/{run_id}/cancel`) had no UI
 * caller: the API function shipped in the scaffold and nothing ever invoked it. The tests below pin
 * the one rule that makes wiring it safe — a run is cancellable while it is still live, including
 * while it waits for a human approval, and never once it is terminal.
 */

import { describe, expect, it } from 'vitest'
import {
  canCancelAgentRun,
  isLiveRun,
  isTerminalRun,
  isWaitingApproval,
  runActionBlockedReason,
} from '@/domain/agent/availability'
import { AGENT_RUN_STATUSES, type AgentRunStatus } from '@/types/domain'

describe('canCancelAgentRun', () => {
  it('allows cancelling a RUNNING run', () => {
    expect(canCancelAgentRun('RUNNING')).toBe(true)
  })

  it('allows cancelling a run WAITING_APPROVAL — it is live and holding budget', () => {
    // The state a reviewer is most likely to want to abandon: the run is parked on a human.
    expect(canCancelAgentRun('WAITING_APPROVAL')).toBe(true)
  })

  it('REFUSES cancelling a terminal run', () => {
    for (const status of ['SUCCEEDED', 'FAILED', 'CANCELLED'] as AgentRunStatus[]) {
      expect(canCancelAgentRun(status), `must not cancel ${status}`).toBe(false)
    }
  })

  it('agrees with isTerminalRun for the whole frozen vocabulary', () => {
    // Every status is either live or terminal, never both and never neither — otherwise a status
    // could render with no explanation and no action.
    for (const status of AGENT_RUN_STATUSES as readonly AgentRunStatus[]) {
      expect(isLiveRun(status) !== isTerminalRun(status), `${status} must be exactly one`).toBe(true)
    }
  })
})

describe('§108 agent surface states', () => {
  it('WAITING_APPROVAL is the waiting state, and it is live', () => {
    expect(isWaitingApproval('WAITING_APPROVAL')).toBe(true)
    for (const status of ['RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED'] as AgentRunStatus[]) {
      expect(isWaitingApproval(status)).toBe(false)
    }
  })
})

describe('runActionBlockedReason — explains rather than hides', () => {
  it('never returns an empty explanation for any status', () => {
    for (const status of AGENT_RUN_STATUSES as readonly AgentRunStatus[]) {
      expect(runActionBlockedReason('cancel', status).length).toBeGreaterThan(0)
    }
  })

  it('distinguishes an already-cancelled run from a finished one', () => {
    expect(runActionBlockedReason('cancel', 'CANCELLED')).toContain('已取消')
    expect(runActionBlockedReason('cancel', 'FAILED')).toContain('已结束')
  })
})
