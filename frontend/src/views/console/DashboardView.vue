<script setup lang="ts">
/**
 * Console dashboard: KPI cards + sales trend chart.
 *
 * The trend chart is produced by the SAME ChartSpec pipeline the agent uses: a spec is
 * built from the analytics response, validated, then rendered. The console therefore
 * cannot show a chart the validator would have rejected — one code path, not two.
 */
import { computed } from 'vue'
import { analyticsApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { sanitizeChartSpec } from '@/charts/sanitizeChartSpec'
import StateView from '@/components/ui/StateView.vue'
import ApexChart from '@/components/charts/ApexChart.vue'
import type { ChartSpec } from '@/types/charts'

const {
  data: overview,
  status,
  error,
  execute,
} = useAsyncState(() => analyticsApi.overview(), { immediate: true })

const {
  data: trend,
  status: trendStatus,
  execute: loadTrend,
} = useAsyncState(() => analyticsApi.salesTrend({ granularity: 'day' }), { immediate: true })

const metrics = computed(() => {
  const data = overview.value
  if (!data) return []
  return [
    { label: 'GMV', amount: data.gmv_amount, hint: '已支付口径' },
    { label: '订单数', value: `${data.order_count}`, hint: `已支付 ${data.paid_order_count}` },
    { label: '退款金额', amount: data.refund_amount, hint: `售后单 ${data.after_sale_count}` },
    { label: '支付转化率', value: `${Math.round(data.payment_conversion_rate * 100)}%`, hint: '下单 → 支付' },
    { label: '退款率', value: `${Math.round(data.refund_rate * 100)}%`, hint: '退款 / GMV' },
    { label: '新增用户', value: `${data.new_user_count}`, hint: '本期新增' },
  ]
})

/**
 * Build a spec from server data, then VALIDATE it. Nothing reaches ApexChart without
 * passing the same validator that guards agent output.
 */
const trendSpec = computed<ChartSpec | null>(() => {
  const points = trend.value?.points ?? []
  if (points.length === 0) return null
  const candidate = {
    kind: 'line' as const,
    title: '销售趋势',
    categories: points.map((point) => point.date),
    series: [{ name: 'GMV', data: points.map((point) => point.value) }],
    money: trend.value?.money ?? true,
    value_unit: '元',
    footnote: '后端以整数分返回金额，图表按元展示。',
  }
  const result = sanitizeChartSpec(candidate)
  return result.ok ? result.spec : null
})

async function refreshAll(): Promise<void> {
  await Promise.all([execute(), loadTrend()])
}
</script>

<template>
  <div class="dashboard">
    <div class="dashboard__head">
      <h2 class="nx-section-title">经营概览</h2>
      <button type="button" class="nx-btn" @click="refreshAll()">刷新</button>
    </div>

    <StateView :state="status" :error="error" @retry="execute()">
      <div class="dashboard__metrics">
        <article v-for="metric in metrics" :key="metric.label" class="dashboard__metric nx-card">
          <div class="nx-card__body">
            <p class="dashboard__metric-label">{{ metric.label }}</p>
            <p class="dashboard__metric-value nx-money">{{ metric.value }}</p>
            <p class="nx-muted dashboard__metric-hint">{{ metric.hint }}</p>
          </div>
        </article>
      </div>
    </StateView>

    <section class="dashboard__chart nx-card">
      <div class="nx-card__body">
        <h3 class="nx-section-title">销售趋势</h3>
        <StateView :state="trendStatus" compact @retry="loadTrend()">
          <ApexChart v-if="trendSpec" :spec="trendSpec" :height="300" />
          <p v-else class="nx-muted">暂无可展示的趋势数据。</p>
        </StateView>
      </div>
    </section>
  </div>
</template>

<style scoped lang="scss">
.dashboard {
  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }

  &__metrics {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 14px;
  }

  &__metric-label {
    margin: 0;
    font-size: 12px;
    color: var(--nx-text-muted);
  }

  &__metric-value {
    margin: 8px 0 4px;
    font-size: 24px;
    letter-spacing: -0.019em;
  }

  &__metric-hint {
    margin: 0;
    font-size: 11.5px;
  }

  &__chart {
    margin-top: 16px;
  }
}
</style>
