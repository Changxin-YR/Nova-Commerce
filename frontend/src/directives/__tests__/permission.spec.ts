/**
 * `v-permission` / `v-can` directive tests (§104).
 *
 * The assertion that matters is that a denied control is REMOVED from the DOM, not
 * merely hidden — and the accompanying comment explains why this is UX only.
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent } from 'vue'
import { registerPermissionDirectives, __isAllowed } from '@/directives/permission'
import { usePermissionStore } from '@/stores/permission'

/**
 * Minimal host that mirrors how `main.ts` registers the directives.
 *
 * The store must be seeded BEFORE mounting: `mounted` is when the directive makes
 * its decision, and the point of this spec is to observe that decision.
 */
function mountWithPermission(template: string, granted: string[], roles: string[] = []) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const store = usePermissionStore()
  store.permissions = [...granted]
  store.roles = [...roles]

  const Host = defineComponent({ template })
  const directive = registerDirective()
  const wrapper = mount(Host, {
    global: {
      plugins: [pinia],
      directives: { permission: directive, can: directive },
    },
  })
  return { wrapper, store }
}

/** Extract the single directive object from the registrar without a Vue app. */
function registerDirective() {
  let captured: unknown
  registerPermissionDirectives({
    directive: (_name: string, directive: unknown) => {
      captured = directive
    },
  })
  return captured as never
}

describe('permission directive', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('removes the element when the code is not granted', () => {
    const { wrapper } = mountWithPermission(
      `<div><button id="del" v-permission="'product:delete'">删除</button><span id="keep">x</span></div>`,
      ['product:read'],
    )
    expect(wrapper.find('#del').exists()).toBe(false)
    expect(wrapper.find('#keep').exists()).toBe(true)
  })

  it('keeps the element when the code IS granted', () => {
    const { wrapper } = mountWithPermission(
      `<div><button id="del" v-permission="'product:delete'">删除</button></div>`,
      ['product:delete'],
    )
    expect(wrapper.find('#del').exists()).toBe(true)
  })

  it('treats an empty value as "no requirement" and keeps the element', () => {
    const { wrapper } = mountWithPermission(
      `<div><button id="open" v-permission="''">公开操作</button></div>`,
      [],
    )
    expect(wrapper.find('#open').exists()).toBe(true)
  })

  it('supports multiple codes with .any (default) semantics', () => {
    const { wrapper } = mountWithPermission(
      `<div><button id="ship" v-permission="['order:write', 'order:admin']">发货</button></div>`,
      ['order:write'],
    )
    expect(wrapper.find('#ship').exists()).toBe(true)
  })

  it('supports .all semantics', () => {
    const { wrapper } = mountWithPermission(
      `<div><button id="both" v-permission.all="['order:write', 'refund:write']">退款</button></div>`,
      ['order:write'],
    )
    expect(wrapper.find('#both').exists()).toBe(false)
  })

  it('works under the v-can alias too', () => {
    const { wrapper } = mountWithPermission(
      `<div><button id="c" v-can="'analytics:read'">报表</button></div>`,
      ['analytics:read'],
    )
    expect(wrapper.find('#c').exists()).toBe(true)
  })

  it('removes the node, so an unprivileged user cannot reach it via the DOM', () => {
    const { wrapper } = mountWithPermission(
      `<div><button id="hidden-action" v-permission="'system:write'">危险</button></div>`,
      [],
    )
    expect(wrapper.html()).not.toContain('hidden-action')
    expect(wrapper.html()).not.toContain('危险')
  })
})

describe('__isAllowed (pure decision function)', () => {
  it('handles any/all/empty cases', () => {
    expect(__isAllowed('a', ['a'])).toBe(true)
    expect(__isAllowed('a', ['b'])).toBe(false)
    expect(__isAllowed(['a', 'b'], ['b'], 'any')).toBe(true)
    expect(__isAllowed(['a', 'b'], ['b'], 'all')).toBe(false)
    expect(__isAllowed(['a', 'b'], ['a', 'b'], 'all')).toBe(true)
    expect(__isAllowed([], ['a'], 'all')).toBe(true)
    expect(__isAllowed(undefined, [], 'any')).toBe(true)
  })
})
