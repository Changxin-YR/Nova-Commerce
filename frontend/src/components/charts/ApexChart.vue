<script setup lang="ts">
/**
 * The one place ECharts is instantiated.
 *
 * SECURITY: the ONLY input is a `ChartSpec` that has already passed
 * `sanitizeChartSpec`, or a module-level spec built by our own analytics code. The
 * component never receives raw agent JSON, never evaluates a string, and never
 * passes agent text into a function slot. `buildEChartsOption` converts the spec to
 * plain data options plus OUR callbacks.
 *
 * ECharts core is imported lazily (dynamic import) so the 300 kB chart runtime is
 * not in the initial bundle — it loads the first time a chart is actually rendered.
 */
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useAppStore } from '@/stores/app'
import { buildEChartsOption } from '@/charts/buildEChartsOption'
import type { ChartSpec } from '@/types/charts'

const props = withDefaults(
  defineProps<{
    spec: ChartSpec
    /** Canvas CSS height in px. */
    height?: number
    /** Show the spec's footnote under the canvas. */
    showFootnote?: boolean
  }>(),
  { height: 280, showFootnote: true },
)

const app = useAppStore()
const container = ref<HTMLDivElement | null>(null)
const failed = ref('')
const ready = ref(false)

/**
 * `shallowRef` on purpose: an ECharts instance is a huge mutable object graph and
 * making it reactive would be both slow and buggy.
 */
const chart = shallowRef<{ setOption: (o: unknown, notMerge?: boolean) => void; resize: () => void; dispose: () => void } | null>(null)
let observer: ResizeObserver | null = null

const option = computed(() => buildEChartsOption(props.spec, { dark: app.isDark }))

async function ensureChart(): Promise<void> {
  if (chart.value || !container.value) return
  try {
    // Lazy + tree-shaken: only the chart types, components and renderer actually
    // used are pulled in, and only on first render (keeps ECharts out of the
    // initial chunk).
    const [echarts, charts, components, renderers] = await Promise.all([
      import('echarts/core'),
      import('echarts/charts'),
      import('echarts/components'),
      import('echarts/renderers'),
    ])

    echarts.use([
      charts.LineChart,
      charts.BarChart,
      charts.PieChart,
      charts.ScatterChart,
      charts.GaugeChart,
      components.GridComponent,
      components.TooltipComponent,
      components.LegendComponent,
      components.TitleComponent,
      renderers.CanvasRenderer,
    ])

    const instance = echarts.init(container.value, undefined, { renderer: 'canvas' })
    instance.setOption(option.value)
    chart.value = instance as unknown as typeof chart.value
    ready.value = true
  } catch (error) {
    failed.value = error instanceof Error ? error.message : '图表渲染失败'
  }
}

onMounted(async () => {
  // `table` specs are rendered as HTML by the message renderer, never here.
  if (props.spec.kind === 'table') return
  await ensureChart()
  if (container.value && typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(() => chart.value?.resize())
    observer.observe(container.value)
  }
})

watch(option, (next) => {
  if (!chart.value) return
  // `notMerge: true` so removing a series cannot leave a ghost series behind.
  chart.value.setOption(next, true)
})

watch(
  () => app.isDark,
  () => {
    if (chart.value) chart.value.setOption(option.value, true)
  },
)

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  chart.value?.dispose()
  chart.value = null
})
</script>

<template>
  <div class="nx-chart">
    <div v-if="failed" class="nx-chart__error" role="alert">
      <strong>图表渲染失败</strong>
      <p>{{ failed }}</p>
    </div>
    <div
      v-else
      ref="container"
      class="nx-chart__canvas"
      :style="{ height: `${height}px` }"
      role="img"
      :aria-label="spec.title ? `图表：${spec.title}` : '数据图表'"
    />
    <p v-if="showFootnote && spec.footnote" class="nx-chart__footnote">{{ spec.footnote }}</p>
  </div>
</template>

<style scoped lang="scss">
.nx-chart {
  width: 100%;

  &__canvas {
    width: 100%;
  }

  &__error {
    padding: 12px 14px;
    border-radius: 12px;
    background: var(--nx-danger-soft);
    color: var(--nx-danger);
    font-size: 13px;

    p {
      margin: 4px 0 0;
      opacity: 0.85;
    }
  }

  &__footnote {
    margin: 6px 0 0;
    font-size: 12px;
    color: var(--nx-text-muted);
  }
}
</style>
