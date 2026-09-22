/**
 * `ChartSpec` — the ONLY thing the agent is allowed to emit for a chart (§102).
 *
 * SECURITY MODEL
 *  The agent emits a *declarative data object*. The frontend converts it into
 *  ECharts options. The agent NEVER emits JavaScript and this codebase NEVER
 *  calls `eval` / `new Function` on agent output. `sanitizeChartSpec()` in
 *  `src/charts/sanitizeChartSpec.ts` validates every field against the schema
 *  below and STRIPS everything it does not recognize (including functions and
 *  `__proto__`/`constructor` keys, i.e. prototype-pollution payloads). An
 *  unrecognized `kind` or an invalid payload is rendered as an Error block —
 *  never rendered "best effort".
 *
 *  Adding a new chart kind is a deliberate two-file change: extend the union
 *  here, extend the validator AND the renderer mapping. That is intended: an
 *  unknown kind must fail closed.
 */

export const CHART_KINDS = ['line', 'bar', 'pie', 'scatter', 'gauge', 'table'] as const
export type ChartKind = (typeof CHART_KINDS)[number]

export interface ChartSeries {
  /** Legend name. Sanitized to a bounded plain string. */
  name: string
  /** One numeric value per x-axis category. */
  data: number[]
  /** Optional per-series colour; validated as a CSS colour token, not free text. */
  color?: string
  /** Only meaningful for line/bar/scatter. */
  stack?: string
}

export interface ChartSpec {
  kind: ChartKind
  title?: string
  subtitle?: string
  /** X-axis categories/labels. */
  categories?: string[]
  series: ChartSeries[]
  /** Explicit unit suffix for tooltips, e.g. '¥' or '件'. */
  value_unit?: string
  /** When true, money values are integer minor units and get /100 for display. */
  money?: boolean
  /** Table kind only: column definitions. */
  columns?: { key: string; title: string; align?: 'left' | 'center' | 'right' }[]
  /** Table kind only: rows keyed by column key. */
  rows?: Record<string, string | number | null>[]
  /** Optional footnote shown under the chart (e.g. data window). */
  footnote?: string
}

/** Result of validation. `ok: false` must be rendered as an Error block. */
export type ChartSpecResult =
  | { ok: true; spec: ChartSpec; warnings: string[] }
  | { ok: false; reason: string; warnings: string[] }
