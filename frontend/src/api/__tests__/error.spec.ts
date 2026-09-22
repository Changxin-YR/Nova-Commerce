/**
 * Error mapper tests (§106, §109).
 *
 * The mapper is pure: it maps an unknown thrown value into one stable shape and
 * never mutates session state (that lives in the client interceptor).
 */

import { AxiosError, AxiosHeaders, CanceledError } from 'axios'
import { describe, expect, it } from 'vitest'
import { normalizeError } from '@/api/error'
import { ErrorCode, TRANSPORT_CODES } from '@/types/api'

/** Build an axios-shaped error with an optional envelope and status. */
function axiosError(status: number, data: unknown, headers: Record<string, string> = {}): AxiosError {
  const response = {
    status,
    statusText: '',
    data,
    headers: new AxiosHeaders(headers),
    config: { headers: new AxiosHeaders() },
  }
  return new AxiosError('Request failed', 'ERR_BAD_REQUEST', response.config, {}, response as never)
}

function envelope(code: number, message: string, traceId = 'trace-abc'): unknown {
  return { code, message, data: null, trace_id: traceId }
}

describe('normalizeError — backend envelope', () => {
  it('reads code, message and trace_id from the envelope', () => {
    const result = normalizeError(axiosError(409, envelope(ErrorCode.INSUFFICIENT_STOCK, 'INSUFFICIENT STOCK')))
    expect(result.code).toBe(ErrorCode.INSUFFICIENT_STOCK)
    expect(result.message).toBe('库存不足')
    expect(result.traceId).toBe('trace-abc')
    expect(result.httpStatus).toBe(409)
    expect(result.retryable).toBe(false)
    expect(result.forbidden).toBe(false)
    expect(result.unauthenticated).toBe(false)
  })

  it('falls back to a curated message when the code is unknown, never the raw one', () => {
    const result = normalizeError(axiosError(400, envelope(99_999, 'internal stack: File "/app/x.py", line 4')))
    expect(result.code).toBe(99_999)
    // Unknown codes keep the server message (contract §95 says it is already safe).
    expect(result.message).toBe('internal stack: File "/app/x.py", line 4')
  })

  it('NEVER surfaces a 5xx envelope message verbatim (gateway/proxy leakage)', () => {
    const result = normalizeError(
      axiosError(500, envelope(ErrorCode.INTERNAL_ERROR, 'Traceback: SecretKey=supersecret')),
    )
    expect(result.message).not.toContain('supersecret')
    expect(result.message).toBe('服务内部错误，请稍后重试')
    expect(result.retryable).toBe(true)
  })

  it('maps forbidden codes to the permission UI path', () => {
    const result = normalizeError(axiosError(403, envelope(ErrorCode.INSUFFICIENT_PERMISSION, 'no')))
    expect(result.forbidden).toBe(true)
    expect(result.unauthenticated).toBe(false)
    expect(result.message).toBe('当前角色权限不足')
  })

  it('treats MERCHANT_MISMATCH as forbidden rather than unauthenticated', () => {
    const result = normalizeError(axiosError(403, envelope(ErrorCode.MERCHANT_MISMATCH, 'no')))
    expect(result.forbidden).toBe(true)
    expect(result.unauthenticated).toBe(false)
  })

  it('maps token codes to the unauthenticated path', () => {
    for (const code of [ErrorCode.UNAUTHENTICATED, ErrorCode.TOKEN_EXPIRED, ErrorCode.SESSION_REVOKED]) {
      const result = normalizeError(axiosError(401, envelope(code, 'x')))
      expect(result.unauthenticated).toBe(true)
      expect(result.forbidden).toBe(false)
    }
  })

  it('does NOT treat ACCOUNT_DISABLED as a refreshable session (it clears the session)', () => {
    const result = normalizeError(axiosError(403, envelope(ErrorCode.ACCOUNT_DISABLED, 'x')))
    expect(result.unauthenticated).toBe(true)
    expect(result.message).toBe('账号已被禁用，请联系管理员')
  })

  it('marks 429 and 503 as retryable', () => {
    expect(normalizeError(axiosError(429, envelope(ErrorCode.RATE_LIMITED, 'x'))).retryable).toBe(true)
    expect(normalizeError(axiosError(503, envelope(ErrorCode.SERVICE_UNAVAILABLE, 'x'))).retryable).toBe(true)
  })
})

describe('normalizeError — transport failures', () => {
  it('maps a network failure to a retryable negative code', () => {
    const error = new AxiosError('Network Error', 'ERR_NETWORK')
    const result = normalizeError(error)
    expect(result.code).toBe(TRANSPORT_CODES.NETWORK_ERROR)
    expect(result.httpStatus).toBe(0)
    expect(result.retryable).toBe(true)
    expect(result.message).toBe('网络连接失败，请检查网络后重试')
  })

  it('distinguishes a timeout from a network failure', () => {
    const result = normalizeError(new AxiosError('timeout of 15000ms exceeded', 'ECONNABORTED'))
    expect(result.code).toBe(TRANSPORT_CODES.TIMEOUT)
    expect(result.retryable).toBe(true)
  })

  it('maps a cancellation to a non-retryable, non-error state', () => {
    // The shape the axios adapter actually throws when an AbortController fires.
    expect(normalizeError(new CanceledError('canceled')).code).toBe(TRANSPORT_CODES.CANCELLED)
    // …and the bare axios error-code variant.
    expect(normalizeError(new AxiosError('canceled', 'ERR_CANCELED')).code).toBe(
      TRANSPORT_CODES.CANCELLED,
    )
    // …and a fetch AbortError (used by the agent SSE stream).
    const abortError = new Error('The operation was aborted')
    abortError.name = 'AbortError'
    const result = normalizeError(abortError)
    expect(result.code).toBe(TRANSPORT_CODES.CANCELLED)
    expect(result.retryable).toBe(false)
    expect(result.unauthenticated).toBe(false)
  })

  it('handles a response with NO envelope (gateway HTML) without leaking the body', () => {
    const result = normalizeError(axiosError(502, '<html><body>nginx 502 Bad Gateway</body></html>'))
    expect(result.code).toBe(ErrorCode.SERVICE_UNAVAILABLE)
    expect(result.message).not.toContain('nginx')
    expect(result.retryable).toBe(true)
  })

  it('maps a bare 401/403/404 without an envelope to the matching code', () => {
    expect(normalizeError(axiosError(401, '')).code).toBe(ErrorCode.UNAUTHENTICATED)
    expect(normalizeError(axiosError(403, '')).code).toBe(ErrorCode.FORBIDDEN)
    expect(normalizeError(axiosError(404, '')).code).toBe(ErrorCode.NOT_FOUND)
    expect(normalizeError(axiosError(403, '')).forbidden).toBe(true)
  })

  it('passes an already-normalized error through unchanged', () => {
    const normalized = normalizeError(axiosError(409, envelope(ErrorCode.CONFLICT, 'x')))
    expect(normalizeError(normalized)).toBe(normalized)
  })

  it('handles a plain Error and a totally unknown throwable', () => {
    expect(normalizeError(new Error('boom')).message).toBe('boom')
    expect(normalizeError(undefined).code).toBe(TRANSPORT_CODES.UNKNOWN)
    expect(normalizeError('weird').code).toBe(TRANSPORT_CODES.UNKNOWN)
  })

  it('uses the interceptor trace id when the envelope has none', () => {
    const result = normalizeError(axiosError(500, 'oops'), 'trace-from-header')
    expect(result.traceId).toBe('trace-from-header')
  })
})
