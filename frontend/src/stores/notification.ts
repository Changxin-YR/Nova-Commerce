/**
 * Notification store (§105) — one of the only six allowed Pinia stores.
 *
 * A cross-page queue (toast the user even after they navigate away plus the bell
 * badge), not a general UI dump. Vitest specs can render notices without touching
 * Element Plus by reading `notices` directly.
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

export type NoticeLevel = 'info' | 'success' | 'warning' | 'error'

export interface Notice {
  id: string
  level: NoticeLevel
  title: string
  message?: string
  /** Business code + trace id, so a screenshot is enough to find the log line. */
  code?: number
  traceId?: string
  createdAt: string
  read: boolean
  /** Disappears by itself; errors stay until dismissed. */
  autoDismissMs?: number
}

let seq = 0

export const useNotificationStore = defineStore('notification', () => {
  const notices = ref<Notice[]>([])
  const unreadCount = computed(() => notices.value.filter((notice) => !notice.read).length)
  const latest = computed(() => notices.value[0] ?? null)

  function push(input: Omit<Notice, 'id' | 'createdAt' | 'read'>): Notice {
    seq += 1
    const notice: Notice = {
      ...input,
      id: `notice-${Date.now().toString(36)}-${seq}`,
      createdAt: new Date().toISOString(),
      read: false,
      // Errors are never auto-dismissed: the user must acknowledge them.
      autoDismissMs: input.autoDismissMs ?? (input.level === 'error' ? 0 : 4000),
    }
    notices.value.unshift(notice)
    // Bound the in-memory queue so a long session cannot grow without limit.
    if (notices.value.length > 100) notices.value.length = 100
    return notice
  }

  const info = (title: string, message?: string) => push({ level: 'info', title, message })
  const success = (title: string, message?: string) => push({ level: 'success', title, message })
  const warning = (title: string, message?: string) => push({ level: 'warning', title, message })
  const error = (title: string, message?: string, code?: number, traceId?: string) =>
    push({ level: 'error', title, message, code, traceId })

  function markRead(id: string): void {
    const notice = notices.value.find((item) => item.id === id)
    if (notice) notice.read = true
  }

  function markAllRead(): void {
    notices.value.forEach((notice) => {
      notice.read = true
    })
  }

  function dismiss(id: string): void {
    notices.value = notices.value.filter((notice) => notice.id !== id)
  }

  function clear(): void {
    notices.value = []
  }

  return {
    notices,
    unreadCount,
    latest,
    push,
    info,
    success,
    warning,
    error,
    markRead,
    markAllRead,
    dismiss,
    clear,
  }
})
