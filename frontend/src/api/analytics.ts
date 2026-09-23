/**
 * Analytics module (§98, §102).
 *
 * Every series returned here is converted to a `ChartSpec` by the caller and then
 * rendered through the ChartSpec pipeline — the UI never builds ECharts options
 * from raw agent output, and money stays in integer minor units until render time.
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import type { AnalyticsEnvelope, AnalyticsMetric } from '@/types/frozen-contract'

/**
 * Date window for every analytics call.
 *
 * ASSUMPTION (API_CONTRACT.md §10 leaves request parameter names unfrozen): the window is sent as
 * `from` / `to` — matching the `period` keys the contract DOES freeze in the response — plus
 * `granularity`. Only the RESPONSE shape is authoritative here; if the backend names these
 * differently the fix is confined to this one interface.
 */
export interface AnalyticsQuery {
  /** ISO date (`YYYY-MM-DD`), inclusive. */
  from?: string
  /** ISO date (`YYYY-MM-DD`), inclusive. */
  to?: string
  granularity?: 'day' | 'week' | 'month'
}

export const analyticsApi = {
  /**
   * Fetch one metric (§8). Every analytics endpoint returns the SAME envelope, so a single chart
   * component and a single table component render all five — which is why there is no per-metric
   * response type here any more.
   *
   * The old `{points: [{date, value}], money: boolean}` shape is gone: `money: boolean` forced the
   * caller to infer units, and a `refund.rate` series rendered through a money formatter is a
   * plausible-looking lie. The envelope carries an explicit `unit`
   * (`minor_currency` | `count` | `ratio`) and the client MUST read it.
   */
  async metric(
    metric: AnalyticsMetric | string,
    query: AnalyticsQuery = {},
  ): Promise<AnalyticsEnvelope> {
    return httpClient.get<AnalyticsEnvelope>(API.analytics.metric(metric), { params: query })
  },
}
