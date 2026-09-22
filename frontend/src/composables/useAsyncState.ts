/**
 * `useAsyncState` — the shared bridge between an API call and the §108 UI states.
 *
 * Every page needs the same four things: a status, the data, the mapped error, and a
 * retry. Centralizing it here is what makes "every core page implements Loading /
 * Success / Empty / Error / PermissionDenied" achievable instead of aspirational.
 *
 * `permission_denied` is DERIVED from the error (`normalized.forbidden`), so a page
 * cannot forget to handle a 403 — it is the same code path as any other failure,
 * rendered with the correct affordance.
 */

import { computed, ref, shallowRef } from 'vue'
import { normalizeError } from '@/api/error'
import type { NormalizedApiError } from '@/types/api'
import type { UiState } from '@/components/ui/StateView.vue'

export interface UseAsyncStateOptions<T> {
  /** Treat an empty array / null / zero-length result as `empty`. */
  isEmpty?: (data: T) => boolean
  /** Run immediately on setup. Defaults to true. */
  immediate?: boolean
  /** Initial value before the first successful load. */
  initial?: T | null
}

export function useAsyncState<T>(
  loader: () => Promise<T>,
  options: UseAsyncStateOptions<T> = {},
) {
  /** `shallowRef`: API payloads are large and never mutated in place. */
  const data = shallowRef<T | null>(options.initial ?? null)
  const error = ref<NormalizedApiError | null>(null)
  const status = ref<UiState>('loading')

  const isEmpty = computed(() => {
    if (!options.isEmpty) {
      const value = data.value
      if (value === null || value === undefined) return true
      if (Array.isArray(value)) return value.length === 0
      return false
    }
    return data.value === null || options.isEmpty(data.value)
  })

  async function execute(): Promise<T | null> {
    status.value = 'loading'
    error.value = null
    try {
      const result = await loader()
      data.value = result
      status.value = resolveStatus(result)
      return result
    } catch (e) {
      const normalized = normalizeError(e)
      error.value = normalized
      // A forbidden/past-session failure must not look like a server outage.
      status.value = normalized.forbidden || normalized.unauthenticated ? 'permission_denied' : 'error'
      return null
    }
  }

  /** Re-run without flickering the loading state (used by polling and refresh). */
  async function refresh(): Promise<T | null> {
    try {
      const result = await loader()
      data.value = result
      error.value = null
      status.value = resolveStatus(result)
      return result
    } catch (e) {
      const normalized = normalizeError(e)
      error.value = normalized
      status.value = normalized.forbidden || normalized.unauthenticated ? 'permission_denied' : 'error'
      return null
    }
  }

  /** Single place that decides success vs empty, used by both paths above. */
  function resolveStatus(result: T): UiState {
    if (options.isEmpty) return options.isEmpty(result) ? 'empty' : 'success'
    if (result === null || result === undefined) return 'empty'
    if (Array.isArray(result) && result.length === 0) return 'empty'
    return 'success'
  }

  const loading = computed(() => status.value === 'loading')

  if (options.immediate !== false) {
    void execute()
  }

  return { data, error, status, loading, isEmpty, execute, refresh }
}
