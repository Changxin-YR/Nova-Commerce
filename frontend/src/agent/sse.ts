/**
 * SSE consumer for the agent event stream (§101).
 *
 * Uses `fetch` + `ReadableStream` rather than the browser `EventSource`, because
 * `EventSource` cannot send an `Authorization` header or a POST body. Auth and
 * `X-Trace-Id` still come from the one HTTP client via `http.rawFetch` (§106).
 *
 * Cancellation is first-class: `AbortController` stops both the network read and
 * the retry, and the UI shows the Cancelled state (§108) — it must not look like a
 * failure.
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import { AGENT_EVENT_NAMES, isAgentEventName, parseAgentEvent, type AgentEvent } from '@/agent/events'
import { normalizeError } from '@/api/error'
import type { NormalizedApiError } from '@/types/api'

export interface AgentStreamOptions {
  threadId?: string
  message: string
  agentName: string
  knowledgeBaseId?: string
  clientRequestId: string
  onEvent: (event: AgentEvent) => void
  onError?: (error: NormalizedApiError) => void
  onClose?: () => void
  signal?: AbortSignal
}

export interface AgentStreamHandle {
  abort: () => void
  done: Promise<void>
}

/** A single parsed frame from the wire. */
interface SseFrame {
  event: string
  data: string
}

/**
 * Incremental SSE frame parser.
 *
 * Kept as a separate pure function so it can be unit-tested without a network:
 * the wire format is `field: value` lines, frames end with a blank line, and `:`
 * lines are comments (the servers send those as keep-alives).
 */
export function createSseParser(): (chunk: string) => SseFrame[] {
  let buffer = ''

  return (chunk: string): SseFrame[] => {
    buffer += chunk.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
    const frames: SseFrame[] = []

    let separator = buffer.indexOf('\n\n')
    while (separator !== -1) {
      const rawFrame = buffer.slice(0, separator)
      buffer = buffer.slice(separator + 2)

      let eventName = ''
      const dataLines: string[] = []
      for (const line of rawFrame.split('\n')) {
        if (!line || line.startsWith(':')) continue
        const colon = line.indexOf(':')
        const field = colon === -1 ? line : line.slice(0, colon)
        const rawValue = colon === -1 ? '' : line.slice(colon + 1)
        const value = rawValue.startsWith(' ') ? rawValue.slice(1) : rawValue
        if (field === 'event') eventName = value
        else if (field === 'data') dataLines.push(value)
      }

      if (dataLines.length > 0 || eventName) {
        frames.push({ event: eventName, data: dataLines.join('\n') })
      }
      separator = buffer.indexOf('\n\n')
    }

    return frames
  }
}

/** Decode one frame into an `AgentEvent`, or `null` when it must be ignored. */
export function decodeSseFrame(frame: SseFrame): AgentEvent | null {
  if (!frame.data) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(frame.data)
  } catch {
    return null
  }
  const payload =
    parsed && typeof parsed === 'object' && !('event' in (parsed as Record<string, unknown>))
      ? { ...(parsed as Record<string, unknown>), event: frame.event }
      : parsed
  return parseAgentEvent(payload)
}

/**
 * Open the stream and pump events until the server closes it, the run reaches a
 * terminal event, or the caller aborts.
 */
export function openAgentStream(options: AgentStreamOptions): AgentStreamHandle {
  const controller = new AbortController()
  const externalSignal = options.signal
  const abort = (): void => controller.abort()

  if (externalSignal) {
    if (externalSignal.aborted) controller.abort()
    else externalSignal.addEventListener('abort', abort, { once: true })
  }

  const done = (async () => {
    try {
      const response = await httpClient.rawFetch(API.agent.chatStream, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Accept: 'text/event-stream',
          'Cache-Control': 'no-cache',
        },
        body: JSON.stringify({
          thread_id: options.threadId,
          message: options.message,
          agent_name: options.agentName,
          knowledge_base_id: options.knowledgeBaseId,
          client_request_id: options.clientRequestId,
        }),
        signal: controller.signal,
      })

      if (!response.ok || !response.body) {
        const businessCode = Number(response.headers.get('X-Business-Code') ?? NaN)
        options.onError?.({
          code: Number.isFinite(businessCode) ? businessCode : -4,
          message: '智能体服务暂时不可用，请稍后重试',
          traceId: response.headers.get('X-Trace-Id') ?? '',
          httpStatus: response.status,
          retryable: response.status >= 500,
          forbidden: response.status === 403,
          unauthenticated: response.status === 401,
        })
        return
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder('utf-8')
      const parse = createSseParser()
      let terminal = false

      while (!terminal) {
        const { done: finished, value } = await reader.read()
        if (finished) break
        const frames = parse(decoder.decode(value, { stream: true }))
        for (const frame of frames) {
          if (frame.event && !isAgentEventName(frame.event)) {
            // A keep-alive or a server-side comment frame: ignore it silently.
            continue
          }
          const event = decodeSseFrame(frame)
          if (!event) continue
          options.onEvent(event)
          if (event.event === 'final_answer' || event.event === 'run_failed') {
            terminal = true
            break
          }
        }
      }
    } catch (error) {
      const normalized = normalizeError(error)
      if (normalized.code !== -3) {
        options.onError?.(normalized)
      }
    } finally {
      options.onClose?.()
    }
  })()

  return { abort, done }
}

/** Exposed for tests and for a future `/agent/events` replay endpoint. */
export const AGENT_EVENT_NAMES_FROZEN = AGENT_EVENT_NAMES
