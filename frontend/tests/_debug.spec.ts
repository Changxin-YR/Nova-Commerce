import type { AxiosAdapter, AxiosResponse, InternalAxiosRequestConfig } from 'axios'
import { describe, expect, it } from 'vitest'
import { NexoraHttpClient } from '@/api/client'
import { setTokens } from '@/api/tokenStore'
import { ErrorCode } from '@/types/api'

function unauthorized(config: InternalAxiosRequestConfig): AxiosResponse {
  return {
    status: 401,
    statusText: 'Unauthorized',
    headers: {},
    config,
    data: { code: ErrorCode.TOKEN_EXPIRED, message: 'TOKEN EXPIRED', data: null, trace_id: 't' },
  }
}

describe('debug single flight', () => {
  it('traces', async () => {
    setTokens({ accessToken: 'stale', refreshToken: 'r1' })
    const events: string[] = []
    const adapter: AxiosAdapter = async (config) => {
      const headers = config.headers as unknown as { get?: (k: string) => unknown }
      const auth = headers?.get?.('Authorization') as string | undefined
      events.push(`req ${String(config.url)} auth=${auth} retried=${String((config as Record<string, unknown>).__retried)}`)
      if (auth === 'Bearer stale') {
        throw Object.assign(new Error('401'), {
          isAxiosError: true,
          config,
          response: unauthorized(config),
          toJSON: () => ({}),
        })
      }
      return {
        status: 200,
        statusText: 'OK',
        headers: {},
        config,
        data: { code: 0, message: 'OK', data: { ok: true }, trace_id: 't' },
      }
    }
    let resolveRefresh: (() => void) | null = null
    const client = new NexoraHttpClient({
      refreshHandler: () =>
        new Promise<void>((resolve) => {
          events.push('refresh called')
          resolveRefresh = () => {
            setTokens({ accessToken: 'fresh' })
            events.push('refresh resolved')
            resolve()
          }
        }),
      onSessionExpired: () => events.push('SESSION EXPIRED'),
    })
    client.axios.defaults.adapter = adapter

    const p = Promise.all([client.get('/a'), client.get('/b')])
    await new Promise((r) => setTimeout(r, 30))
    events.push('--- resolving refresh ---')
    resolveRefresh?.()
    try {
      const res = await p
      events.push(`result ok=${JSON.stringify(res)}`)
    } catch (e) {
      events.push(`threw ${JSON.stringify(e)}`)
    }
    console.log(events.join('\n'))
    expect(events.length).toBeGreaterThan(0)
  })
})
