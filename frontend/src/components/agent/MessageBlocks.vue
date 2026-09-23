<script setup lang="ts">
/**
 * Message block renderer (§101).
 *
 * Supports exactly the eight frozen block kinds:
 *   Text · Metric · Table · Chart · Citation · ToolProgress · ActionProposal · Error
 *
 * SAFETY CONTRACT
 *  - Every string is interpolated as TEXT (`{{ }}`). There is no `v-html` anywhere in
 *    this file, so a `<script>` tag inside an agent response renders as visible
 *    characters instead of executing.
 *  - Chart blocks receive an already-VALIDATED `ChartSpec`. A rejected spec arrives as
 *    an Error block instead, because `useAiThreadStore` fails closed.
 *  - ActionProposal blocks are never executed here: they render an approval card. The
 *    write happens only after a human approves, and the server revalidates the hash.
 */
import { computed } from 'vue'
import ApexChart from '@/components/charts/ApexChart.vue'
import ActionApprovalCard from '@/components/agent/ActionApprovalCard.vue'
import type { AgentMessageBlock } from '@/stores/aiThread'

const props = defineProps<{ blocks: AgentMessageBlock[] }>()

/** Blocks are appended in place by the store, so identity is stable. */
const items = computed(() => props.blocks)
</script>

<template>
  <div class="blocks">
    <template v-for="block in items" :key="block.id">
      <!-- Text -->
      <p v-if="block.kind === 'text'" class="blocks__text">{{ block.text }}</p>

      <!-- Metric -->
      <div v-else-if="block.kind === 'metric'" class="blocks__metric">
        <span class="blocks__metric-label">{{ block.label }}</span>
        <span class="blocks__metric-value">
          {{ block.metric?.value }}
          <small v-if="block.metric?.unit">{{ block.metric.unit }}</small>
          <span
            v-if="block.metric?.trend"
            class="blocks__metric-trend"
            :class="`blocks__metric-trend--${block.metric.trend}`"
          >
            {{ block.metric.trend === 'up' ? '↑' : block.metric.trend === 'down' ? '↓' : '→' }}
          </span>
        </span>
      </div>

      <!-- Table: real HTML, so cells stay selectable and accessible -->
      <div v-else-if="block.kind === 'table' && block.table" class="blocks__table-wrap">
        <table class="blocks__table">
          <thead>
            <tr>
              <th
                v-for="column in block.table.columns"
                :key="column.key"
                :style="{ textAlign: column.align ?? 'left' }"
              >
                {{ column.title }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, rowIndex) in block.table.rows" :key="rowIndex">
              <td
                v-for="column in block.table.columns"
                :key="column.key"
                :style="{ textAlign: column.align ?? 'left' }"
              >
                {{ row[column.key] === null || row[column.key] === undefined ? '—' : row[column.key] }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Chart: only a spec that passed sanitizeChartSpec reaches here -->
      <div v-else-if="block.kind === 'chart' && block.chart" class="blocks__chart">
        <p v-if="block.label" class="blocks__chart-title">{{ block.label }}</p>
        <ApexChart :spec="block.chart" :height="260" />
      </div>

      <!-- Citation -->
      <div v-else-if="block.kind === 'citation'" class="blocks__citations">
        <p class="blocks__citations-title">{{ block.label ?? '引用' }}</p>
        <ol>
          <li v-for="citation in block.citations ?? []" :key="`${citation.doc_id}-${citation.index}`">
            <span class="blocks__citation-index">[{{ citation.index }}]</span>
            <span class="blocks__citation-doc">{{ citation.doc_name }}</span>
            <span class="blocks__citation-score">score {{ citation.score.toFixed(3) }}</span>
            <p class="blocks__citation-snippet">{{ citation.snippet }}</p>
          </li>
        </ol>
      </div>

      <!-- ToolProgress -->
      <div
        v-else-if="block.kind === 'tool_progress' && block.tool"
        class="blocks__tool"
        :class="`blocks__tool--${block.tool.status}`"
      >
        <span class="blocks__tool-status" aria-hidden="true">
          {{ block.tool.status === 'running' ? '◌' : block.tool.status === 'succeeded' ? '✓' : block.tool.status === 'failed' ? '✕' : '–' }}
        </span>
        <span class="blocks__tool-name">{{ block.tool.tool_name }}</span>
        <span class="blocks__tool-summary">{{ block.tool.summary }}</span>
        <span v-if="block.tool.duration_ms !== undefined" class="blocks__tool-duration">
          {{ block.tool.duration_ms }} ms
        </span>
      </div>

      <!-- ActionProposal: an approval card, never an auto-executed write -->
      <ActionApprovalCard v-else-if="block.kind === 'action_proposal' && block.action" :action="block.action" />

      <!-- Error -->
      <div v-else-if="block.kind === 'error'" class="blocks__error" role="alert">
        <strong v-if="block.label" class="blocks__error-label">{{ block.label }}</strong>
        <span>{{ block.text }}</span>
      </div>
    </template>
  </div>
</template>

<style scoped lang="scss">
.blocks {
  display: flex;
  flex-direction: column;
  gap: 8px;

  &__text {
    margin: 0;
    font-size: 13px;
    line-height: 1.7;
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* -- Metric: a clear numeric hierarchy, not a decorative card -----------
     label 12px/muted  ->  value 24px/700/tabular  ->  unit 12px  ->  trend */
  &__metric {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 10px 12px;
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
    border-left: 3px solid var(--nx-brand);
  }

  &__metric-label {
    font-size: 12px;
    color: var(--nx-text-muted);
  }

  &__metric-value {
    display: flex;
    align-items: baseline;
    gap: 4px;
    font-size: 24px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    line-height: 1.15;
    color: var(--nx-text);

    small {
      font-size: 12px;
      font-weight: 400;
      color: var(--nx-text-muted);
    }
  }

  &__metric-trend {
    margin-left: 4px;
    font-size: 13px;
    font-weight: 400;

    &--up {
      color: var(--nx-brand);
    }

    &--down {
      color: var(--nx-success);
    }

    &--flat {
      color: var(--nx-text-muted);
    }
  }

  /* -- Table: operations density (36px rows, tabular figures) ------------ */
  &__table-wrap {
    overflow-x: auto;
    border: 1px solid var(--nx-border);
  }

  &__table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;

    th,
    td {
      padding: 8px 10px;
      border-bottom: 1px solid var(--nx-border);
      white-space: nowrap;
    }

    th {
      background: var(--nx-surface-sunken);
      border-right: 1px solid var(--nx-border);
      color: var(--nx-text-secondary);
      font-weight: 700;

      &:last-child {
        border-right: none;
      }
    }

    tbody tr:hover {
      background: var(--nx-surface-hover);
    }

    tr:last-child td {
      border-bottom: none;
    }

    td {
      font-variant-numeric: tabular-nums;
    }
  }

  &__chart {
    padding: 10px 12px 4px;
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
  }

  &__chart-title {
    margin: 0 0 8px;
    font-size: 13px;
    font-weight: 700;
  }

  /* -- Citations --------------------------------------------------------- */
  &__citations {
    padding: 10px 12px;
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);

    ol {
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin: 0;
      padding: 0;
      list-style: none;
    }

    li {
      padding-left: 8px;
      border-left: 2px solid var(--nx-border);
      font-size: 12px;
      line-height: 1.6;
    }
  }

  &__citations-title {
    margin: 0 0 8px;
    color: var(--nx-text-secondary);
    font-size: 12px;
    font-weight: 700;
  }

  &__citation-index {
    color: var(--nx-brand);
    font-weight: 700;
  }

  &__citation-doc {
    margin-left: 6px;
    font-weight: 700;
  }

  &__citation-score {
    margin-left: 8px;
    color: var(--nx-text-muted);
    font-variant-numeric: tabular-nums;
  }

  &__citation-snippet {
    margin: 2px 0 0;
    color: var(--nx-text-secondary);
  }

  /* -- ToolProgress: a one-line ledger entry ----------------------------- */
  &__tool {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 10px;
    background: var(--nx-surface-sunken);
    border: 1px solid var(--nx-border);
    font-size: 12px;

    &--running {
      color: var(--nx-info);
      border-color: var(--nx-info);
    }

    &--succeeded {
      color: var(--nx-success);
    }

    &--failed {
      color: var(--nx-danger);
      border-color: var(--nx-danger);
    }
  }

  &__tool-name {
    color: var(--nx-text);
    font-weight: 700;
  }

  &__tool-summary {
    overflow: hidden;
    color: var(--nx-text-secondary);
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  &__tool-duration {
    margin-left: auto;
    color: var(--nx-text-muted);
    font-variant-numeric: tabular-nums;
  }

  &__error {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 8px 10px;
    background: var(--nx-danger-soft);
    border: 1px solid var(--nx-danger);
    color: var(--nx-danger);
    font-size: 12px;
    line-height: 1.6;
  }

  &__error-label {
    font-size: 12px;
    font-weight: 700;
  }
}
</style>
