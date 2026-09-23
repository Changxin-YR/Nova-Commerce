/**
 * Unit-aware analytics value rendering (API_CONTRACT.md §8).
 *
 * THE HAZARD THIS EXISTS TO PREVENT
 *  The analytics envelope carries `unit: 'minor_currency' | 'count' | 'ratio'`. A single
 *  formatter that always prepends `¥` is the tempting shortcut, and it produces values that
 *  look completely plausible while being wrong:
 *
 *      refund.rate = 0.12   ->  "¥0.12"    (a 12% rate shown as twelve fen)
 *      sales.order_count = 137  ->  "¥1.37" (137 orders shown as one yuan thirty-seven)
 *
 *  Neither throws, neither looks odd at a glance, and both would sail through review. So the
 *  unit is read, never assumed, and `minor_currency` is the ONLY branch that becomes money.
 *
 * This module returns a discriminated result rather than a string, so callers must decide how
 * to render money: it goes through `<PriceText>` (the single price renderer), not through a
 * string that already contains a formatted number.
 */

import type { AnalyticsUnit } from '@/types/frozen-contract'

export type AnalyticsDisplay =
  | { kind: 'money'; amount: number }
  | { kind: 'count'; text: string }
  | { kind: 'ratio'; text: string; percent: number }

/** A ratio is a fraction (0.12 = 12%). Accepts and normalises percentage-style input too. */
export function ratioToPercent(value: number, options: { assumePercentWhenOverOne?: boolean } = {}): number {
  const { assumePercentWhenOverOne = true } = options
  if (!Number.isFinite(value)) return 0
  // Guard the common backend slip of returning `12` instead of `0.12`: a rate above 1 is
  // almost never a fraction, and rendering 1200% would be worse than rendering 12%.
  if (assumePercentWhenOverOne && Math.abs(value) > 1) return value
  return value * 100
}

/**
 * Format one analytics value according to the frozen unit.
 *
 * `grouping` is applied to counts (137 000 orders is common) but never to money, whose
 * grouping belongs to `<PriceText>`.
 */
export function formatAnalyticsValue(
  value: number,
  unit: AnalyticsUnit,
  options: { decimals?: number } = {},
): AnalyticsDisplay {
  const { decimals = 1 } = options

  if (unit === 'minor_currency') {
    // Hand the INTEGER straight through. The caller renders it with <PriceText>, so no
    // float ever touches a monetary value.
    return { kind: 'money', amount: value }
  }

  if (unit === 'count') {
    const rounded = Math.round(value)
    const grouped = String(Math.abs(rounded)).replace(/\B(?=(\d{3})+(?!\d))/g, ',')
    return { kind: 'count', text: `${rounded < 0 ? '−' : ''}${grouped}` }
  }

  const percent = ratioToPercent(value)
  // Trim a trailing `.0` so "12%" does not read as "12.0%".
  const rendered = Number.isInteger(percent) ? String(percent) : percent.toFixed(decimals)
  return { kind: 'ratio', text: `${rendered}%`, percent }
}

/** Compact label for a summary/dimension card, used in tooltips and table headers. */
export function unitLabel(unit: AnalyticsUnit): string {
  switch (unit) {
    case 'minor_currency':
      return '金额'
    case 'count':
      return '数量'
    case 'ratio':
      return '比例'
    default:
      return '值'
  }
}

/**
 * The five frozen metrics (§120, §124) with their units.
 *
 * A metric appearing here with the WRONG unit is the exact failure this map prevents, so it is
 * keyed by the frozen metric name rather than inferred from the response.
 */
export const METRIC_UNITS: Record<string, AnalyticsUnit> = {
  'sales.gmv': 'minor_currency',
  'sales.order_count': 'count',
  'inventory.turnover': 'count',
  'product.performance': 'count',
  'refund.rate': 'ratio',
}

/**
 * Cross-check the server's declared unit against the frozen expectation.
 * Returns a warning string when they disagree — the response is still authoritative, but a
 * mismatch means a contract bug worth surfacing rather than silently rendering.
 */
export function checkMetricUnit(metric: string, declared: AnalyticsUnit): string | null {
  const expected = METRIC_UNITS[metric]
  if (!expected) return null
  if (expected === declared) return null
  return `指标 ${metric} 的单位为 ${expected}，服务端返回 ${declared}`
}
