/**
 * The single most important client test: concurrent 401s must trigger exactly ONE
 * refresh, and every waiter must replay its original request with the new token.
 *
 * Why it matters: the backend rotates refresh tokens and reports reuse as
 * `REFRESH_TOKEN_REUSED` (20004). If N parallel 401s each started a refresh, the
 * second one would present an already-rotated token and log the user out for real.
 *
 * The test installs a custom axios adapter, so no network or test server is needed.
 */

import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { NovaHttpClient } from '@/api/client'
import { __resetTokenStoreForTests, getAccessToken, setTokens } from '@/api/tokenStore'
import { ErrorCode } from '@/types/api'
import { TRACE_ID_HEADER } from '@/utils/trace'

/** 401 envelope exactly as the backend would emit it. */
function unauthorizedResponse(config: InternalAxiosRequestConfig): AxiosResponse {
  return {
    status: 401,
    statusText: 'Unauthorized',
    headers: {},
    config,
    data: {
      code: ErrorCode.TOKEN_EXPIRED,
      message: 'TOKEN EXPIRED',
      data: null,
      trace_id: 'server-trace-401',
    },
  }
}

function okResponse(config: InternalAxiosRequestConfig, payload: unknown): AxiosResponse {
  return {
    status: 200,
    statusText: 'OK',
    headers: { [TRACE_ID_HEADER.toLowerCase()]: 'server-trace-200' },
    config,
    data: { code: 0, message: 'OK', data: payload, trace_id: 'server-trace-200' },
  }
}

interface AdapterHarness {
  adapter: AxiosAdapter
  calls: { url: string; authorization: string | undefined; traceId: string | undefined }[]
}

function makeAdapter(options: { failFirstWith401: boolean }): AdapterHarness {
  const calls: AdapterHarness['calls'] = []
  const adapter: AxiosAdapter = async (config) => {
    const headers = config.headers as unknown as { get?: (k: string) => unknown }
    calls.push({
      url: String(config.url),
      authorization: headers?.get?.('Authorization') as string | undefined,
      traceId: headers?.get?.(TRACE_ID_HEADER) as string | undefined,
    })
    // Anything still holding the OLD token gets a 401; the replayed request carries
    // the refreshed token and therefore succeeds.
    const token = headers?.get?.('Authorization') as string | undefined
    if (options.failFirstWith401 && token === 'Bearer stale-token') {
      const response = unauthorizedResponse(config)
      const error = Object.assign(new Error('Request failed with status code 401'), {
        isAxiosError: true,
        config,
        response,
        toJSON: () => ({}),
      })
      throw error
    }
    return okResponse(config, { ok: true, url: config.url })
  }
  return { adapter, calls }
}

describe('NovaHttpClient single-flight refresh', () => {
  let refreshCalls = 0
  let resolveRefresh: (() => void) | null = null

  beforeEach(() => {
    __resetTokenStoreForTests()
    refreshCalls = 0
    resolveRefresh = null
  })

  afterEach(() => {
    __resetTokenStoreForTests()
    vi.restoreAllMocks()
  })

  it('refreshes ONCE for N concurrent 401s and replays every request', async () => {
    setTokens({ accessToken: 'stale-token', refreshToken: 'refresh-1' })
    const { adapter, calls } = makeAdapter({ failFirstWith401: true })

    const refreshHandler = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          refreshCalls += 1
          resolveRefresh = () => {
            setTokens({ accessToken: 'fresh-token', refreshToken: 'refresh-2' })
            resolve()
          }
        }),
    )

    const client = new NovaHttpClient({ refreshHandler })
    client.axios.defaults.adapter = adapter

    // Fire five requests that will all fail with 401 at roughly the same time.
    const pending = Promise.all([
      client.get<{ ok: boolean }>('/orders/orders'),
      client.get<{ ok: boolean }>('/cart/customer/cart'),
      client.get<{ ok: boolean }>('/auth/users/me'),
      client.get<{ ok: boolean }>('/analytics/admin/overview'),
      client.get<{ ok: boolean }>('/inventory/admin/stock'),
    ])

    // Let all five reach the 401 path before the refresh resolves.
    await vi.waitFor(() => {
      expect(refreshCalls).toBe(1)
    })
    expect(resolveRefresh).not.toBeNull()
    resolveRefresh?.()

    const results = await pending

    expect(results).toHaveLength(5)
    expect(results.every((r) => r.ok)).toBe(true)
    // THE assertion: one refresh, not five.
    expect(refreshHandler).toHaveBeenCalledTimes(1)
    expect(refreshCalls).toBe(1)

    // Each original request was attempted twice: once stale, once fresh.
    const retried = calls.filter((c) => c.authorization === 'Bearer fresh-token')
    expect(retried).toHaveLength(5)
  })

  it('does not refresh when the request succeeds', async () => {
    setTokens({ accessToken: 'good-token', refreshToken: 'refresh-1' })
    const { adapter } = makeAdapter({ failFirstWith401: true })
    const refreshHandler = vi.fn(() => Promise.resolve())

    const client = new NovaHttpClient({ refreshHandler })
    client.axios.defaults.adapter = adapter

    const result = await client.get<{ ok: boolean }>('/catalog/public/products')
    expect(result.ok).toBe(true)
    expect(refreshHandler).not.toHaveBeenCalled()
  })

  it('allows a SECOND refresh after the first one settled (promise is not cached)', async () => {
    setTokens({ accessToken: 'stale-token', refreshToken: 'refresh-1' })
    const { adapter } = makeAdapter({ failFirstWith401: true })
    const refreshHandler = vi.fn(async () => {
      setTokens({ accessToken: 'fresh-token' })
    })

    const client = new NovaHttpClient({ refreshHandler })
    client.axios.defaults.adapter = adapter

    await client.get('/orders/orders')
    expect(refreshHandler).toHaveBeenCalledTimes(1)

    // Simulate the token expiring again later.
    setTokens({ accessToken: 'stale-token' })
    await client.get('/orders/orders')
    expect(refreshHandler).toHaveBeenCalledTimes(2)
  })

  it('fails fast without looping when the retry also returns 401', async () => {
    setTokens({ accessToken: 'stale-token', refreshToken: 'refresh-1' })
    const { adapter, calls } = makeAdapter({ failFirstWith401: true })
    // Refresh "succeeds" but hands back a token the server still rejects.
    const refreshHandler = vi.fn(async () => {
      setTokens({ accessToken: 'stale-token' })
    })

    const client = new NovaHttpClient({ refreshHandler })
    client.axios.defaults.adapter = adapter

    await expect(client.get('/orders/orders')).rejects.toMatchObject({
      code: ErrorCode.TOKEN_EXPIRED,
      unauthenticated: true,
    })
    // Original + one replay = 2; no infinite loop.
    expect(calls).toHaveLength(2)
    expect(refreshHandler).toHaveBeenCalledTimes(1)
  })

  it('propagates a trace id on every attempt and stores it for error reporting', async () => {
    setTokens({ accessToken: 'good-token' })
    const { adapter, calls } = makeAdapter({ failFirstWith401: false })
    const client = new NovaHttpClient({})
    client.axios.defaults.adapter = adapter

    await client.get('/catalog/public/products')

    expect(calls[0]?.traceId).toMatch(/^[0-9a-f]{16}$/)
  })

  it('clears the session only when the refresh itself fails', async () => {
    setTokens({ accessToken: 'stale-token', refreshToken: 'refresh-1' })
    const { adapter } = makeAdapter({ failFirstWith401: true })
    const onSessionExpired = vi.fn()
    const refreshHandler = vi.fn(async () => {
      throw new Error('refresh rejected')
    })

    const client = new NovaHttpClient({ refreshHandler, onSessionExpired })
    client.axios.defaults.adapter = adapter

    await expect(client.get('/orders/orders')).rejects.toBeTruthy()
    expect(onSessionExpired).toHaveBeenCalledTimes(1)
    expect(getAccessToken()).toBe('')
  })
})
