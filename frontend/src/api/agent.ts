/**
 * Agent module (§101).
 *
 * `chat` is the non-streaming fallback; the workspace uses the SSE stream from
 * `src/agent/sse.ts` so the user sees tool progress and approval gates live.
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import type {
  AgentRunQuery,
  ChatRequest,
  ChatResponse,
} from '@/types/api-contract'
import type { AgentRun, AgentThread, Paged } from '@/types/domain'

export interface AgentToolInfo {
  name: string
  description: string
  /** Highest risk level this tool can reach; drives the approval policy. */
  risk_level: string
  enabled: boolean
  /** Write tools always require approval at HIGH/CRITICAL (§101). */
  requires_approval: boolean
  input_schema?: Record<string, unknown>
}

export const agentApi = {
  async chat(payload: ChatRequest): Promise<ChatResponse> {
    return httpClient.post<ChatResponse>(API.agent.chat, payload, {
      idempotencyKey: payload.client_request_id,
    })
  },

  async threads(): Promise<AgentThread[]> {
    return httpClient.get<AgentThread[]>(API.agent.threads)
  },

  async runs(query: AgentRunQuery = {}): Promise<Paged<AgentRun>> {
    return httpClient.get<Paged<AgentRun>>(API.agent.runs, { params: query })
  },

  async run(runId: string): Promise<AgentRun> {
    return httpClient.get<AgentRun>(API.agent.run(runId))
  },

  /** Task endpoint (§99): cancelling a run is an intent, not a status patch. */
  async cancelRun(runId: string): Promise<AgentRun> {
    return httpClient.post<AgentRun>(API.agent.cancelRun(runId), {})
  },

  async tools(): Promise<AgentToolInfo[]> {
    return httpClient.get<AgentToolInfo[]>(API.agent.adminTools)
  },
}
