/**
 * Permission store (§104, §105).
 *
 * Holds the permission codes the LAST server response reported. It exists so the UI
 * can hide what the user cannot do — nothing more.
 *
 * !!! THE BACKEND IS THE REAL AUTHORITY !!!
 * Route guards, menu filtering and `v-permission` all read this store, and all of
 * them are UX only. The server re-checks every request; a user who forges these
 * codes in the console gains exactly nothing.
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { authApi } from '@/api'
import type { CurrentUser } from '@/types/api-contract'

export const usePermissionStore = defineStore('permission', () => {
  const roles = ref<string[]>([])
  const permissions = ref<string[]>([])
  const loaded = ref(false)

  const hasRole = (role: string): boolean => roles.value.includes(role)
  const hasAnyRole = (candidates: readonly string[]): boolean =>
    candidates.some((role) => roles.value.includes(role))

  const has = (code: string): boolean => permissions.value.includes(code)
  const hasAny = (codes: readonly string[]): boolean =>
    codes.length === 0 || codes.some((code) => permissions.value.includes(code))
  const hasAll = (codes: readonly string[]): boolean =>
    codes.length === 0 || codes.every((code) => permissions.value.includes(code))

  /** Console access is gated on at least one console role. */
  const canAccessConsole = computed(() => hasAnyRole(['admin', 'operator', 'viewer']))

  function setFromUser(user: CurrentUser | null): void {
    roles.value = user?.roles ?? []
  }

  /** Fetch codes from the server. Failure leaves the previous snapshot intact. */
  async function load(): Promise<void> {
    const result = await authApi.permissions()
    roles.value = result.roles
    permissions.value = result.permissions
    loaded.value = true
  }

  function reset(): void {
    roles.value = []
    permissions.value = []
    loaded.value = false
  }

  return {
    roles,
    permissions,
    loaded,
    canAccessConsole,
    has,
    hasAny,
    hasAll,
    hasRole,
    hasAnyRole,
    setFromUser,
    load,
    reset,
  }
})
