/**
 * AI thread store (§105, §101) — one of the only six allowed Pinia stores.
 *
 * Holds the conversation, the live run status and the streamed blocks. It is
 * deliberately the ONLY place that mutates message state while an SSE run is in
 * flight, so a late event from a cancelled run cannot resurrect a stale message.
 *
 * SECURITY NOTES
 *  - Only the 12 frozen event names are handled; an unknown event is ignored.
 *  - A `chart_spec` on `final_answer` is validated by `sanitizeChartSpec` before it
 *    ever reaches a renderer. Nothing from the stream is ever evaluated as code.
 *  - No chain-of-thought is requested, received or displayed (§132).
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { openAgentStream, type AgentStreamHandle } from '@/agent/sse'
import type { AgentEvent } from '@/agent/events'
import type { AgentName } from '@/config/app'
import { sanitizeChartSpec } from '@/charts/sanitizeChartSpec'
import type { NormalizedApiError } from '@/types/api'
import type { Citation, PendingAction, RiskLevel, ToolProgress } from '@/types/domain'
import type { ChartSpec } from '@/types/charts'

export type AgentBlockKind =
  | 'text'
  | 'metric'
  | 'table'
  | 'chart'
  | 'citation'
  | 'tool_progress'
  | 'action_proposal'
  | 'error'

export interface AgentMessageBlock {
  id: string
  kind: AgentBlockKind
  /** Text payload for `text` / `error`. */
  text?: string
  /** Optional label rendered above the block (metric name, tool name, ...). */
  label?: string
  /** Metric block. */
  metric?: { value: string; unit?: string; trend?: 'up' | 'down' | 'flat' }
  /** Table block. */
  table?: { columns: { key: string; title: string; align?: 'left' | 'center' | 'right' }[]; rows: Record<string, string | number | null>[] }
  /** Chart block: a VALIDATED spec, never raw agent output. */
  chart?: ChartSpec
  /** Set when the agent sent a chart the validator rejected. */
  chartError?: string
  /** Citation block. */
  citations?: Citation[]
  /** ToolProgress block. */
  tool?: ToolProgress
  /** ActionProposal block — rendered as an approval card, never auto-executed. */
  action?: {
    pendingActionId: string
    actionType: string
    toolName: string
    summary: string
    riskLevel: RiskLevel
    payloadHash: string
    expiresAt: string
    requiresApproval: boolean
    status: string
  }
}

export interface AgentMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  createdAt: string
  blocks: AgentMessageBlock[]
  runId?: string
}

/** Live run status; maps directly onto the §108 agent states. */
export type RunStatus =
  | 'idle'
  | 'run_started'
  | 'streaming'
  | 'waiting_approval'
  | 'succeeded'
  | 'failed'
  | 'cancelled'

let blockSeq = 0
function nextBlockId(prefix = 'blk'): string {
  blockSeq += 1
  return `${prefix}-${Date.now().toString(36)}-${blockSeq}`
}

export const useAiThreadStore = defineStore('aiThread', () => {
  const threadId = ref<string>('')
  const agentName = ref<AgentName>('assistant')
  const messages = ref<AgentMessage[]>([])
  const status = ref<RunStatus>('idle')
  const activeRunId = ref<string>('')
  const routeInfo = ref<{ route: string; reason: string } | null>(null)
  const plan = ref<{ step_id: string; title: string; status: string; tool_name?: string }[]>([])
  const toolCalls = ref<ToolProgress[]>([])
  const citations = ref<Citation[]>([])
  const pendingAction = ref<PendingAction | null>(null)
  const error = ref<NormalizedApiError | null>(null)
  /** Last event sequence applied; out-of-order re-deliveries are dropped. */
  const lastSeq = ref(0)

  let handle: AgentStreamHandle | null = null

  const isStreaming = computed(() => status.value === 'streaming' || status.value === 'run_started')
  const isWaitingApproval = computed(() => status.value === 'waiting_approval')
  const isRunning = computed(
    () => isStreaming.value || isWaitingApproval.value,
  )
  const lastAssistantMessage = computed(() => {
    for (let i = messages.value.length - 1; i >= 0; i -= 1) {
      const message = messages.value[i]
      if (message?.role === 'assistant') return message
    }
    return null
  })

  function currentAssistantMessage(): AgentMessage | null {
    return lastAssistantMessage.value
  }

  function pushUserMessage(text: string): void {
    messages.value.push({
      id: nextBlockId('msg'),
      role: 'user',
      createdAt: new Date().toISOString(),
      blocks: [{ id: nextBlockId(), kind: 'text', text }],
    })
  }

  function startAssistantMessage(runId: string): AgentMessage {
    const message: AgentMessage = {
      id: nextBlockId('msg'),
      role: 'assistant',
      createdAt: new Date().toISOString(),
      blocks: [],
      runId,
    }
    messages.value.push(message)
    return message
  }

  /** Append a block to the live assistant message, creating it if needed. */
  function appendBlock(block: AgentMessageBlock, runId = activeRunId.value): void {
    const message = currentAssistantMessage() ?? startAssistantMessage(runId)
    message.blocks.push(block)
  }

  /**
   * Apply ONE SSE event. This is the single mutation point for streamed state.
   * Unknown events are ignored, and out-of-order events (seq <= lastSeq) are
   * dropped rather than corrupting the message.
   */
  function applyEvent(event: AgentEvent): void {
    if (typeof event.seq === 'number' && event.seq > 0) {
      if (event.seq <= lastSeq.value) return
      lastSeq.value = event.seq
    }

    switch (event.event) {
      case 'run_started': {
        activeRunId.value = event.run_id
        threadId.value = event.thread_id
        status.value = 'run_started'
        routeInfo.value = null
        plan.value = []
        toolCalls.value = []
        citations.value = []
        pendingAction.value = null
        error.value = null
        startAssistantMessage(event.run_id)
        break
      }

      case 'route_selected': {
        routeInfo.value = { route: event.route, reason: event.reason }
        break
      }

      case 'plan_created': {
        plan.value = event.steps.map((step) => ({ ...step }))
        break
      }

      case 'tool_started': {
        status.value = 'streaming'
        const progress: ToolProgress = {
          tool_call_id: event.tool_call_id,
          tool_name: event.tool_name,
          status: 'running',
          started_at: event.at,
        }
        toolCalls.value.push(progress)
        appendBlock({
          id: nextBlockId('tool'),
          kind: 'tool_progress',
          label: event.tool_name,
          tool: progress,
        })
        break
      }

      case 'tool_completed': {
        const index = toolCalls.value.findIndex((t) => t.tool_call_id === event.tool_call_id)
        if (index !== -1) {
          const existing = toolCalls.value[index]
          if (existing) {
            toolCalls.value[index] = {
              ...existing,
              status: event.status,
              summary: event.summary,
              finished_at: event.at,
              duration_ms: event.duration_ms,
            }
          }
        }
        const message = currentAssistantMessage()
        const block = message?.blocks.find(
          (b) => b.kind === 'tool_progress' && b.tool?.tool_call_id === event.tool_call_id,
        )
        if (block?.tool) {
          block.tool = {
            ...block.tool,
            status: event.status,
            summary: event.summary,
            finished_at: event.at,
            duration_ms: event.duration_ms,
          }
        }
        break
      }

      case 'evidence_ready': {
        citations.value = event.citations ?? []
        if (citations.value.length > 0) {
          appendBlock({
            id: nextBlockId('cite'),
            kind: 'citation',
            label: '引用证据',
            citations: citations.value,
          })
        }
        break
      }

      case 'action_pending': {
        status.value = 'waiting_approval'
        pendingAction.value = {
          id: event.pending_action_id,
          agent_run_id: event.run_id,
          action_type: event.action_type,
          tool_name: event.tool_name,
          summary: event.summary,
          risk_level: event.risk_level,
          status: 'PENDING',
          payload: {},
          payload_hash: event.payload_hash,
          requested_by: 'agent',
          expires_at: event.expires_at,
          created_at: event.at,
        }
        appendBlock({
          id: nextBlockId('action'),
          kind: 'action_proposal',
          label: event.tool_name,
          action: {
            pendingActionId: event.pending_action_id,
            actionType: event.action_type,
            toolName: event.tool_name,
            summary: event.summary,
            riskLevel: event.risk_level,
            payloadHash: event.payload_hash,
            expiresAt: event.expires_at,
            requiresApproval: event.requires_approval,
            status: 'PENDING',
          },
        })
        break
      }

      case 'action_approved': {
        status.value = event.approved ? 'streaming' : 'waiting_approval'
        if (pendingAction.value) {
          pendingAction.value = {
            ...pendingAction.value,
            status: event.approved ? 'APPROVED' : 'REJECTED',
            decided_by: event.approved_by,
            decision_reason: event.decision_reason,
          }
        }
        updateActionBlock(event.pending_action_id, event.approved ? 'APPROVED' : 'REJECTED')
        break
      }

      case 'action_executing': {
        status.value = 'streaming'
        updateActionBlock(event.pending_action_id, 'EXECUTING')
        break
      }

      case 'action_completed': {
        updateActionBlock(event.pending_action_id, event.status)
        if (event.summary) {
          appendBlock({
            id: nextBlockId('exec'),
            kind: 'text',
            text: `执行结果：${event.summary}`,
          })
        }
        break
      }

      case 'final_answer': {
        status.value = 'succeeded'
        // A chart from the agent is UNTRUSTED data: validate before rendering.
        if (event.chart_spec !== undefined) {
          const result = sanitizeChartSpec(event.chart_spec)
          if (result.ok) {
            appendBlock({
              id: nextBlockId('chart'),
              kind: 'chart',
              chart: result.spec,
              label: result.spec.title,
            })
          } else {
            appendBlock({
              id: nextBlockId('charterr'),
              kind: 'error',
              text: `图表数据不符合规范，已拒绝渲染：${result.reason}`,
              chartError: result.reason,
            })
          }
        }
        if (event.answer) {
          appendBlock({ id: nextBlockId('ans'), kind: 'text', text: event.answer })
        }
        break
      }

      case 'run_failed': {
        status.value = 'failed'
        error.value = {
          code: event.error_code,
          message: event.message,
          traceId: '',
          httpStatus: 0,
          retryable: Boolean(event.retryable),
          forbidden: event.error_code === 20_008 || event.error_code === 20_009,
          unauthenticated: event.error_code === 20_000,
        }
        appendBlock({
          id: nextBlockId('err'),
          kind: 'error',
          text: event.message,
          label: `错误码 ${event.error_code}`,
        })
        break
      }

      default: {
        // Exhaustive over the 12 frozen names; nothing else can reach here.
        break
      }
    }
  }

  function updateActionBlock(pendingActionId: string, status: string): void {
    for (const message of messages.value) {
      for (const block of message.blocks) {
        if (block.kind === 'action_proposal' && block.action?.pendingActionId === pendingActionId) {
          block.action = { ...block.action, status }
        }
      }
    }
  }

  /** Open the SSE stream for a new turn. */
  function send(text: string, runAgentName?: AgentName): void {
    if (isRunning.value) return
    const agent = runAgentName ?? agentName.value
    agentName.value = agent
    pushUserMessage(text)
    status.value = 'streaming'
    lastSeq.value = 0

    handle = openAgentStream({
      threadId: threadId.value || undefined,
      message: text,
      agentName: agent,
      clientRequestId: `chat-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
      onEvent: (event) => applyEvent(event),
      onError: (normalized) => {
        status.value = 'failed'
        error.value = normalized
        appendBlock({
          id: nextBlockId('err'),
          kind: 'error',
          text: normalized.message,
          label: normalized.traceId ? `trace ${normalized.traceId}` : undefined,
        })
      },
      onClose: () => {
        // A closed stream without a terminal event while still "streaming" means
        // the connection dropped: surface it instead of spinning forever.
        if (status.value === 'streaming' || status.value === 'run_started') {
          status.value = 'failed'
          appendBlock({
            id: nextBlockId('err'),
            kind: 'error',
            text: '连接已中断，请重试或查看 Agent Runs 获取完整记录。',
          })
        }
        handle = null
      },
    })
  }

  /** User-initiated cancel -> the §108 `Cancelled` state, NOT a failure. */
  function cancel(): void {
    handle?.abort()
    handle = null
    if (isRunning.value) {
      status.value = 'cancelled'
      appendBlock({ id: nextBlockId('cancel'), kind: 'text', text: '本次运行已被取消。' })
    }
  }

  /** Called after an approval decision so the stream can continue. */
  function markApprovalDecided(approved: boolean): void {
    if (!pendingAction.value) return
    updateActionBlock(pendingAction.value.id, approved ? 'APPROVED' : 'REJECTED')
    status.value = approved ? 'streaming' : 'waiting_approval'
  }

  function selectThread(id: string): void {
    cancel()
    threadId.value = id
    messages.value = []
    status.value = 'idle'
    lastSeq.value = 0
    pendingAction.value = null
    toolCalls.value = []
    citations.value = []
  }

  function reset(): void {
    cancel()
    threadId.value = ''
    messages.value = []
    status.value = 'idle'
    lastSeq.value = 0
    routeInfo.value = null
    plan.value = []
    toolCalls.value = []
    citations.value = []
    pendingAction.value = null
    error.value = null
  }

  return {
    threadId,
    agentName,
    messages,
    status,
    activeRunId,
    routeInfo,
    plan,
    toolCalls,
    citations,
    pendingAction,
    error,
    lastSeq,
    isStreaming,
    isWaitingApproval,
    isRunning,
    lastAssistantMessage,
    applyEvent,
    send,
    cancel,
    markApprovalDecided,
    selectThread,
    reset,
  }
})
