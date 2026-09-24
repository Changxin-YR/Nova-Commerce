/**
 * Token storage for the HTTP client.
 *
 * The short-lived access token is mirrored into localStorage for reloads. The
 * refresh token is an HttpOnly cookie and is never readable by JavaScript (§23).
 *
 * The auth store is the only writer. The client reads through this module so that
 * `src/api/client.ts` stays free of Pinia imports — Pinia cannot be touched from
 * an axios interceptor before the app is created, and importing it here would make
 * the client untestable in isolation.
 */

import { STORAGE_KEYS } from '@/config/constants'

export interface TokenPair {
  accessToken: string
}

let accessToken = ''

/** Called when the client concludes the session is unrecoverable. */
type SessionExpiredListener = () => void
const sessionExpiredListeners = new Set<SessionExpiredListener>()

export function setTokens(pair: Partial<TokenPair> | null): void {
  if (!pair) {
    clearTokens()
    return
  }
  if (typeof pair.accessToken === 'string') accessToken = pair.accessToken
  persist()
}

export function getAccessToken(): string {
  try {
    globalThis.localStorage?.removeItem(STORAGE_KEYS.refreshToken)
  } catch {
    // Legacy browser state may be inaccessible; access-token reads still work.
  }
  if (!accessToken) accessToken = read(STORAGE_KEYS.accessToken)
  return accessToken
}

export function clearTokens(): void {
  accessToken = ''
  try {
    globalThis.localStorage?.removeItem(STORAGE_KEYS.accessToken)
    globalThis.localStorage?.removeItem(STORAGE_KEYS.refreshToken)
  } catch {
    // Ignore: memory is already cleared, which is what matters.
  }
}

export function hasSession(): boolean {
  return Boolean(getAccessToken())
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
  sessionExpiredListeners.clear()
}

function persist(): void {
  try {
    globalThis.localStorage?.setItem(STORAGE_KEYS.accessToken, accessToken)
    globalThis.localStorage?.removeItem(STORAGE_KEYS.refreshToken)
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
