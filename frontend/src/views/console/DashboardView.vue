<script setup lang="ts">
/**
 * Console dashboard: KPI cards + GMV trend chart.
 *
 * The trend chart is produced by the SAME ChartSpec pipeline the agent uses: a spec is built
 * from the analytics response, validated, then rendered. The console therefore cannot show a
 * chart the validator would have rejected — one code path, not two.
 */
import { computed } from 'vue'
import { analyticsApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { checkMetricUnit, formatAnalyticsValue } from '@/domain/analytics/unit'
import { sanitizeChartSpec } from '@/charts/sanitizeChartSpec'
import StateView from '@/components/ui/StateView.vue'
import ApexChart from '@/components/charts/ApexChart.vue'
import PriceText from '@/components/ui/PriceText.vue'
import type { AnalyticsEnvelope } from '@/types/frozen-contract'
import type { ChartSpec } from '@/types/charts'

/**
 * ONE ENVELOPE PER METRIC (API_CONTRACT.md §8).
 *
 * The old dashboard called `analyticsApi.overview()` and read invented fields (`gmv_amount`,
 * `payment_conversion_rate`, …). There is no overview endpoint in the frozen contract: every
 * analytics endpoint returns the SAME envelope, so each KPI is its own call and the envelope's
 * `unit` decides how the number renders. That is exactly what stops `refund.rate = 0.12` being
 * drawn as a currency amount.
 */
const {
  data: gmv,
  status,
  error,
  execute,
} = useAsyncState(() => analyticsApi.metric('sales.gmv', { granularity: 'day' }), { immediate: true })

const {
  data: orderCount,
  status: orderCountStatus,
  execute: loadOrderCount,
} = useAsyncState(() => analyticsApi.metric('sales.order_count', { granularity: 'day' }), {
  immediate: true,
})

const { data: refundRate, execute: loadRefundRate } = useAsyncState(
  () => analyticsApi.metric('refund.rate', { granularity: 'day' }),
  { immediate: true },
)

/** A KPI card: money goes to `<PriceText>`, everything else to the unit-aware formatter. */
interface KpiCard {
  label: string
  hint: string
  /** Integer minor units, when the metric's unit is money. */
  amount?: number
  /** Pre-formatted text for `count` / `ratio`. */
  text?: string
}

/** Nothing here is money that did not SAY it was money (the point of `AnalyticsUnit`). */
function kpi(
  label: string,
  hint: string,
  envelope: AnalyticsEnvelope | null | undefined,
): KpiCard | null {
  if (!envelope) return null
  // Cross-check the declared unit against the frozen expectation, so a contract bug surfaces
  // rather than being silently rendered as a plausible-looking wrong number.
  checkMetricUnit(envelope.metric, envelope.unit)
  const display = formatAnalyticsValue(envelope.summary.total, envelope.unit)
  if (display.kind === 'money') return { label, hint, amount: display.amount }
  return { label, hint, text: display.text }
}

const metrics = computed<KpiCard[]>(() =>
  [
    kpi('GMV', '已支付口径', gmv.value),
    kpi('订单数', '本期下单', orderCount.value),
    kpi('退款率', '退款 / GMV', refundRate.value),
  ].filter((card): card is KpiCard => card !== null),
)

/**
 * Build a spec from server data, then VALIDATE it. Nothing reaches ApexChart without passing the
 * same validator that guards agent output — one code path, not two.
 *
 * The series is converted minor→major ONLY in the money branch, and only here at the render
 * boundary; the envelope itself stays in integer minor units throughout.
 */
const trendSpec = computed<ChartSpec | null>(() => {
  const envelope = gmv.value
  const series = envelope?.series ?? []
  if (!envelope || series.length === 0) return null
  const isMoney = envelope.unit === 'minor_currency'
  const candidate = {
    kind: 'line' as const,
    title: 'GMV 趋势',
    categories: series.map((point) => point.bucket),
    series: [
      {
        name: 'GMV',
        data: series.map((point) => (isMoney ? Math.round(point.value / 100) : point.value)),
      },
    ],
    money: isMoney,
    value_unit: isMoney ? '元' : '',
    footnote: isMoney ? '后端以整数分返回金额，图表仅在渲染时换算为元。' : '单位为数量，非金额。',
  }
  const result = sanitizeChartSpec(candidate)
  return result.ok ? result.spec : null
})

const trendStatus = computed(() => orderCountStatus.value)

async function refreshAll(): Promise<void> {
  await Promise.all([execute(), loadOrderCount(), loadRefundRate()])
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
            <p class="dashboard__metric-value nx-money">
              <PriceText v-if="metric.amount !== undefined" :amount="metric.amount" size="lg" />
              <template v-else>{{ metric.text }}</template>
            </p>
            <p class="nx-muted dashboard__metric-hint">{{ metric.hint }}</p>
          </div>
        </article>
      </div>
    </StateView>

    <section class="dashboard__chart nx-card">
      <div class="nx-card__body">
        <h3 class="nx-section-title">GMV 趋势</h3>
        <StateView :state="trendStatus" compact @retry="loadOrderCount()">
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
