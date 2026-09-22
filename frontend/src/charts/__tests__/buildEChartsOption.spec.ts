/**
 * ChartSpec -> ECharts option tests (§102).
 *
 * These assert the security property end-to-end: after validation AND option
 * building, the object handed to ECharts is plain data plus OUR OWN callbacks —
 * nothing derived from agent strings.
 */

import { describe, expect, it } from 'vitest'
import { buildEChartsOption, CHART_PALETTE, isTabularSpec } from '@/charts/buildEChartsOption'
import { sanitizeChartSpec } from '@/charts/sanitizeChartSpec'
import type { ChartSpec } from '@/types/charts'

function spec(input: unknown): ChartSpec {
  const result = sanitizeChartSpec(input)
  if (!result.ok) throw new Error(`fixture failed validation: ${result.reason}`)
  return result.spec
}

describe('buildEChartsOption', () => {
  it('builds a line chart with categories and series', () => {
    const option = buildEChartsOption(
      spec({
        kind: 'line',
        title: 'GMV 趋势',
        categories: ['1月', '2月'],
        series: [{ name: 'GMV', data: [100, 200] }],
      }),
    )
    expect((option.xAxis as Record<string, unknown>).data).toEqual(['1月', '2月'])
    const series = option.series as Record<string, unknown>[]
    expect(series).toHaveLength(1)
    expect(series[0]?.type).toBe('line')
    expect(series[0]?.data).toEqual([100, 200])
  })

  it('builds a bar chart and a pie chart', () => {
    const bar = buildEChartsOption(
      spec({ kind: 'bar', categories: ['a'], series: [{ name: 's', data: [1] }] }),
    )
    expect((bar.series as Record<string, unknown>[])[0]?.type).toBe('bar')

    const pie = buildEChartsOption(
      spec({ kind: 'pie', categories: ['移动端', '桌面端'], series: [{ name: '占比', data: [70, 30] }] }),
    )
    const pieSeries = pie.series as Record<string, unknown>[]
    expect(pieSeries[0]?.type).toBe('pie')
    expect(pieSeries[0]?.data).toEqual([
      { name: '移动端', value: 70 },
      { name: '桌面端', value: 30 },
    ])
  })

  it('renders a gauge from a single value', () => {
    const option = buildEChartsOption(
      spec({ kind: 'gauge', series: [{ name: '支付转化率', data: [0.62] }] }),
    )
    const gauge = (option.series as Record<string, unknown>[])[0]
    expect(gauge?.type).toBe('gauge')
    expect(gauge?.max).toBe(1)
  })

  it('returns empty options for a table spec (tables are HTML, not canvas)', () => {
    const tableSpec = spec({
      kind: 'table',
      columns: [{ key: 'a', title: 'A' }],
      rows: [{ a: 1 }],
    })
    expect(buildEChartsOption(tableSpec)).toEqual({})
    expect(isTabularSpec(tableSpec)).toBe(true)
  })

  it('formats money axes in MAJOR units without touching the underlying data', () => {
    const option = buildEChartsOption(
      spec({
        kind: 'bar',
        categories: ['a'],
        money: true,
        series: [{ name: 'GMV', data: [123456] }],
      }),
    )
    const yAxis = option.yAxis as { axisLabel: { formatter: (v: number) => string } }
    expect(yAxis.axisLabel.formatter(123456)).toBe('1234.56')
    // The raw integer minor units stay in the series data for exactness.
    expect((option.series as Record<string, unknown>[])[0]?.data).toEqual([123456])
  })

  it('uses a safe per-series colour and falls back to the palette', () => {
    const option = buildEChartsOption(
      spec({
        kind: 'line',
        categories: ['a'],
        series: [
          { name: 'branded', data: [1], color: '#ef4444' },
          { name: 'default', data: [2] },
        ],
      }),
    )
    const series = option.series as { itemStyle: { color: string } }[]
    expect(series[0]?.itemStyle.color).toBe('#ef4444')
    expect(series[1]?.itemStyle.color).toBe(CHART_PALETTE[1])
  })

  it('emits NO executable text derived from agent strings', () => {
    const hostile = spec({
      kind: 'line',
      title: 'a\'; alert(1); //',
      categories: ['</script><script>alert(1)</script>'],
      series: [{ name: 'name"><img src=x onerror=alert(1)>', data: [1] }],
    })
    const option = buildEChartsOption(hostile)
    const serialized = JSON.stringify(option)
    // The dangerous text is present ONLY as an inert string value (that is fine and
    // expected: it is a label). What must not exist is a formatter/template channel.
    expect(option).not.toHaveProperty('formatter')
    const stringChannels: string[] = []
    walk(option, (key, value) => {
      if (key === 'formatter' && typeof value === 'string') stringChannels.push(value)
    })
    expect(stringChannels).toEqual([])
    // Serializing must not throw, i.e. the graph is acyclic plain data.
    expect(typeof serialized).toBe('string')
  })

  it('keeps a category literally named __proto__ as data only', () => {
    const tricky = spec({
      kind: 'bar',
      categories: ['__proto__', 'constructor'],
      series: [{ name: 's', data: [1, 2] }],
    })
    const option = buildEChartsOption(tricky)
    expect((option.xAxis as Record<string, unknown>).data).toEqual(['__proto__', 'constructor'])
    expect(({} as Record<string, unknown>).polluted).toBeUndefined()
  })
})

function walk(value: unknown, visit: (key: string, value: unknown) => void, depth = 0): void {
  if (depth > 12 || !value || typeof value !== 'object') return
  for (const [key, child] of Object.entries(value as Record<string, unknown>)) {
    visit(key, child)
    walk(child, visit, depth + 1)
  }
}
