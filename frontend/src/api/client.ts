/**
 * THE unified HTTP client (§106, REQ-FE-008).
 *
 * Every request in this app goes through here. Components MUST NOT import axios;
 * they import a module from `src/api/` and that module uses this client.
 *
 * Responsibilities handled centrally:
 *   - base URL + request timeout
 *   - auth header injection (Bearer, read lazily so a refresh is picked up)
 *   - `X-Trace-Id` propagation out, trace-id capture back
 *   - 401 -> refresh-token retry with SINGLE-FLIGHT coalescing
 *   - error mapping into the backend envelope shape
 *
 * SINGLE-FLIGHT REFRESH (the subtle part)
 *  When N requests fail with 401 at the same moment, they must NOT start N
 *  refreshes. The backend rotates refresh tokens and detects reuse
 *  (`REFRESH_TOKEN_REUSED` = 20004), so a stampede would log the user out for
 *  real. Therefore: the first 401 starts one refresh; every other 401 awaits that
 *  same promise; when it resolves all waiters replay their original request with
 *  the new token. The refresh call itself is marked `skipAuthRefresh` so a 401
 *  from the refresh endpoint cannot recurse.
 */

import axios, {
  type AxiosError,
  type AxiosInstance,
  type AxiosRequestConfig,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios'
import {
  API_BASE_URL,
  CONTENT_TYPE_JSON,
  DEFAULT_TIMEOUT_MS,
  IDEMPOTENCY_KEY_HEADER,
} from '@/config/constants'
import { ErrorCode, TRANSPORT_CODES, type ApiEnvelope, type NormalizedApiError } from '@/types/api'
import { isApiEnvelope, normalizeError } from '@/api/error'
import {
  clearTokens,
  emitSessionExpired,
  getAccessToken,
  getRefreshToken,
  setTokens,
} from '@/api/tokenStore'
import { TRACE_ID_HEADER, newTraceId, readTraceIdHeader, setLastTraceId } from '@/utils/trace'

/** Per-request options understood by our interceptors. */
export interface NexoraExtras {
  /** Absolute path (e.g. '/health/ready') —never prefixed with the base URL. */
  absoluteUrl?: string
  /** Set on the refresh request itself and on the replay to break the 401 loop. */
  skipAuthRefresh?: boolean
  /** Set after one replay so a second 401 fails fast instead of looping. */
  __retried?: boolean
  /** Caller-supplied idempotency key for non-idempotent writes (§96). */
  idempotencyKey?: string
}

/**
 * Request config accepted by our methods.
 *
 * `headers` is deliberately loosely typed (axios itself accepts an `AxiosHeaders`
 * instance, a plain object, or undefined) —narrowing it here produced a config
 * that no caller could satisfy. The interceptors normalize it before use.
 */
export type NexoraRequestConfig = AxiosRequestConfig & NexoraExtras


export interface NexoraClientOptions {
  baseURL?: string
  timeoutMs?: number
  /**
   * Performs the actual refresh. Defaults to POSTing `/auth/refresh`.
   * Injected in tests to assert single-flight behaviour without a server.
   */
  refreshHandler?: () => Promise<void>
  /** Called once when the session is unrecoverable. */
  onSessionExpired?: () => void
}

/** Shape returned by the refresh endpoint. */
interface RefreshPayload {
  access_token?: string
  refresh_token?: string
  accessToken?: string
  refreshToken?: string
}

/**
 * Build a `NormalizedApiError` for a failure that never reached the server.
 * `normalizeError` returns these objects untouched, so the shape must stay exact.
 */
function transportError(code: number, message: string): NormalizedApiError {
  return {
    code,
    message,
    traceId: '',
    httpStatus: 0,
    retryable: code === TRANSPORT_CODES.NETWORK_ERROR || code === TRANSPORT_CODES.TIMEOUT,
    forbidden: false,
    unauthenticated: code === TRANSPORT_CODES.SESSION_EXPIRED,
  }
}

/**
 * Stable per-tab client id sent as `client_request_id` on writes so the backend
 * can collapse retries of the same logical submission (§96).
 */
function clientRequestId(): string {
  return newTraceId()
}

export class NexoraHttpClient {
  readonly axios: AxiosInstance
  private refreshPromise: Promise<void> | null = null
  private readonly refreshHandler: () => Promise<void>
  private readonly onSessionExpired: () => void

  constructor(options: NexoraClientOptions = {}) {
    this.axios = axios.create({
      baseURL: options.baseURL ?? API_BASE_URL,
      timeout: options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
      headers: { 'Content-Type': CONTENT_TYPE_JSON, Accept: CONTENT_TYPE_JSON },
      // Let the interceptor decide: a 401 must reach us instead of throwing early.
      validateStatus: (status) => status >= 200 && status < 300,
    })
    this.refreshHandler = options.refreshHandler ?? (() => this.defaultRefresh())
    this.onSessionExpired = options.onSessionExpired ?? (() => emitSessionExpired())
    this.installInterceptors()
  }

  // -- public API ---------------------------------------------------------

  /** Full envelope —use when the caller needs `trace_id` or `message`. */
  async request<T>(config: NexoraRequestConfig): Promise<ApiEnvelope<T>> {
    const response = await this.axios.request<ApiEnvelope<T>>(config)
    return response.data
  }

  /**
   * Envelope-unwrapped DATA. Throws `NormalizedApiError` on any failure, so
   * callers can `catch (e)` and read a stable shape without inspecting axios.
   *
   * NOTE: if the backend returns a paged payload as a BARE ARRAY (which several
   * list endpoints do), `envelope.data` IS that array; `Paged<T>` is only for
   * endpoints that wrap items in `{items, meta}`. API modules declare which.
   */
  async requestData<T>(config: NexoraRequestConfig): Promise<T> {
    const envelope = await this.request<T>(config)
    return envelope.data as T
  }

  get<T>(url: string, config: AxiosRequestConfig = {}): Promise<T> {
    return this.requestData<T>({ ...config, method: 'GET', url })
  }

  post<T>(
    url: string,
    body?: unknown,
    config: NexoraRequestConfig = {},
  ): Promise<T> {
    return this.requestData<T>({
      ...config,
      method: 'POST',
      url,
      data: body,
      idempotencyKey: config.idempotencyKey ?? (body === undefined ? undefined : clientRequestId()),
    })
  }

  put<T>(url: string, body?: unknown, config: NexoraRequestConfig = {}): Promise<T> {
    return this.requestData<T>({ ...config, method: 'PUT', url, data: body })
  }

  patch<T>(url: string, body?: unknown, config: NexoraRequestConfig = {}): Promise<T> {
    return this.requestData<T>({ ...config, method: 'PATCH', url, data: body })
  }

  delete<T>(url: string, config: NexoraRequestConfig = {}): Promise<T> {
    return this.requestData<T>({ ...config, method: 'DELETE', url })
  }

  /** Multipart upload. Overrides the JSON content type for this call only. */
  upload<T>(url: string, form: FormData, config: NexoraRequestConfig = {}): Promise<T> {
    return this.requestData<T>({
      ...config,
      method: 'POST',
      url,
      data: form,
      headers: { ...config.headers, 'Content-Type': 'multipart/form-data' },
    })
  }

  /** Raw fetch that bypasses the envelope unwrapping. Used for SSE (the agent
   * event stream is `text/event-stream`, not an envelope) and for `/health`
   * probes. Auth/trace headers are still applied by the interceptor.
   */
  async rawFetch(url: string, init: RequestInit = {}): Promise<Response> {
    const headers = new Headers(init.headers)
    const token = getAccessToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
    if (!headers.has(TRACE_ID_HEADER)) headers.set(TRACE_ID_HEADER, newTraceId())
    const base = this.axios.defaults.baseURL ?? ''
    const full = /^https?:\/\//i.test(url) ? url : `${base}${url}`
    const response = await fetch(full, { ...init, headers })
    const trace = readTraceIdHeader(response.headers as unknown)
    if (trace) setLastTraceId(trace)
    return response
  }

  /** Request a path OUTSIDE the `/api/v1` prefix (e.g. `/health/ready`). */
  absolute<T>(url: string, config: NexoraRequestConfig = {}): Promise<T> {
    return this.requestData<T>({
      ...config,
      url,
      absoluteUrl: url,
    })
  }

  /** Test seam: force-clear the in-flight refresh so specs start clean. */
  __resetSingleFlightForTests(): void {
    this.refreshPromise = null
  }

  // -- internals ----------------------------------------------------------

  private installInterceptors(): void {
    this.axios.interceptors.request.use((config) => {
      // Axios guarantees a concrete `AxiosHeaders` instance at this point, while
      // the public config type loosely allows an object or undefined. Our extras
      // (idempotencyKey / absoluteUrl / skipAuthRefresh) ride along unchanged.
      const cfg = config as InternalAxiosRequestConfig & NexoraExtras
      if (!cfg.absoluteUrl) {
        if (!cfg.headers.has(TRACE_ID_HEADER)) cfg.headers.set(TRACE_ID_HEADER, newTraceId())
      } else {
        // Absolute path outside `/api/v1` (e.g. `/health/ready`). Axios joins
        // `baseURL` with a leading-slash url by CONCATENATION, so the default base
        // must be neutralized for this one request.
        cfg.baseURL = ''
        cfg.url = cfg.absoluteUrl
      }

      const token = getAccessToken()
      if (token && !cfg.headers.has('Authorization')) {
        cfg.headers.set('Authorization', `Bearer ${token}`)
      }

      if (cfg.idempotencyKey && !cfg.headers.has(IDEMPOTENCY_KEY_HEADER)) {
        cfg.headers.set(IDEMPOTENCY_KEY_HEADER, cfg.idempotencyKey)
      }

      return cfg
    })

    this.axios.interceptors.response.use(
      (response: AxiosResponse) => {
        const trace = readTraceIdHeader(response.headers as unknown)
        const envelope = response.data
        if (trace) {
          setLastTraceId(trace)
        } else if (isApiEnvelope(envelope) && envelope.trace_id) {
          setLastTraceId(envelope.trace_id)
        }
        return response
      },
      async (error: AxiosError) => this.handleResponseError(error),
    )
  }

  private async handleResponseError(error: AxiosError): Promise<AxiosResponse> {
    const cfg = (error.config ?? {}) as InternalAxiosRequestConfig & NexoraExtras
    const responseTrace = readTraceIdHeader(error.response?.headers as unknown)
    if (responseTrace) setLastTraceId(responseTrace)

    const envelope = isApiEnvelope(error.response?.data) ? error.response?.data : undefined
    const businessCode = envelope?.code
    const status = error.response?.status ?? 0
    const refreshable =
      !cfg.skipAuthRefresh &&
      !cfg.__retried &&
      (status === 401 || (businessCode !== undefined && (businessCode === ErrorCode.UNAUTHENTICATED || businessCode === ErrorCode.TOKEN_EXPIRED)))

    if (refreshable) {
      try {
        await this.refreshOnce()
      } catch (refreshError) {
        this.sessionExpired()
        throw normalizeError(refreshError, responseTrace || (envelope?.trace_id ?? ''))
      }
      const replay: NexoraRequestConfig = { ...cfg, __retried: true }
      return this.axios.request(replay)
    }

    const normalized = normalizeError(error, responseTrace || (envelope?.trace_id ?? ''))
    if (normalized.unauthenticated) this.sessionExpired()
    throw normalized
  }

  /**
   * SINGLE-FLIGHT: concurrent callers share one in-flight refresh promise.
   * Cleared in a `finally` so a later 401 can refresh again, but a `catch`-free
   * rejection path still stops waiters from re-using a rejected promise.
   */
  private refreshOnce(): Promise<void> {
    if (this.refreshPromise) return this.refreshPromise
    this.refreshPromise = (async () => {
      try {
        await this.refreshHandler()
      } finally {
        this.refreshPromise = null
      }
    })()
    return this.refreshPromise
  }

  private async defaultRefresh(): Promise<void> {
    const refreshToken = getRefreshToken()
    if (!refreshToken) {
      throw transportError(TRANSPORT_CODES.SESSION_EXPIRED, '登录已过期，请重新登录')
    }
    const response = await this.axios.request<ApiEnvelope<RefreshPayload>>({
      method: 'POST',
      url: '/auth/refresh',
      data: { refresh_token: refreshToken },
      skipAuthRefresh: true,
    } as NexoraRequestConfig)

    const payload = response.data.data ?? {}
    const accessToken = payload.access_token ?? payload.accessToken ?? ''
    const nextRefresh = payload.refresh_token ?? payload.refreshToken ?? refreshToken
    if (!accessToken) {
      throw transportError(TRANSPORT_CODES.MALFORMED_RESPONSE, '服务返回格式异常，请联系技术支持')
    }
    setTokens({ accessToken, refreshToken: nextRefresh })
  }

  private sessionExpired(): void {
    clearTokens()
    this.onSessionExpired()
  }
}

/** App-wide singleton. */
export const http = new NexoraHttpClient()

/** Named export for readability in API modules: `httpClient.get<T>(...)`. */
export const httpClient = http

export type { AxiosRequestConfig }
