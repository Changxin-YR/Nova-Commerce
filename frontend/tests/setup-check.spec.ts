import { defineComponent, h } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

const Spike = defineComponent({
  name: 'Spike',
  setup() {
    return () => h('div', { class: 'spike' }, 'ok')
  },
})

describe('toolchain spike', () => {
  it('mounts a Vue 3 component in jsdom', () => {
    const wrapper = mount(Spike)
    expect(wrapper.text()).toBe('ok')
  })
})
