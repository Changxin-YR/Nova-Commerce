<script setup lang="ts">
/**
 * Analytics: KPI cards, trend chart, ranked products and a summary table.
 *
 * Every chart goes through `sanitizeChartSpec` before rendering, exactly like agent output does.
 * That is deliberate: the console must not have a private, unvalidated path into the renderer
 * (§102).
 *
 * FROZEN SHAPE (API_CONTRACT.md §8). The contract returns ONE envelope for EVERY analytics
 * endpoint — `{metric, unit, period, series[{bucket,value}], summary, dimensions[]}` — so this
 * page no longer has three unrelated shapes (`SalesTrend`, `TopProduct[]`, `OrderFunnelStage[]`).
 * It has five envelopes, and the shared `unit` field decides how each number is rendered, so a
 * `ratio` can never be drawn as money.
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

const GRANULARITY = { granularity: 'day' } as const

const {
  data: trend,
  status: trendStatus,
  error: trendError,
  execute: loadTrend,
} = useAsyncState(() => analyticsApi.metric('sales.gmv', GRANULARITY), { immediate: true })

const {
  data: topProducts,
  status: topStatus,
  execute: loadTop,
} = useAsyncState(() => analyticsApi.metric('product.performance', GRANULARITY), {
  immediate: true,
})

const {
  data: orderCount,
  status: funnelStatus,
  execute: loadFunnel,
} = useAsyncState(() => analyticsApi.metric('sales.order_count', GRANULARITY), { immediate: true })

const {
  data: turnover,
  status: turnoverStatus,
  error: turnoverError,
  execute: loadTurnover,
} = useAsyncState(() => analyticsApi.metric('inventory.turnover', GRANULARITY), { immediate: true })

const {
  data: refunds,
  status: refundStatus,
  error: refundError,
  execute: loadRefunds,
} = useAsyncState(() => analyticsApi.metric('refund.rate', GRANULARITY), { immediate: true })

/**
 * Turn an envelope into a ChartSpec.
 *
 * The MONEY branch is the only one that touches `/100`, and it does so here at the render
 * boundary — never on the stored value. `value_unit` follows the declared unit so a count series
 * cannot be labelled as currency.
 */
function specFromEnvelope(
  envelope: AnalyticsEnvelope | null | undefined,
  options: { kind: 'line' | 'bar'; title: string },
): ChartSpec | null {
  if (!envelope || envelope.series.length === 0) return null
  checkMetricUnit(envelope.metric, envelope.unit)
  const isMoney = envelope.unit === 'minor_currency'
  const isRatio = envelope.unit === 'ratio'
  const result = sanitizeChartSpec({
    kind: options.kind,
    title: options.title,
    categories: envelope.series.map((point) => point.bucket),
    series: [
      {
        name: envelope.metric,
        data: envelope.series.map((point) => {
          if (isMoney) return Math.round(point.value / 100)
          if (isRatio) return Math.round(point.value * 1000) / 10
          return point.value
        }),
      },
    ],
    money: isMoney,
    value_unit: isMoney ? '元' : isRatio ? '%' : '',
    footnote: `指标 ${envelope.metric}，粒度 ${envelope.period.granularity}。`,
  })
  return result.ok ? result.spec : null
}

const trendSpec = computed<ChartSpec | null>(() =>
  specFromEnvelope(trend.value, { kind: 'line', title: 'GMV 趋势' }),
)

const topSpec = computed<ChartSpec | null>(() =>
  specFromEnvelope(topProducts.value, { kind: 'bar', title: '商品表现' }),
)

const funnelSpec = computed<ChartSpec | null>(() =>
  specFromEnvelope(orderCount.value, { kind: 'bar', title: '订单量趋势' }),
)

/** The series is a time trend; the top SKU is separate dimension metadata. */
interface RankedRow {
  bucket: string
  raw: number
  unit: AnalyticsEnvelope['unit']
}

const rankedRows = computed<RankedRow[]>(() => {
  const envelope = topProducts.value
  if (!envelope) return []
  return envelope.series.map((point) => ({
    bucket: point.bucket,
    raw: point.value,
    unit: envelope.unit,
  }))
})
const topSkuLabel = computed(() => topProducts.value?.dimensions?.find((d) => d.key === 'sku_id')?.value)

function nonMoneyText(row: RankedRow): string {
  const display = formatAnalyticsValue(row.raw, row.unit)
  return display.kind === 'money' ? '' : display.text
}

/** Unit-aware summary figure for the headline card. */
const summaryDisplay = computed(() => {
  const envelope = topProducts.value
  if (!envelope) return null
  return formatAnalyticsValue(envelope.summary.total, envelope.unit)
})
const turnoverText = computed(() => `${(turnover.value?.summary.total ?? 0).toFixed(2)} 次`)
const refundRateText = computed(() => {
  const dimensions = refunds.value?.dimensions ?? []
  const refunded = Number(dimensions.find((item) => item.key === 'refunded_amount')?.value ?? 0)
  const collected = Number(dimensions.find((item) => item.key === 'collected_amount')?.value ?? 0)
  if (refunded > 0 && collected === 0) return '无本期入账'
  const display = formatAnalyticsValue(refunds.value?.summary.total ?? 0, 'ratio')
  return display.kind === 'ratio' ? display.text : '—'
})
const unitWarnings = computed(() => [trend.value, topProducts.value, orderCount.value, turnover.value, refunds.value]
  .filter((item): item is AnalyticsEnvelope => item !== null && item !== undefined)
  .map((item) => checkMetricUnit(item.metric, item.unit))
  .filter((warning): warning is string => warning !== null))

async function refresh(): Promise<void> {
  await Promise.all([loadTrend(), loadTop(), loadFunnel(), loadTurnover(), loadRefunds()])
}
</script>

<template>
  <div class="analytics">
    <div class="analytics__head">
      <h2 class="nx-section-title">数据分析</h2>
      <button type="button" class="nx-btn" @click="refresh()">刷新</button>
    </div>
    <div v-if="unitWarnings.length" class="nx-card" role="alert">
      <div class="nx-card__body">{{ unitWarnings.join('；') }}</div>
    </div>

    <div class="analytics__total nx-card">
      <div class="nx-card__body">
        <p class="nx-muted">{{ topProducts?.metric ?? 'product.performance' }} 合计</p>
        <p class="analytics__total-value">
          <PriceText
            v-if="summaryDisplay?.kind === 'money'"
            :amount="summaryDisplay.amount"
            size="xl"
          />
          <template v-else-if="summaryDisplay">{{ summaryDisplay.text }}</template>
          <template v-else>—</template>
        </p>
        <p v-if="topSkuLabel" class="nx-muted">销售额最高的 SKU：{{ topSkuLabel }}</p>
      </div>
    </div>

    <div class="analytics__grid">
      <section class="nx-card">
        <div class="nx-card__body">
          <StateView :state="turnoverStatus" :error="turnoverError" @retry="loadTurnover()">
            <p class="nx-muted">库存周转</p>
            <p class="analytics__total-value">{{ turnoverText }}</p>
            <p class="nx-muted">售出件数 ÷ 期初与期末平均在库件数</p>
          </StateView>
        </div>
      </section>
      <section class="nx-card">
        <div class="nx-card__body">
          <StateView :state="refundStatus" :error="refundError" @retry="loadRefunds()">
            <p class="nx-muted">退款率</p>
            <p class="analytics__total-value">{{ refundRateText }}</p>
            <p class="nx-muted">本期成功退款金额 ÷ 本期入账金额</p>
          </StateView>
        </div>
      </section>
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
            <p v-else class="nx-muted">暂无商品表现数据。</p>
          </StateView>
        </div>
      </section>

      <section class="nx-card">
        <div class="nx-card__body">
          <StateView :state="funnelStatus" @retry="loadFunnel()">
            <ApexChart v-if="funnelSpec" :spec="funnelSpec" :height="300" />
            <p v-else class="nx-muted">暂无订单量数据。</p>
          </StateView>
        </div>
      </section>
    </div>

    <section class="nx-card">
      <div class="nx-card__body">
        <h3 class="nx-section-title">指标明细</h3>
        <table v-if="rankedRows.length" class="nx-table">
          <thead>
            <tr>
              <th>时间桶</th>
              <th style="text-align: right">数值</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in rankedRows" :key="index">
              <td>{{ row.bucket }}</td>
              <td style="text-align: right">
                <PriceText v-if="row.unit === 'minor_currency'" :amount="row.raw" size="sm" />
                <template v-else>{{ nonMoneyText(row) }}</template>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="nx-muted">暂无明细数据。</p>
        <p class="nx-muted analytics__note">
          数值由服务端计算；金额按元显示，比例按百分比显示。
        </p>
      </div>
    </section>
  </div>
</template>

<style scoped lang="scss">
.analytics {
  display: grid;
  gap: 14px;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  &__total-value {
    margin: 8px 0 0;
    font-size: 28px;
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 14px;
  }

  &__note {
    margin: 10px 0 0;
    font-size: 11.5px;
  }
}
</style>
