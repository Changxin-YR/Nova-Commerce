/**
 * `<PriceText>` — the minor→major conversion and the display contract.
 *
 * This is the component every page uses to show money, so these assertions are the
 * project's price-rendering contract. They check both the arithmetic (integer cents in,
 * grouped major units out) and the visual structure (symbol / integer / cents are separate
 * elements, which is what makes the "big integer, small symbol" convention possible).
 */

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import PriceText from '@/components/ui/PriceText.vue'

function text(root: { text: () => string }): string {
  return root.text().replace(/\s+/g, ' ').trim()
}

describe('PriceText — minor units to major units', () => {
  it('renders 299900 cents as ¥2,999.00', () => {
    const wrapper = mount(PriceText, { props: { amount: 299900 } })
    expect(wrapper.find('.price__symbol').text()).toBe('¥')
    expect(wrapper.find('.price__integer').text()).toBe('2,999')
    expect(wrapper.find('.price__decimal').text()).toBe('.00')
  })

  it('keeps the cents padded so 5 cents never renders as .5', () => {
    const wrapper = mount(PriceText, { props: { amount: 5 } })
    expect(wrapper.find('.price__integer').text()).toBe('0')
    expect(wrapper.find('.price__decimal').text()).toBe('.05')
  })

  it('groups thousands above 4 digits', () => {
    const wrapper = mount(PriceText, { props: { amount: 1234567890 } })
    expect(wrapper.find('.price__integer').text()).toBe('12,345,678')
    expect(wrapper.find('.price__decimal').text()).toBe('.90')
  })

  it('can drop grouping for narrow table cells', () => {
    const wrapper = mount(PriceText, { props: { amount: 299900, grouping: false } })
    expect(wrapper.find('.price__integer').text()).toBe('2999')
  })

  it('renders a negative amount with a sign rather than a stray hyphen inside the digits', () => {
    const wrapper = mount(PriceText, { props: { amount: -2500 } })
    expect(wrapper.find('.price__integer').text()).toBe('−25')
    expect(wrapper.find('.price__decimal').text()).toBe('.00')
  })

  it('renders zero explicitly instead of leaving the field blank', () => {
    const wrapper = mount(PriceText, { props: { amount: 0 } })
    expect(wrapper.find('.price__integer').text()).toBe('0')
    expect(wrapper.find('.price__decimal').text()).toBe('.00')
  })

  it('omits the cents when asked, without rounding the integer up', () => {
    // 299999 cents = 2,999.99 yuan. Truncating keeps the displayed price at or below the
    // real charge: a checkout must never show more than the server will take.
    const wrapper = mount(PriceText, { props: { amount: 299999, showDecimal: false } })
    expect(wrapper.find('.price__integer').text()).toBe('2,999')
    expect(wrapper.find('.price__decimal').exists()).toBe(false)
  })

  it('can hide the currency symbol for columns with a "金额" header', () => {
    const wrapper = mount(PriceText, { props: { amount: 100, noSymbol: true } })
    expect(wrapper.find('.price__symbol').exists()).toBe(false)
    expect(wrapper.find('.price__integer').text()).toBe('1')
  })
})

describe('PriceText — original (list) price', () => {
  it('shows a struck-through original price only when it is actually higher', () => {
    const higher = mount(PriceText, { props: { amount: 299900, originalAmount: 399900 } })
    expect(higher.find('.price__original').exists()).toBe(true)
    expect(higher.find('.price__original').text()).toContain('3,999')

    const lower = mount(PriceText, { props: { amount: 299900, originalAmount: 199900 } })
    expect(lower.find('.price__original').exists()).toBe(false)
  })

  it('hides the original price when none was supplied', () => {
    const wrapper = mount(PriceText, { props: { amount: 299900 } })
    expect(wrapper.find('.price__original').exists()).toBe(false)
  })
})

describe('PriceText — accessibility + theming', () => {
  it('exposes one readable sentence to assistive tech', () => {
    const wrapper = mount(PriceText, { props: { amount: 299900 } })
    const sr = wrapper.find('.price__sr')
    expect(sr.exists()).toBe(true)
    expect(text(sr)).toContain('2,999.00 元')
    // The decorative symbol is hidden so it is not read as "yen".
    expect(wrapper.find('.price__symbol').attributes('aria-hidden')).toBe('true')
  })

  it('applies the size scale class and the muted variant', () => {
    const lg = mount(PriceText, { props: { amount: 100, size: 'xl' } })
    expect(lg.classes()).toContain('price--xl')

    const muted = mount(PriceText, { props: { amount: 100, muted: true } })
    expect(muted.classes()).toContain('price--muted')
  })
})
