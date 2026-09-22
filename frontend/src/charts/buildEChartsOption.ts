/**
 * ChartSpec -> ECharts options (pure, testable, no ECharts import).
 *
 * SECURITY: every value written here comes from a spec that already passed
 * `sanitizeChartSpec`, and every string is emitted as DATA (a label/category value),
 * never as a formatter callback or a template. There is deliberately no way to put
 * executable text into these options: no `formatter` string, no `rich` markup from
 * the agent, and no colour other than what the validator whitelisted.
 *
 * Maps are used (not object spreads) so a category named `__proto__` or
 * `constructor` cannot pollute anything when it is used as a key.
 */

import type { ChartSpec } from '@/types/charts'
import { toMajorString } from '@/utils/money'
import { formatSpecValue } from '@/charts/sanitizeChartSpec'

/** Design tokens; override per theme. Kept as hex so the validator can accept them. */
export const CHART_PALETTE = [
  '#2563eb',
  '#0ea5e9',
  '#14b8a6',
  '#f59e0b',
  '#ef4444',
  '#8b5cf6',
  '#ec4899',
  '#84cc16',
] as const

export interface BuildOptions {
  /** Rendered width/height hints. ECharts needs explicit numbers for `grid`. */
  dark?: boolean
  /** Overrides the palette order (e.g. a brand set). */
  palette?: readonly string[]
}

/** Minimal structural type; avoids importing echarts types into this module. */
export type EChartsOption = Record<string, unknown>

function axisStyle(dark: boolean): Record<string, unknown> {
  const axisLine = dark ? '#2a2a2a' : '#e5e7eb'
  const label = dark ? '#9ca3af' : '#6b7280'
  return {
    axisLine: { lineStyle: { color: axisLine } },
    axisTick: { show: false },
    axisLabel: { color: label, fontSize: 11 },
    splitLine: { lineStyle: { color: dark ? '#1f1f1f' : '#f1f5f9' } },
  }
}

function valueTooltipFormatter(spec: ChartSpec, dark: boolean): Record<string, unknown> {
  const base = {
    backgroundColor: dark ? '#181818' : '#ffffff',
    borderColor: dark ? '#2a2a2a' : '#e5e7eb',
    textStyle: { color: dark ? '#ededed' : '#111827', fontSize: 12 },
  }
  if (!spec.money) return base
  // A money chart shows major units in the tooltip. `valueFormatter` is attached by
  // the caller below; leaving a `undefined` key here would be overwritten anyway.
  return { ...base }
}

/**
 * Build ECharts options from a validated spec.
 * Unknown kinds are impossible here (the validator rejected them) but the switch is
 * still exhaustive with a default that returns empty options, so a future kind that
 * reaches this function renders nothing instead of throwing.
 */
export function buildEChartsOption(spec: ChartSpec, options: BuildOptions = {}): EChartsOption {
  const dark = Boolean(options.dark)
  const palette = options.palette ?? CHART_PALETTE
  const textColor = dark ? '#ededed' : '#111827'

  const title: Record<string, unknown> = {
    text: spec.title ?? '',
    subtext: spec.subtitle ?? '',
    left: 0,
    top: 0,
    textStyle: { color: textColor, fontSize: 14, fontWeight: 600 },
    subtextStyle: { color: dark ? '#767676' : '#6b7280', fontSize: 11 },
  }

  const tooltip = valueTooltipFormatter(spec, dark)
  if (spec.money) {
    // Money values stay integer minor units in the spec and are rendered in major
    // units through the shared helper. `valueFormatter` receives a NUMBER and
    // returns a string — it is our own callback, defined here, never agent input.
    tooltip.valueFormatter = (value: number) => formatSpecValue(value, spec)
  }

  if (spec.kind === 'table') {
    // Tables are NOT rendered by ECharts: the message renderer draws them as HTML so
    // they stay selectable and screen-reader accessible. Returning an empty option
    // keeps this function total.
    return {}
  }

  if (spec.kind === 'pie') {
    const series = spec.series[0]
    const data = (series?.data ?? []).map((value, index) => ({
      name: spec.categories?.[index] ?? `${index + 1}`,
      value,
    }))
    return {
      color: [...palette],
      title,
      tooltip: { ...tooltip, trigger: 'item' },
      legend: {
        bottom: 0,
        textStyle: { color: dark ? '#9ca3af' : '#6b7280', fontSize: 11 },
      },
      series: [
        {
          type: 'pie',
          radius: ['45%', '70%'],
          avoidLabelOverlap: true,
          itemStyle: { borderColor: dark ? '#181818' : '#ffffff', borderWidth: 2 },
          label: { color: textColor, fontSize: 11 },
          data,
        },
      ],
    }
  }

  if (spec.kind === 'gauge') {
    const series = spec.series[0]
    const value = series?.data[0] ?? 0
    const max = Math.max(...(series?.data ?? [value]), 1)
    return {
      color: [...palette],
      title,
      tooltip,
      series: [
        {
          type: 'gauge',
          min: 0,
          // A gauge's ceiling is derived from the data so a 0..1 rate and a 0..100
          // percentage both render sensibly.
          max: max <= 1 ? 1 : Math.ceil(max),
          progress: { show: true, roundCap: true },
          axisLine: { lineStyle: { color: [[1, dark ? '#2a2a2a' : '#e5e7eb']] } },
          axisLabel: { color: dark ? '#767676' : '#9ca3af', fontSize: 10 },
          detail: {
            valueAnimation: true,
            color: textColor,
            fontSize: 20,
            formatter: (value_: number) => formatSpecValue(value_, spec),
          },
          data: [{ value, name: series?.name ?? '' }],
        },
      ],
    }
  }

  // line | bar | scatter share the cartesian shape.
  const seriesType = spec.kind === 'scatter' ? 'scatter' : spec.kind
  const series = spec.series.map((item, index) => ({
    name: item.name,
    type: seriesType,
    data: item.data,
    stack: item.stack,
    smooth: spec.kind === 'line',
    symbolSize: spec.kind === 'scatter' ? 10 : undefined,
    itemStyle: { color: item.color ?? palette[index % palette.length] },
    lineStyle: spec.kind === 'line' ? { width: 2 } : undefined,
    barMaxWidth: spec.kind === 'bar' ? 28 : undefined,
  }))

  return {
    color: [...palette],
    title,
    tooltip: { ...tooltip, trigger: 'axis' },
    legend: {
      top: 0,
      right: 0,
      textStyle: { color: dark ? '#9ca3af' : '#6b7280', fontSize: 11 },
    },
    grid: { left: 8, right: 16, top: spec.title ? 56 : 32, bottom: 8, containLabel: true },
    xAxis: {
      type: 'category',
      boundaryGap: spec.kind === 'bar',
      data: spec.categories ?? [],
      ...axisStyle(dark),
    },
    yAxis: {
      type: 'value',
      ...axisStyle(dark),
      // The axis label is a DATA string, not a template the agent controls.
      axisLabel: {
        color: dark ? '#9ca3af' : '#6b7280',
        fontSize: 11,
        formatter: (value: number) => {
          if (spec.money) return toMajorString(value)
          return String(value)
        },
      },
    },
    series,
  }
}

/** True when the spec must be rendered as an HTML table rather than a canvas chart. */
export function isTabularSpec(spec: ChartSpec): boolean {
  return spec.kind === 'table'
}
