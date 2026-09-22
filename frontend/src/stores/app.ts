/**
 * App store (§105) — one of the only six allowed Pinia stores.
 *
 * Genuinely cross-page shell state only: theme, locale, sidebar and the global
 * request-progress flag. Page-level UI state (a dialog's open flag, a form draft, a
 * table's sort order) belongs to the component that owns it and must NOT be moved
 * here — that is the failure mode §105 exists to prevent.
 */

import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { STORAGE_KEYS } from '@/config/constants'
import { APP_TITLE } from '@/config/app'

export type ThemeMode = 'light' | 'dark'

function readStoredTheme(): ThemeMode {
  try {
    const stored = globalThis.localStorage?.getItem(STORAGE_KEYS.theme)
    if (stored === 'dark' || stored === 'light') return stored
  } catch {
    // Storage unavailable: fall through to the system preference.
  }
  const prefersDark =
    typeof globalThis.matchMedia === 'function' &&
    globalThis.matchMedia('(prefers-color-scheme: dark)').matches
  return prefersDark ? 'dark' : 'light'
}

export const useAppStore = defineStore('app', () => {
  const theme = ref<ThemeMode>(readStoredTheme())
  const sidebarCollapsed = ref(false)
  const consoleSidebarCollapsed = ref(false)
  const locale = ref<'zh-CN' | 'en-US'>('zh-CN')
  /** Number of in-flight requests. The top progress bar shows when > 0. */
  const pendingRequests = ref(0)
  const globalError = ref<string>('')

  const isDark = computed(() => theme.value === 'dark')
  const isBusy = computed(() => pendingRequests.value > 0)

  function applyTheme(value: ThemeMode): void {
    theme.value = value
    const root = globalThis.document?.documentElement
    if (root) {
      // Element Plus reads `dark` on <html> for its dark variables.
      root.classList.toggle('dark', value === 'dark')
      root.dataset.theme = value
    }
    try {
      globalThis.localStorage?.setItem(STORAGE_KEYS.theme, value)
    } catch {
      // Non-fatal: the theme still applies for this session.
    }
  }

  function toggleTheme(): void {
    applyTheme(theme.value === 'dark' ? 'light' : 'dark')
  }

  function toggleSidebar(): void {
    sidebarCollapsed.value = !sidebarCollapsed.value
  }

  function toggleConsoleSidebar(): void {
    consoleSidebarCollapsed.value = !consoleSidebarCollapsed.value
  }

  function startRequest(): void {
    pendingRequests.value += 1
  }

  function finishRequest(): void {
    pendingRequests.value = Math.max(0, pendingRequests.value - 1)
  }

  function setGlobalError(message: string): void {
    globalError.value = message
  }

  function setDocumentTitle(pageTitle?: string): void {
    const title = pageTitle ? `${pageTitle} · ${APP_TITLE}` : APP_TITLE
    if (globalThis.document) globalThis.document.title = title
  }

  // Apply once on store creation so a reload keeps the chosen theme.
  applyTheme(theme.value)

  // Keep <html> in sync if the value is changed by another tab or a devtools edit.
  watch(theme, (value) => applyTheme(value))

  return {
    theme,
    isDark,
    sidebarCollapsed,
    consoleSidebarCollapsed,
    locale,
    pendingRequests,
    isBusy,
    globalError,
    applyTheme,
    toggleTheme,
    toggleSidebar,
    toggleConsoleSidebar,
    startRequest,
    finishRequest,
    setGlobalError,
    setDocumentTitle,
  }
})
