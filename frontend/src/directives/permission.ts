/**
 * `v-permission` / `v-can` directive (§104, REQ-FE-006).
 *
 * !!! THE BACKEND IS THE REAL AUTHORITY !!!
 * This directive only removes controls the current user cannot use, so the UI does
 * not offer actions that would be rejected with `FORBIDDEN` / `INSUFFICIENT_PERMISSION`.
 * It is NOT a security boundary: anyone can re-add the element in devtools and any
 * request can be replayed with curl. NEVER rely on it to protect data — every
 * endpoint enforces its own authorization server-side.
 *
 * Usage:
 *   <el-button v-permission="'product:write'">上架</el-button>
 *   <el-button v-can:any="['order:write', 'order:admin']">发货</el-button>
 *   <div v-permission.all="['a', 'b']">要求同时具备</div>
 *
 * Semantics: default modifier is `.any` (one sufficient code); `.all` requires all.
 */

import type { Directive, DirectiveBinding } from 'vue'
import { usePermissionStore } from '@/stores/permission'

type PermissionValue = string | string[] | undefined
type Mode = 'any' | 'all'

/** Resolve the permission store lazily: the directive may run before pinia installs. */
function store() {
  return usePermissionStore()
}

function normalizeCodes(value: PermissionValue): string[] {
  if (Array.isArray(value)) return value.filter((v): v is string => typeof v === 'string' && v.length > 0)
  if (typeof value === 'string' && value.length > 0) return [value]
  return []
}

function modeOf(binding: DirectiveBinding<PermissionValue>): Mode {
  return binding.modifiers.all ? 'all' : 'any'
}

function allowed(value: PermissionValue, mode: Mode): boolean {
  const codes = normalizeCodes(value)
  if (codes.length === 0) return true
  const permissions = store()
  return mode === 'all' ? permissions.hasAll(codes) : permissions.hasAny(codes)
}

/**
 * Hide by REMOVING the node, not by `display: none`: a hidden-but-present control
 * still leaks what exists, and screen readers can reach it. Removal is also what
 * makes the directive testable.
 */
function apply(el: HTMLElement, binding: DirectiveBinding<PermissionValue>): void {
  const mode = modeOf(binding)
  if (!allowed(binding.value, mode)) {
    const parent = el.parentNode
    if (parent && el.dataset.permissionRemoved !== 'true') {
      // Keep a marker so `unmounted` bookkeeping and re-mounts stay predictable.
      el.dataset.permissionRemoved = 'true'
      parent.removeChild(el)
    }
    return
  }
  // Re-enable a previously denied element whose permissions have since loaded.
  if (el.dataset.permissionRemoved === 'true') {
    delete el.dataset.permissionRemoved
    if (el.hasAttribute('disabled')) el.removeAttribute('disabled')
  }
}

export const vPermission: Directive<HTMLElement, PermissionValue> = {
  mounted: apply,
  updated: apply,
}

export const vCan = vPermission

/** Register both names. Called once from `src/main.ts`. */
export function registerPermissionDirectives(app: {
  directive: (name: string, directive: Directive<HTMLElement, PermissionValue>) => void
}): void {
  app.directive('permission', vPermission)
  app.directive('can', vCan)
}

/** Test seam: pure decision function, no DOM required. */
export function __isAllowed(
  value: PermissionValue,
  granted: readonly string[],
  mode: Mode = 'any',
): boolean {
  const codes = normalizeCodes(value)
  if (codes.length === 0) return true
  return mode === 'all'
    ? codes.every((code) => granted.includes(code))
    : codes.some((code) => granted.includes(code))
}
