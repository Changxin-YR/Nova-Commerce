/**
 * Governance module: the human-in-the-loop approval queue (§101).
 *
 * Approving is a real write. The approve/reject call echoes `payload_hash` — the
 * hash the approver actually saw. If the underlying payload changed between render
 * and decision the server answers `PENDING_ACTION_PAYLOAD_CHANGED` (12004) and the
 * UI must re-render the diff instead of retrying blindly.
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import type { DecidePendingActionRequest } from '@/types/api-contract'
import type { Paged, PendingAction } from '@/types/domain'
import type { PageQuery } from '@/types/api'

export interface PendingActionQuery extends PageQuery {
  status?: string
  risk_level?: string
}

export const governanceApi = {
  async pendingActions(query: PendingActionQuery = {}): Promise<Paged<PendingAction>> {
    return httpClient.get<Paged<PendingAction>>(API.governance.pendingActions, { params: query })
  },

  async pendingAction(actionId: string): Promise<PendingAction> {
    return httpClient.get<PendingAction>(API.governance.pendingAction(actionId))
  },

  async approve(actionId: string, payload: DecidePendingActionRequest): Promise<PendingAction> {
    return httpClient.post<PendingAction>(API.governance.approve(actionId), payload)
  },

  async reject(actionId: string, payload: DecidePendingActionRequest): Promise<PendingAction> {
    return httpClient.post<PendingAction>(API.governance.reject(actionId), payload)
  },
}
