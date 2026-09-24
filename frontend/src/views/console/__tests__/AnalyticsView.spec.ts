import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AnalyticsView from '@/views/console/AnalyticsView.vue'

const metric = vi.fn()
vi.mock('@/api', () => ({ analyticsApi: { metric: (...args: unknown[]) => metric(...args) } }))

describe('merchant analytics page', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    metric.mockImplementation(async (name: string) => {
      const units: Record<string, 'minor_currency' | 'count' | 'ratio'> = {
        'sales.gmv': 'minor_currency',
        'sales.order_count': 'count',
        'inventory.turnover': 'ratio',
        'product.performance': 'minor_currency',
        'refund.rate': 'ratio',
      }
      const values: Record<string, number> = {
        'sales.gmv': 1999,
        'sales.order_count': 1,
        'inventory.turnover': 0.75,
        'product.performance': 1999,
        'refund.rate': 0.12,
      }
      return {
        metric: name, unit: units[name],
        period: { from: '2026-09-24', to: '2026-09-24', granularity: 'day' },
        series: [{ bucket: '2026-09-24', value: values[name] }],
        summary: { total: values[name], average: values[name], change_ratio: 0 },
        dimensions: name === 'product.performance'
          ? [{ key: 'sku_id', label: 'Top SKU', value: 'Phone (#42)' }] : [],
      }
    })
  })

  it('loads all five metrics and renders money, turnover and refund rate with their units', async () => {
    const wrapper = mount(AnalyticsView, { global: { stubs: { ApexChart: true } } })
    await flushPromises()
    expect(metric.mock.calls.map((call) => call[0]).sort()).toEqual([
      'inventory.turnover', 'product.performance', 'refund.rate',
      'sales.gmv', 'sales.order_count',
    ])
    expect(wrapper.text()).toContain('0.75 次')
    expect(wrapper.text()).toContain('12%')
    expect(wrapper.text()).toContain('Phone (#42)')
    expect(wrapper.text()).toContain('¥19.99')
  })
})
