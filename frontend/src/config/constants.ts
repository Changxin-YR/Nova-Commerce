/**
 * Global frontend constants.
 *
 * Spec refs: §95 (uniform envelope under /api/v1), §104 (permission is UX only),
 * §100 (route names), §106 (single HTTP client).
 */

/** Base URL for the versioned API. One origin in production via Nginx (:18000). */
export const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? '/api/v1'

/** Default request timeout in milliseconds. */
export const DEFAULT_TIMEOUT_MS = Number(import.meta.env.VITE_API_TIMEOUT ?? 15000)

/** Correlation headers. The backend reads `X-Trace-Id` back out of every response. */
export const TRACE_ID_HEADER = 'X-Trace-Id'

/** Required by the backend for non-idempotent writes (§96 order create). */
export const IDEMPOTENCY_KEY_HEADER = 'Idempotency-Key'

/** Content type for JSON bodies. */
export const CONTENT_TYPE_JSON = 'application/json'

/** Storage keys. Tokens live in localStorage so a refresh survives a page reload. */
export const STORAGE_KEYS = {
  accessToken: 'nova.access_token',
  refreshToken: 'nova.refresh_token',
  traceId: 'nova.last_trace_id',
  theme: 'nova.theme',
  locale: 'nova.locale',
} as const

/** Success business code. Everything else is a failure (§95). */
export const CODE_OK = 0

/**
 * Business codes the HTTP client must treat as "the access token is gone, try a
 * refresh once" (see src/api/client.ts). Kept narrow on purpose: a 401 caused by
 * `CSRF_VALIDATION_FAILED` or `ACCOUNT_DISABLED` must NOT trigger a refresh loop.
 */
export const REFRESHABLE_CODES: readonly number[] = [
  20000, // UNAUTHENTICATED
  20002, // TOKEN_EXPIRED
]

/** Business codes that mean "the caller is authenticated but not allowed" (§104). */
export const FORBIDDEN_CODES: readonly number[] = [
  20008, // FORBIDDEN
  20009, // INSUFFICIENT_PERMISSION
  20010, // DATA_SCOPE_VIOLATION
  20012, // MERCHANT_MISMATCH
]

/** Roles known to the console. Strings are for display; the server is authoritative. */
export const CONSOLE_ROLES = ['admin', 'operator', 'viewer'] as const
export type ConsoleRole = (typeof CONSOLE_ROLES)[number]
