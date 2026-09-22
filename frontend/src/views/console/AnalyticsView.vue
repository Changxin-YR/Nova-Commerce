<script setup lang="ts">
/**
 * Analytics: KPI cards, trend chart, top products and the order funnel.
 *
 * Every chart goes through `sanitizeChartSpec` before rendering, exactly like agent
 * output does. That is deliberate: the console must not have a private, unvalidated
 * path into the renderer (§102).
 */
import { computed } from 'vue'
import { analyticsApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { sanitizeChartSpec } from '@/charts/sanitizeChartSpec'
import { formatMoney } from '@/utils/money'
import StateView from '@/components/ui/StateView.vue'
import ApexChart from '@/components/charts/ApexChart.vue'
import type { ChartSpec } from '@/types/charts'

const {
  data: trend,
  status: trendStatus,
  error: trendError,
  execute: loadTrend,
} = useAsyncState(() => analyticsApi.salesTrend({ granularity: 'day' }), { immediate: true })

const {
  data: topProducts,
  status: topStatus,
  execute: loadTop,
} = useAsyncState(() => analyticsApi.topProducts({ limit: 10 }), { immediate: true })

const {
  data: funnel,
  status: funnelStatus,
  execute: loadFunnel,
} = useAsyncState(() => analyticsApi.orderFunnel(), { immediate: true })

const trendSpec = computed<ChartSpec | null>(() => {
  const points = trend.value?.points ?? []
  if (points.length === 0) return null
  const result = sanitizeChartSpec({
    kind: 'line',
    title: '销售趋势',
    categories: points.map((point) => point.date),
    series: [{ name: 'GMV', data: points.map((point) => point.value) }],
    money: trend.value?.money ?? true,
    value_unit: '元',
  })
  return result.ok ? result.spec : null
})

const topSpec = computed<ChartSpec | null>(() => {
  const rows = topProducts.value ?? []
  if (rows.length === 0) return null
  const result = sanitizeChartSpec({
    kind: 'bar',
    title: '销量 TOP',
    categories: rows.map((row) => row.title),
    series: [{ name: '销量', data: rows.map((row) => row.sold_quantity) }],
    value_unit: '件',
  })
  return result.ok ? result.spec : null
})

const funnelSpec = computed<ChartSpec | null>(() => {
  const stages = funnel.value ?? []
  if (stages.length === 0) return null
  const LABELS: Record<string, string> = {
    created: '创建',
    paid: '支付',
    shipped: '发货',
    completed: '完成',
  }
  const result = sanitizeChartSpec({
    kind: 'bar',
    title: '订单漏斗',
    categories: stages.map((stage) => LABELS[stage.stage] ?? stage.stage),
    series: [{ name: '订单数', data: stages.map((stage) => stage.count) }],
    value_unit: '单',
  })
  return result.ok ? result.spec : null
})

const topTableSpec = computed<ChartSpec | null>(() => {
  const rows = topProducts.value ?? []
  if (rows.length === 0) return null
  const result = sanitizeChartSpec({
    kind: 'table',
    title: '热销商品明细',
    columns: [
      { key: 'title', title: '商品' },
      { key: 'sold_quantity', title: '销量', align: 'right' },
      { key: 'sales_amount', title: '销售额(分)', align: 'right' },
    ],
    rows: rows.map((row) => ({
      title: row.title,
      sold_quantity: row.sold_quantity,
      sales_amount: row.sales_amount,
    })),
    footnote: '销售额以整数分展示，避免浮点误差。',
  })
  return result.ok ? result.spec : null
})

async function refresh(): Promise<void> {
  await Promise.all([loadTrend(), loadTop(), loadFunnel()])
}

const totalSales = computed(() =>
  (topProducts.value ?? []).reduce((sum, row) => sum + row.sales_amount, 0),
)
</script>

<template>
  <div class="analytics">
    <div class="analytics__head">
      <h2 class="nx-section-title">数据分析</h2>
      <button type="button" class="nx-btn" @click="refresh()">刷新</button>
    </div>

    <div class="analytics__total nx-card">
      <div class="nx-card__body">
        <p class="nx-muted">TOP 商品销售额合计</p>
        <p class="analytics__total-value nx-money">{{ formatMoney(totalSales) }}</p>
      </div>
    </div>

    <section class="nx-card">
      <div class="nx-card__body">
        <StateView :state="trendStatus" :error="trendError" @retry="loadTrend()">
          <ApexChart v-if="trendSpec" :spec="trendSpec" :height="300" />
          <p v-else class="nx-muted">暂无趋势数据。</p>
        </StateView>
      </div>
    </section>

    <div class="analytics__grid">
      <section class="nx-card">
        <div class="nx-card__body">
          <StateView :state="topStatus" @retry="loadTop()">
            <ApexChart v-if="topSpec" :spec="topSpec" :height="300" />
            <p v-else class="nx-muted">暂无销量数据。</p>
          </StateView>
        </div>
      </section>

      <section class="nx-card">
        <div class="nx-card__body">
          <StateView :state="funnelStatus" @retry="loadFunnel()">
            <ApexChart v-if="funnelSpec" :spec="funnelSpec" :height="300" />
            <p v-else class="nx-muted">暂无漏斗数据。</p>
          </StateView>
        </div>
      </section>
    </div>

    <section class="nx-card">
      <div class="nx-card__body">
        <h3 class="nx-section-title">热销商品明细</h3>
        <template v-if="topTableSpec?.rows">
          <table class="analytics__table">
            <thead>
              <tr>
                <th v-for="column in topTableSpec.columns ?? []" :key="column.key" :style="{ textAlign: column.align ?? 'left' }">
                  {{ column.title }}
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in topTableSpec.rows" :key="index">
                <td v-for="column in topTableSpec.columns ?? []" :key="column.key" :style="{ textAlign: column.align ?? 'left' }">
                  {{ row[column.key] }}
                </td>
              </tr>
            </tbody>
          </table>
          <p v-if="topTableSpec.footnote" class="nx-muted analytics__footnote">{{ topTableSpec.footnote }}</p>
        </template>
        <p v-else class="nx-muted">暂无明细数据。</p>
      </div>
    </section>
  </div>
</template>

<style scoped lang="scss">
.analytics {
  display: flex;
  flex-direction: column;
  gap: 16px;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__total-value {
    margin: 6px 0 0;
    font-size: 26px;
    color: var(--nx-danger);
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 16px;
  }

  &__table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12.5px;

    th,
    td {
      padding: 9px 12px;
      border-bottom: 1px solid var(--nx-border);
    }

    th {
      background: var(--nx-surface-sunken);
      font-weight: 600;
      color: var(--nx-text-secondary);
    }

    td {
      font-variant-numeric: tabular-nums;
    }
  }

  &__footnote {
    margin: 10px 0 0;
    font-size: 11.5px;
  }
}
</style>
