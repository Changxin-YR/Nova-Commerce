/**
 * Trace-id propagation (§131).
 *
 * Every outgoing request carries `X-Trace-Id`. The backend echoes it in both the
 * response header and the response envelope, and the value is stored so error
 * reports and support screens can quote it.
 *
 * The generator prefers `crypto.randomUUID()` (available in every browser this app
 * targets and in Node >= 19) and degrades to a defensible fallback so an ancient
 * or locked-down environment cannot break every request.
 */

import { STORAGE_KEYS, TRACE_ID_HEADER } from '@/config/constants'

export { TRACE_ID_HEADER }

let lastTraceId = ''

/** 16 hex chars, matching the backend's own trace-id shape. */
export function newTraceId(): string {
  const c = globalThis.crypto as Crypto | undefined
  if (c && typeof c.randomUUID === 'function') {
    return c.randomUUID().replace(/-/g, '').slice(0, 16)
  }
  let out = ''
  for (let i = 0; i < 16; i += 1) {
    out += Math.floor(Math.random() * 16).toString(16)
  }
  return out
}

/** Remember the trace id of the most recent response for error reporting. */
export function setLastTraceId(traceId: string | undefined | null): void {
  if (!traceId) return
  lastTraceId = traceId
  try {
    globalThis.localStorage?.setItem(STORAGE_KEYS.traceId, traceId)
  } catch {
    // Storage can be unavailable (private mode, quota). Trace ids are a
    // diagnostic convenience, never a correctness requirement.
  }
}

export function getLastTraceId(): string {
  if (lastTraceId) return lastTraceId
  try {
    lastTraceId = globalThis.localStorage?.getItem(STORAGE_KEYS.traceId) ?? ''
  } catch {
    lastTraceId = ''
  }
  return lastTraceId
}

/** Normalize whatever a header accessor returns into a single string. */
export function readTraceIdHeader(headers: unknown): string {
  if (!headers || typeof headers !== 'object') return ''
  const raw = (headers as Record<string, unknown>)[TRACE_ID_HEADER]
    ?? (headers as Record<string, unknown>)[TRACE_ID_HEADER.toLowerCase()]
  if (typeof raw === 'string') return raw
  if (Array.isArray(raw) && typeof raw[0] === 'string') return raw[0]
  return ''
}
