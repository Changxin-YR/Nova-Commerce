/**
 * Unit-aware analytics rendering (API_CONTRACT.md §8).
 *
 * The point of these tests is the WRONG-BUT-PLAUSIBLE cases: a rate shown as money and a count
 * shown as money. Both render without error, both look reasonable to a reviewer skimming a
 * screenshot, and both are lies. Only a unit-aware test catches them.
 */

import { describe, expect, it } from 'vitest'
import {
  METRIC_UNITS,
  checkMetricUnit,
  formatAnalyticsValue,
  ratioToPercent,
  unitLabel,
} from '@/domain/analytics/unit'

describe('formatAnalyticsValue — minor_currency stays money, and stays INTEGER', () => {
  it('hands the raw minor-unit value through for <PriceText> to render', () => {
    const result = formatAnalyticsValue(1289900, 'minor_currency')
    expect(result).toEqual({ kind: 'money', amount: 1289900 })
  })

  it('never divides by 100 or produces a formatted money string itself', () => {
    const result = formatAnalyticsValue(299900, 'minor_currency')
    // A money result carries a NUMBER, so no float formatting can happen here.
    expect(result.kind).toBe('money')
    if (result.kind === 'money') {
      expect(Number.isInteger(result.amount)).toBe(true)
      expect(result.amount).toBe(299900)
    }
  })

  it('does not treat a zero amount as empty', () => {
    expect(formatAnalyticsValue(0, 'minor_currency')).toEqual({ kind: 'money', amount: 0 })
  })
})

describe('formatAnalyticsValue — count is NEVER rendered as currency', () => {
  it('renders an order count as a grouped integer', () => {
    const result = formatAnalyticsValue(137, 'count')
    expect(result).toEqual({ kind: 'count', text: '137' })
  })

  it('groups large counts', () => {
    expect(formatAnalyticsValue(137000, 'count')).toEqual({ kind: 'count', text: '137,000' })
  })

  it('rounds a fractional count instead of showing cents', () => {
    expect(formatAnalyticsValue(136.6, 'count')).toEqual({ kind: 'count', text: '137' })
  })

  it('renders a negative count with a minus sign', () => {
    expect(formatAnalyticsValue(-42, 'count')).toEqual({ kind: 'count', text: '−42' })
  })
})

describe('formatAnalyticsValue — ratio is a percentage, and the trap is explicit', () => {
  it('renders 0.12 as 12%, NOT as ¥0.12', () => {
    const result = formatAnalyticsValue(0.12, 'ratio')
    expect(result.kind).toBe('ratio')
    if (result.kind === 'ratio') {
      expect(result.text).toBe('12%')
      expect(result.percent).toBeCloseTo(12)
    }
    // The regression this guards: money formatting would have produced "¥0.12".
    expect(JSON.stringify(result)).not.toContain('¥')
  })

  it('renders a zero ratio as 0%, not as an empty field', () => {
    expect(formatAnalyticsValue(0, 'ratio')).toEqual({ kind: 'ratio', text: '0%', percent: 0 })
  })

  it('trims a trailing .0 so 0.5 reads as 50%', () => {
    expect(formatAnalyticsValue(0.5, 'ratio')).toEqual({ kind: 'ratio', text: '50%', percent: 50 })
  })

  it('keeps one decimal for ratios that need it', () => {
    const result = formatAnalyticsValue(0.1234, 'ratio')
    expect(result.kind).toBe('ratio')
    if (result.kind === 'ratio') expect(result.text).toBe('12.3%')
  })

  it('keeps a fraction above one instead of silently rescaling it', () => {
    expect(ratioToPercent(1.2)).toBe(120)
    expect(formatAnalyticsValue(1.2, 'ratio')).toEqual({ kind: 'ratio', text: '120%', percent: 120 })
  })

  it('treats a value at or below 1 as a fraction', () => {
    expect(ratioToPercent(1)).toBe(100)
    expect(ratioToPercent(0.25)).toBe(25)
  })

  it('tolerates a non-finite value instead of rendering NaN%', () => {
    expect(ratioToPercent(Number.NaN)).toBe(0)
  })
})

describe('the three units produce three genuinely different renderings', () => {
  it('a single numeric value renders differently per unit', () => {
    const asMoney = formatAnalyticsValue(1, 'minor_currency')
    const asCount = formatAnalyticsValue(1, 'count')
    const asRatio = formatAnalyticsValue(1, 'ratio')

    expect(asMoney.kind).toBe('money')
    expect(asCount).toEqual({ kind: 'count', text: '1' })
    expect(asRatio).toEqual({ kind: 'ratio', text: '100%', percent: 100 })
    // Same input, three outcomes — this is the whole point of reading `unit`.
    expect(new Set([asMoney.kind, asCount.kind, asRatio.kind]).size).toBe(3)
  })
})

describe('unitLabel', () => {
  it('labels each unit for headers and tooltips', () => {
    expect(unitLabel('minor_currency')).toBe('金额')
    expect(unitLabel('count')).toBe('数量')
    expect(unitLabel('ratio')).toBe('比例')
  })
})

describe('checkMetricUnit — catches a server/contract unit mismatch', () => {
  it('accepts the frozen unit for each of the five metrics', () => {
    for (const [metric, unit] of Object.entries(METRIC_UNITS)) {
      expect(checkMetricUnit(metric, unit), `${metric} should accept ${unit}`).toBeNull()
    }
  })

  it('flags refund.rate arriving as money — the dangerous case', () => {
    const warning = checkMetricUnit('refund.rate', 'minor_currency')
    expect(warning).toContain('refund.rate')
    expect(warning).toContain('ratio')
  })

  it('flags order_count arriving as money', () => {
    expect(checkMetricUnit('sales.order_count', 'minor_currency')).toContain('count')
  })

  it('flags gmv arriving as a ratio', () => {
    expect(checkMetricUnit('sales.gmv', 'ratio')).toContain('minor_currency')
  })

  it('returns null for a metric outside the frozen five', () => {
    expect(checkMetricUnit('some.future.metric', 'ratio')).toBeNull()
  })
})
