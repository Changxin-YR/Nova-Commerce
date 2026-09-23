/**
 * `useAsyncState` — the §108 state derivation every console page depends on.
 *
 * The paged case is the one that mattered in practice: the API returns
 * `{ items: [], meta: { total: 0 } }`, and an empty *array* check misses that shape, so every
 * list page rendered an empty table with a "共 0 条" pager instead of the Empty state. The
 * console Orders spec caught it; these tests pin it down at the source.
 */

import { describe, expect, it, vi } from 'vitest'
import { useAsyncState, isEmptyPaged } from '@/composables/useAsyncState'

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

/** Flush microtasks so the loader's promise settles. */
const tick = () => new Promise((resolve) => setTimeout(resolve, 0))

describe('isEmptyPaged', () => {
  it('detects the empty paged envelope', () => {
    expect(isEmptyPaged({ items: [], meta: { page: 1, page_size: 20, total: 0, total_pages: 0 } })).toBe(true)
  })

  it('does not treat a non-empty page as empty', () => {
    expect(isEmptyPaged({ items: [1], meta: {} })).toBe(false)
  })

  it('ignores shapes that are not paged payloads', () => {
    expect(isEmptyPaged([])).toBe(false)
    expect(isEmptyPaged(null)).toBe(false)
    expect(isEmptyPaged(undefined)).toBe(false)
    expect(isEmptyPaged('items')).toBe(false)
    expect(isEmptyPaged({ total: 0 })).toBe(false)
    expect(isEmptyPaged({ items: 'not-an-array' })).toBe(false)
  })
})

describe('useAsyncState — status derivation', () => {
  it('reports empty for an empty bare array', async () => {
    const { data, status } = useAsyncState(() => Promise.resolve<number[]>([]), { immediate: true })
    await tick()
    expect(status.value).toBe('empty')
    expect(data.value).toEqual([])
  })

  it('reports empty for an empty PAGED payload (the console regression)', async () => {
    const { status } = useAsyncState(
      () => Promise.resolve({ items: [] as string[], meta: { total: 0 } }),
      { immediate: true },
    )
    await tick()
    expect(status.value).toBe('empty')
  })

  it('reports success for a populated paged payload', async () => {
    const { status } = useAsyncState(
      () => Promise.resolve({ items: ['a'], meta: { total: 1 } }),
      { immediate: true },
    )
    await tick()
    expect(status.value).toBe('success')
  })

  it('reports empty for null and undefined results', async () => {
    const nullState = useAsyncState(() => Promise.resolve(null), { immediate: true })
    await tick()
    expect(nullState.status.value).toBe('empty')
  })

  it('honours a caller-supplied isEmpty predicate over the built-in heuristic', async () => {
    const { status } = useAsyncState(() => Promise.resolve({ items: ['a'], total: 99 }), {
      immediate: true,
      isEmpty: (result) => result.total === 0,
    })
    await tick()
    // The predicate wins even though `items` is populated.
    expect(status.value).toBe('success')

    const custom = useAsyncState(() => Promise.resolve({ items: ['a'], total: 0 }), {
      immediate: true,
      isEmpty: (result) => result.total === 0,
    })
    await tick()
    expect(custom.status.value).toBe('empty')
  })

  it('maps a forbidden failure to permission_denied, not error (§104 UI consequence)', async () => {
    const { status, error } = useAsyncState(
      () =>
        Promise.reject({
          code: 20_009,
          message: '当前角色权限不足',
          traceId: 't',
          httpStatus: 403,
          retryable: false,
          forbidden: true,
          unauthenticated: false,
        }),
      { immediate: true },
    )
    await tick()
    expect(status.value).toBe('permission_denied')
    expect(error.value?.forbidden).toBe(true)
  })

  it('maps a plain failure to error', async () => {
    const { status } = useAsyncState(() => Promise.reject(new Error('boom')), { immediate: true })
    await tick()
    expect(status.value).toBe('error')
  })

  it('refresh() re-runs the loader without passing through loading', async () => {
    const loader = vi
      .fn<() => Promise<string[]>>()
      .mockResolvedValueOnce(['a'])
      .mockResolvedValueOnce([])

    const { status, refresh, execute } = useAsyncState(loader, { immediate: false })
    await execute()
    expect(status.value).toBe('success')

    const refreshed = refresh()
    // Status flips to empty only once the new payload arrives — not to 'loading' in between.
    expect(status.value).toBe('success')
    await refreshed
    expect(status.value).toBe('empty')
    expect(loader).toHaveBeenCalledTimes(2)
  })

  it('returns null from execute() on failure so callers can branch', async () => {
    const { execute } = useAsyncState(() => Promise.reject(new Error('nope')), { immediate: false })
    await expect(execute()).resolves.toBeNull()
  })

  it('resolves the deferred value (guards against a loader that never settles)', async () => {
    const gate = deferred<string[]>()
    const state = useAsyncState(() => gate.promise, { immediate: true })
    expect(state.status.value).toBe('loading')
    gate.resolve(['x'])
    await tick()
    expect(state.status.value).toBe('success')
  })
})
