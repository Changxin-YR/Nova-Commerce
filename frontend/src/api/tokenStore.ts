/**
 * Token storage for the HTTP client.
 *
 * Tokens are kept in an in-memory slot (the source of truth for a running tab)
 * and mirrored into `localStorage` so a page reload does not log the user out.
 *
 * The auth store is the only writer. The client reads through this module so that
 * `src/api/client.ts` stays free of Pinia imports — Pinia cannot be touched from
 * an axios interceptor before the app is created, and importing it here would make
 * the client untestable in isolation.
 */

import { STORAGE_KEYS } from '@/config/constants'

export interface TokenPair {
  accessToken: string
  refreshToken: string
}

let accessToken = ''
let refreshToken = ''

/** Called when the client concludes the session is unrecoverable. */
type SessionExpiredListener = () => void
const sessionExpiredListeners = new Set<SessionExpiredListener>()

export function setTokens(pair: Partial<TokenPair> | null): void {
  if (!pair) {
    clearTokens()
    return
  }
  if (typeof pair.accessToken === 'string') accessToken = pair.accessToken
  if (typeof pair.refreshToken === 'string') refreshToken = pair.refreshToken
  persist()
}

export function getAccessToken(): string {
  if (!accessToken) accessToken = read(STORAGE_KEYS.accessToken)
  return accessToken
}

export function getRefreshToken(): string {
  if (!refreshToken) refreshToken = read(STORAGE_KEYS.refreshToken)
  return refreshToken
}

export function clearTokens(): void {
  accessToken = ''
  refreshToken = ''
  try {
    globalThis.localStorage?.removeItem(STORAGE_KEYS.accessToken)
    globalThis.localStorage?.removeItem(STORAGE_KEYS.refreshToken)
  } catch {
    // Ignore: memory is already cleared, which is what matters.
  }
}

export function hasSession(): boolean {
  return Boolean(getAccessToken() || getRefreshToken())
}

export function onSessionExpired(listener: SessionExpiredListener): () => void {
  sessionExpiredListeners.add(listener)
  return () => sessionExpiredListeners.delete(listener)
}

export function emitSessionExpired(): void {
  clearTokens()
  sessionExpiredListeners.forEach((listener) => {
    try {
      listener()
    } catch {
      // A failing listener must not prevent the remaining listeners from running.
    }
  })
}

/** Test seam: reset module-level state between specs. */
export function __resetTokenStoreForTests(): void {
  accessToken = ''
  refreshToken = ''
  sessionExpiredListeners.clear()
}

function persist(): void {
  try {
    globalThis.localStorage?.setItem(STORAGE_KEYS.accessToken, accessToken)
    globalThis.localStorage?.setItem(STORAGE_KEYS.refreshToken, refreshToken)
  } catch {
    // Private mode / quota exceeded: the tab still works until it is reloaded.
  }
}

function read(key: string): string {
  try {
    return globalThis.localStorage?.getItem(key) ?? ''
  } catch {
    return ''
  }
}
