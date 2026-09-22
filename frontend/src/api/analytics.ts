/**
 * Analytics module (§98, §102).
 *
 * Every series returned here is converted to a `ChartSpec` by the caller and then
 * rendered through the ChartSpec pipeline — the UI never builds ECharts options
 * from raw agent output, and money stays in integer minor units until render time.
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import type {
  AnalyticsOverview,
  OrderFunnelStage,
  SalesTrend,
  TopProduct,
} from '@/types/api-contract'

export interface AnalyticsQuery {
  /** ISO date (inclusive). */
  start_date?: string
  /** ISO date (inclusive). */
  end_date?: string
  granularity?: 'day' | 'week' | 'month'
}

export const analyticsApi = {
  async overview(query: AnalyticsQuery = {}): Promise<AnalyticsOverview> {
    return httpClient.get<AnalyticsOverview>(API.analytics.overview, { params: query })
  },

  async salesTrend(query: AnalyticsQuery = {}): Promise<SalesTrend> {
    return httpClient.get<SalesTrend>(API.analytics.salesTrend, { params: query })
  },

  async topProducts(query: AnalyticsQuery & { limit?: number } = {}): Promise<TopProduct[]> {
    return httpClient.get<TopProduct[]>(API.analytics.topProducts, { params: query })
  },

  async orderFunnel(query: AnalyticsQuery = {}): Promise<OrderFunnelStage[]> {
    return httpClient.get<OrderFunnelStage[]>(API.analytics.orderFunnel, { params: query })
  },
}
