/**
 * Auth store (§105) — one of the only six allowed Pinia stores.
 *
 * Owns the session: tokens live in `src/api/tokenStore` (so the HTTP client can read
 * them without importing Pinia), while the user profile and login status live here.
 *
 * It subscribes to the client's `session-expired` event so a failed refresh in ANY
 * request lands in one place instead of each caller handling a 401 itself.
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { authApi, clearTokens, hasSession, onSessionExpired } from '@/api'
import { usePermissionStore } from '@/stores/permission'
import type { CurrentUser, LoginRequest } from '@/types/api-contract'
import type { NormalizedApiError } from '@/types/api'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<CurrentUser | null>(null)
  const loading = ref(false)
  const error = ref<NormalizedApiError | null>(null)
  const bootstrapped = ref(false)
  /** True once a login succeeded but `me()` has not resolved yet. */
  const authenticated = ref(false)

  const isLoggedIn = computed(() => authenticated.value && user.value !== null)
  const displayName = computed(() => user.value?.display_name ?? user.value?.username ?? '未登录')

  let unsubscribe: (() => void) | null = null

  function ensureSessionListener(): void {
    if (unsubscribe) return
    unsubscribe = onSessionExpired(() => {
      // The token is already gone; clear the profile so guards redirect to login.
      user.value = null
      authenticated.value = false
      usePermissionStore().reset()
    })
  }

  async function login(payload: LoginRequest): Promise<CurrentUser> {
    loading.value = true
    error.value = null
    try {
      const result = await authApi.login(payload)
      user.value = result.user
      authenticated.value = true
      usePermissionStore().setFromUser(result.user)
      ensureSessionListener()
      return result.user
    } catch (e) {
      error.value = e as NormalizedApiError
      throw e
    } finally {
      loading.value = false
    }
  }

  async function logout(): Promise<void> {
    try {
      await authApi.logout()
    } catch {
      // A failed logout must still clear local state: otherwise the user thinks
      // they are logged out while the token keeps working.
    } finally {
      reset()
    }
  }

  /** Restore the session after a page reload when a token is still stored. */
  async function bootstrap(): Promise<void> {
    if (bootstrapped.value) return
    ensureSessionListener()
    if (!hasSession()) {
      bootstrapped.value = true
      return
    }
    loading.value = true
    try {
      const me = await authApi.me()
      user.value = me
      authenticated.value = true
      usePermissionStore().setFromUser(me)
      // Permission codes are fetched separately: a forbidden permission call must
      // not invalidate an otherwise valid session.
      try {
        await usePermissionStore().load()
      } catch {
        // UX-only data; the server still enforces everything.
      }
    } catch {
      reset()
    } finally {
      loading.value = false
      bootstrapped.value = true
    }
  }

  function reset(): void {
    clearTokens()
    user.value = null
    authenticated.value = false
    error.value = null
    usePermissionStore().reset()
  }

  return {
    user,
    loading,
    error,
    bootstrapped,
    authenticated,
    isLoggedIn,
    displayName,
    login,
    logout,
    bootstrap,
    reset,
  }
})
