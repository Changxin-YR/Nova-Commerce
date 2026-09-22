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
  gap: 10px;

  &__text {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-word;
  }

  &__metric {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 10px 14px;
    border-radius: var(--nx-radius-stage);
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
  }

  &__metric-label {
    font-size: 12px;
    color: var(--nx-text-muted);
  }

  &__metric-value {
    font-size: 22px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;

    small {
      margin-left: 4px;
      font-size: 12px;
      font-weight: 400;
      color: var(--nx-text-muted);
    }
  }

  &__metric-trend {
    margin-left: 6px;
    font-size: 14px;

    &--up {
      color: var(--nx-success);
    }

    &--down {
      color: var(--nx-danger);
    }

    &--flat {
      color: var(--nx-text-muted);
    }
  }

  &__table-wrap {
    overflow-x: auto;
    border: 1px solid var(--nx-border);
    border-radius: var(--nx-radius-stage);
  }

  &__table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;

    th,
    td {
      padding: 8px 12px;
      border-bottom: 1px solid var(--nx-border);
      white-space: nowrap;
    }

    th {
      background: var(--nx-surface-sunken);
      font-weight: 600;
      color: var(--nx-text-secondary);
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
    border: 1px solid var(--nx-border);
    border-radius: var(--nx-radius-stage);
    background: var(--nx-surface);
  }

  &__chart-title {
    margin: 0 0 8px;
    font-size: 13px;
    font-weight: 600;
  }

  &__citations {
    padding: 10px 12px;
    border: 1px solid var(--nx-border);
    border-radius: var(--nx-radius-stage);
    background: var(--nx-surface);

    ol {
      margin: 0;
      padding: 0;
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    li {
      font-size: 12px;
      line-height: 1.6;
    }
  }

  &__citations-title {
    margin: 0 0 8px;
    font-size: 12px;
    font-weight: 600;
    color: var(--nx-text-secondary);
  }

  &__citation-index {
    color: var(--nx-primary);
    font-weight: 600;
  }

  &__citation-doc {
    margin-left: 6px;
    font-weight: 600;
  }

  &__citation-score {
    margin-left: 8px;
    color: var(--nx-text-muted);
    font-variant-numeric: tabular-nums;
  }

  &__citation-snippet {
    margin: 2px 0 0;
    color: var(--nx-text-muted);
  }

  &__tool {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 7px 12px;
    border-radius: var(--nx-radius-control);
    background: var(--nx-surface-sunken);
    font-size: 12.5px;

    &--running {
      color: var(--nx-primary);
    }

    &--succeeded {
      color: var(--nx-success);
    }

    &--failed {
      color: var(--nx-danger);
    }
  }

  &__tool-name {
    font-weight: 600;
  }

  &__tool-summary {
    color: var(--nx-text-muted);
    overflow: hidden;
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
    padding: 10px 12px;
    border-radius: var(--nx-radius-stage);
    background: var(--nx-danger-soft);
    color: var(--nx-danger);
    font-size: 13px;
  }

  &__error-label {
    font-size: 11.5px;
    opacity: 0.9;
  }
}
</style>
