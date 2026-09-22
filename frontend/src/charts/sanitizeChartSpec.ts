/**
 * ChartSpec validator (§102, REQ-FE-004).
 *
 * THE INVARIANT
 *  The agent emits a declarative data object; the frontend turns it into ECharts
 *  options. The agent NEVER emits JavaScript, and this module NEVER evaluates it:
 *  there is no `eval`, no `new Function`, no `setTimeout(string)`, no template
 *  compilation and no `innerHTML` anywhere in the chart path. A spec that fails
 *  validation is rendered as an ERROR BLOCK — never "best effort".
 *
 * WHAT IS DEFENDED AGAINST
 *  1. Code smuggling — any `function` value is dropped; the output is plain JSON
 *     data only, so a payload like `{kind:'line', series:[{name:'x', data:[...],
 *     formatter: () => fetch('//evil')}]}` loses its formatter.
 *  2. Prototype pollution — `__proto__`, `constructor`, `prototype` keys are
 *     skipped before they can be assigned, and objects are built fresh with plain
 *     literals rather than spread from input.
 *  3. Resource exhaustion — categories, series, rows and string lengths are capped,
 *     so a 10-million-point "chart" cannot freeze the tab.
 *  4. Colour injection — `color` accepts only a hex colour or a CSS variable name,
 *     never `url(...)`, `expression(...)` or anything else a style parser would run.
 *  5. Markup injection — every string is stripped of control characters; the
 *     renderer binds them as TEXT, and this is the second layer that keeps a
 *     `<script>` payload from ever looking like markup.
 */

import {
  CHART_KINDS,
  type ChartKind,
  type ChartSeries,
  type ChartSpec,
  type ChartSpecResult,
} from '@/types/charts'
import { toMajorString } from '@/utils/money'

/** Caps. Chosen to be generous for real analytics, tiny for an attacker. */
export const LIMITS = {
  categories: 500,
  series: 20,
  dataPointsPerSeries: 500,
  tableRows: 1000,
  columns: 30,
  stringLength: 200,
} as const

const DANGEROUS_KEYS = new Set(['__proto__', 'constructor', 'prototype'])

/** Hex colours (#rgb/#rrggbb/#rrggbbaa) or a `var(--token)` reference. */
const HEX_COLOR = /^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/
const VAR_COLOR = /^var\(--[a-zA-Z0-9-_]+\)$/

function isPlainObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null) return false
  if (Array.isArray(value)) return false
  const proto = Object.getPrototypeOf(value) as unknown
  return proto === Object.prototype || proto === null
}

/** Control characters stripped so nothing can smuggle newlines/ANSI into a label. */
function safeString(value: unknown, maxLength: number = LIMITS.stringLength): string | undefined {
  if (typeof value !== 'string') return undefined
  // eslint-disable-next-line no-control-regex
  const cleaned = value.replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]/g, '').trim()
  if (!cleaned) return undefined
  return cleaned.length > maxLength ? `${cleaned.slice(0, maxLength)}…` : cleaned
}

/** Finite numbers only: NaN/Infinity would break ECharts layout silently. */
function safeNumber(value: unknown): number | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined
  return value
}

function safeColor(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  const trimmed = value.trim()
  if (HEX_COLOR.test(trimmed) || VAR_COLOR.test(trimmed)) return trimmed
  return undefined
}

/** Read only OWN enumerable properties, skipping dangerous keys. */
function ownEntries(value: Record<string, unknown>): [string, unknown][] {
  const entries: [string, unknown][] = []
  for (const key of Object.keys(value)) {
    if (DANGEROUS_KEYS.has(key)) continue
    entries.push([key, value[key]])
  }
  return entries
}

function sanitizeSeries(raw: unknown, warnings: string[], index: number): ChartSeries | null {
  if (!isPlainObject(raw)) {
    warnings.push(`series[${index}] 不是对象，已跳过`)
    return null
  }
  const fields = new Map(ownEntries(raw))
  const name = safeString(fields.get('name'))
  if (!name) {
    warnings.push(`series[${index}] 缺少名称，已跳过`)
    return null
  }
  const rawData = fields.get('data')
  if (!Array.isArray(rawData)) {
    warnings.push(`series[${index}] 缺少数据数组，已跳过`)
    return null
  }

  const data: number[] = []
  for (const point of rawData.slice(0, LIMITS.dataPointsPerSeries)) {
    const numeric = safeNumber(point)
    if (numeric === undefined) continue
    data.push(numeric)
  }
  if (rawData.length > LIMITS.dataPointsPerSeries) {
    warnings.push(`series[${index}] 数据点超出上限 ${LIMITS.dataPointsPerSeries}，已截断`)
  }
  if (data.length === 0) {
    warnings.push(`series[${index}] 没有有效数值，已跳过`)
    return null
  }

  const series: ChartSeries = { name, data }
  const color = safeColor(fields.get('color'))
  if (color) series.color = color
  const stack = safeString(fields.get('stack'), 40)
  if (stack) series.stack = stack
  return series
}

/**
 * Validate and fully rebuild a candidate spec.
 *
 * The returned spec is a NEW plain object graph: it shares no reference with the
 * input, so a caller cannot mutate it back to an unsafe value afterwards.
 */
export function sanitizeChartSpec(input: unknown): ChartSpecResult {
  const warnings: string[] = []

  if (!isPlainObject(input)) {
    return { ok: false, reason: '图表数据不是对象', warnings }
  }

  const fields = new Map(ownEntries(input))
  const rawKind = fields.get('kind')
  const kind = typeof rawKind === 'string' ? rawKind : ''

  if (!kind) {
    return { ok: false, reason: '缺少图表类型 kind', warnings }
  }
  if (!(CHART_KINDS as readonly string[]).includes(kind)) {
    // FAIL CLOSED: an unknown kind is never rendered "best effort".
    return {
      ok: false,
      reason: `未知的图表类型 "${safeString(kind, 40) ?? ''}"`,
      warnings,
    }
  }

  const chartKind = kind as ChartKind
  const spec: ChartSpec = { kind: chartKind, series: [] }

  const title = safeString(fields.get('title'))
  if (title) spec.title = title
  const subtitle = safeString(fields.get('subtitle'))
  if (subtitle) spec.subtitle = subtitle
  const valueUnit = safeString(fields.get('value_unit'), 20)
  if (valueUnit) spec.value_unit = valueUnit
  const footnote = safeString(fields.get('footnote'))
  if (footnote) spec.footnote = footnote
  if (fields.get('money') === true) spec.money = true

  // -- table kind ---------------------------------------------------------
  if (chartKind === 'table') {
    const rawColumns = fields.get('columns')
    if (!Array.isArray(rawColumns)) {
      return { ok: false, reason: 'table 类型缺少 columns', warnings }
    }
    const columns: { key: string; title: string; align?: 'left' | 'center' | 'right' }[] = []
    for (const rawColumn of rawColumns.slice(0, LIMITS.columns)) {
      if (!isPlainObject(rawColumn)) continue
      const columnFields = new Map(ownEntries(rawColumn))
      const key = safeString(columnFields.get('key'), 60)
      const columnTitle = safeString(columnFields.get('title'), 60)
      if (!key || !columnTitle) continue
      const alignRaw = columnFields.get('align')
      const align =
        alignRaw === 'left' || alignRaw === 'center' || alignRaw === 'right' ? alignRaw : undefined
      columns.push(align ? { key, title: columnTitle, align } : { key, title: columnTitle })
    }
    if (columns.length === 0) {
      return { ok: false, reason: 'table 类型没有有效列定义', warnings }
    }
    spec.columns = columns

    const rawRows = fields.get('rows')
    if (!Array.isArray(rawRows)) {
      return { ok: false, reason: 'table 类型缺少 rows', warnings }
    }
    const allowed = new Set(columns.map((column) => column.key))
    const rows: Record<string, string | number | null>[] = []
    for (const rawRow of rawRows.slice(0, LIMITS.tableRows)) {
      if (!isPlainObject(rawRow)) continue
      const row: Record<string, string | number | null> = {}
      for (const [key, value] of ownEntries(rawRow)) {
        if (!allowed.has(key)) continue
        if (value === null) {
          row[key] = null
        } else if (typeof value === 'number' && Number.isFinite(value)) {
          row[key] = value
        } else if (typeof value === 'string') {
          row[key] = safeString(value) ?? ''
        } else {
          // Anything else (objects, functions, booleans we did not declare) is dropped.
          continue
        }
      }
      rows.push(row)
    }
    if (rawRows.length > LIMITS.tableRows) {
      warnings.push(`表格行数超出上限 ${LIMITS.tableRows}，已截断`)
    }
    spec.rows = rows
    return { ok: true, spec, warnings }
  }

  // -- cartesian / pie / gauge kinds --------------------------------------
  const rawCategories = fields.get('categories')
  if (Array.isArray(rawCategories)) {
    const categories: string[] = []
    for (const rawCategory of rawCategories.slice(0, LIMITS.categories)) {
      const category = safeString(rawCategory, 80)
      categories.push(category ?? '')
    }
    if (rawCategories.length > LIMITS.categories) {
      warnings.push(`分类数超出上限 ${LIMITS.categories}，已截断`)
    }
    spec.categories = categories
  }

  const rawSeries = fields.get('series')
  if (!Array.isArray(rawSeries)) {
    return { ok: false, reason: '缺少 series 数组', warnings }
  }
  const series: ChartSeries[] = []
  for (const [index, rawSerie] of rawSeries.slice(0, LIMITS.series).entries()) {
    const sanitized = sanitizeSeries(rawSerie, warnings, index)
    if (sanitized) series.push(sanitized)
  }
  if (rawSeries.length > LIMITS.series) {
    warnings.push(`系列数超出上限 ${LIMITS.series}，已截断`)
  }
  if (series.length === 0) {
    return { ok: false, reason: '没有任何有效的系列数据', warnings }
  }
  spec.series = series

  // A pie/gauge needs exactly one series to be meaningful; more is a spec bug.
  if (chartKind === 'pie' && series.length > 1) {
    warnings.push('饼图仅使用第一个系列渲染')
    spec.series = [series[0] as ChartSeries]
  }
  if (chartKind === 'gauge' && series.length > 1) {
    warnings.push('仪表盘仅使用第一个系列渲染')
    spec.series = [series[0] as ChartSeries]
  }

  // Categories and data length disagreement is a common agent slip: align to the
  // shorter side and TELL the user rather than drawing a wrong chart.
  if (spec.categories) {
    const maxData = Math.max(...spec.series.map((serie) => serie.data.length))
    if (maxData !== spec.categories.length) {
      warnings.push(
        `分类数(${spec.categories.length})与数据点(${maxData})不一致，已按较短者对齐`,
      )
      if (maxData < spec.categories.length) {
        spec.categories = spec.categories.slice(0, maxData)
      } else {
        spec.series = spec.series.map((serie) => ({
          ...serie,
          data: serie.data.slice(0, spec.categories?.length ?? serie.data.length),
        }))
      }
    }
  }

  return { ok: true, spec, warnings }
}

/**
 * Money-aware value formatter for tooltips/labels.
 * Kept here (not in the renderer) so the minor-unit conversion has exactly one
 * implementation: it reuses `money.toMajorString`, which uses integer arithmetic
 * instead of `value / 100` (which drifts for large sums).
 */
export function formatSpecValue(value: number, spec: Pick<ChartSpec, 'money' | 'value_unit'>): string {
  const rendered = spec.money ? toMajorString(value) : String(value)
  return spec.value_unit ? `${rendered}${spec.value_unit}` : rendered
}
