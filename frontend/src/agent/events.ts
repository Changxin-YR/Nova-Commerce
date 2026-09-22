/**
 * The agent wire protocol (§101, §131).
 *
 * FROZEN: exactly these 12 event names exist. There is NO hidden chain-of-thought
 * event, and the UI must not try to display one. If the backend ever grows an
 * internal-reasoning channel it must NOT be rendered here — surfacing it would
 * violate §132 (logs and payloads never contain hidden reasoning).
 *
 * `tool_completed` carries a SAFE SUMMARY, not the tool's raw internals.
 */

import type { Citation, RiskLevel, ToolProgress } from '@/types/domain'
import type { ChartSpec } from '@/types/charts'

export const AGENT_EVENT_NAMES = [
  'run_started',
  'route_selected',
  'plan_created',
  'tool_started',
  'tool_completed',
  'evidence_ready',
  'action_pending',
  'action_approved',
  'action_executing',
  'action_completed',
  'final_answer',
  'run_failed',
] as const

export type AgentEventName = (typeof AGENT_EVENT_NAMES)[number]

export function isAgentEventName(value: string): value is AgentEventName {
  return (AGENT_EVENT_NAMES as readonly string[]).includes(value)
}

interface BaseEvent {
  event: AgentEventName
  run_id: string
  thread_id: string
  /** Monotonic per run; used to drop out-of-order re-deliveries. */
  seq: number
  at: string
}

export interface RunStartedEvent extends BaseEvent {
  event: 'run_started'
  agent_name: string
  query: string
}

export interface RouteSelectedEvent extends BaseEvent {
  event: 'route_selected'
  /** Which graph entry point the router chose, and why (a label, not reasoning). */
  route: string
  reason: string
  candidates?: string[]
}

export interface PlanStep {
  step_id: string
  title: string
  tool_name?: string
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'skipped'
}

export interface PlanCreatedEvent extends BaseEvent {
  event: 'plan_created'
  steps: PlanStep[]
}

export interface ToolStartedEvent extends BaseEvent {
  event: 'tool_started'
  tool_call_id: string
  tool_name: string
  /** Already-redacted display inputs. */
  arguments_summary?: string
}

export interface ToolCompletedEvent extends BaseEvent {
  event: 'tool_completed'
  tool_call_id: string
  tool_name: string
  status: 'succeeded' | 'failed' | 'skipped'
  summary?: string
  duration_ms?: number
}

export interface EvidenceReadyEvent extends BaseEvent {
  event: 'evidence_ready'
  citations: Citation[]
  /** True when retrieval found too little to answer (§100010). */
  insufficient?: boolean
}

export interface ActionPendingEvent extends BaseEvent {
  event: 'action_pending'
  pending_action_id: string
  action_type: string
  tool_name: string
  summary: string
  risk_level: RiskLevel
  payload_hash: string
  expires_at: string
  requires_approval: boolean
}

export interface ActionApprovedEvent extends BaseEvent {
  event: 'action_approved'
  pending_action_id: string
  approved_by: string
  approved: boolean
  decision_reason?: string
}

export interface ActionExecutingEvent extends BaseEvent {
  event: 'action_executing'
  pending_action_id: string
}

export interface ActionCompletedEvent extends BaseEvent {
  event: 'action_completed'
  pending_action_id: string
  status: 'SUCCEEDED' | 'FAILED'
  summary?: string
  receipt?: Record<string, unknown>
  error_code?: number
}

export interface FinalAnswerEvent extends BaseEvent {
  event: 'final_answer'
  answer: string
  citations: Citation[]
  /** Optional declarative chart. NEVER executable code (§102). */
  chart_spec?: ChartSpec
  pending_action_id?: string
  tokens_used?: number
}

export interface RunFailedEvent extends BaseEvent {
  event: 'run_failed'
  error_code: number
  message: string
  retryable?: boolean
}

export type AgentEvent =
  | RunStartedEvent
  | RouteSelectedEvent
  | PlanCreatedEvent
  | ToolStartedEvent
  | ToolCompletedEvent
  | EvidenceReadyEvent
  | ActionPendingEvent
  | ActionApprovedEvent
  | ActionExecutingEvent
  | ActionCompletedEvent
  | FinalAnswerEvent
  | RunFailedEvent

/** Events that end a run. The stream is also closed by the server after these. */
export const TERMINAL_EVENT_NAMES: readonly AgentEventName[] = ['final_answer', 'run_failed']

/**
 * Narrow an unknown parsed payload to an `AgentEvent`, or `null`.
 * Unknown names and malformed payloads are DROPPED (never rendered) so that a
 * future server-side event cannot silently break the message renderer.
 */
export function parseAgentEvent(raw: unknown): AgentEvent | null {
  if (!raw || typeof raw !== 'object') return null
  const value = raw as Record<string, unknown>
  const name = value.event
  if (typeof name !== 'string' || !isAgentEventName(name)) return null
  if (typeof value.run_id !== 'string') return null
  if (value.chart_spec !== undefined && name !== 'final_answer') return null
  return value as unknown as AgentEvent
}

/** Convenience: cast a `ToolProgress`-shaped payload out of a tool event. */
export function toToolProgress(
  event: ToolStartedEvent | ToolCompletedEvent,
): Pick<ToolProgress, 'tool_call_id' | 'tool_name'> {
  return { tool_call_id: event.tool_call_id, tool_name: event.tool_name }
}
