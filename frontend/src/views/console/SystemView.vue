<script setup lang="ts">
/**
 * System status (§130) + a reverse-proxy configuration reference.
 *
 * The point of this page is the CRITICALITY column: MySQL down means the shop is down,
 * while Qdrant/LLM/reranker/MCP down only degrades features. Collapsing those into one
 * red light is the mistake the dependency hierarchy exists to avoid.
 */
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { systemApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { POLL_INTERVAL_MS } from '@/config/app'
import StateView from '@/components/ui/StateView.vue'
import type { DependencyCriticality } from '@/api/system'

const {
  data: health,
  status,
  error,
  execute,
} = useAsyncState(() => systemApi.ready(), { immediate: true })

const {
  data: live,
  execute: loadLive,
} = useAsyncState(() => systemApi.live(), { immediate: true })

let timer: ReturnType<typeof setInterval> | null = null

onMounted(() => {
  timer = setInterval(() => void execute(), POLL_INTERVAL_MS * 4)
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})

const CRITICALITY_LABELS: Record<DependencyCriticality, string> = {
  critical: '关键',
  important: '重要',
  degradable: '可降级',
}

const checks = computed(() => health.value?.checks ?? [])

const overallTone = computed(() => {
  switch (health.value?.status) {
    case 'ok':
      return 'success'
    case 'degraded':
      return 'warning'
    case 'unhealthy':
      return 'danger'
    default:
      return 'neutral'
  }
})
</script>

<template>
  <div class="system">
    <div class="system__head">
      <h2 class="nx-section-title">系统状态</h2>
      <div class="system__head-actions">
        <span class="nx-muted">环境：{{ health?.environment ?? '—' }}</span>
        <span class="nx-muted">版本：{{ health?.version ?? '—' }}</span>
        <button type="button" class="nx-btn" @click="execute(); loadLive()">立即探测</button>
      </div>
    </div>

    <StateView :state="status" :error="error" @retry="execute()">
      <div class="system__summary nx-card" :class="`system__summary--${overallTone}`">
        <div class="nx-card__body">
          <p class="system__overall">
            总体状态：<strong>{{ health?.status }}</strong>
          </p>
          <p class="nx-muted">
            live 探针：{{ live?.status ?? '—' }} · ready 探针：{{ health?.status ?? '—' }} ·
            检测时间 {{ health?.checked_at ? new Date(health.checked_at).toLocaleString() : '—' }}
          </p>
        </div>
      </div>

      <table class="nx-table">
        <thead>
          <tr>
            <th>依赖</th>
            <th>关键性</th>
            <th>状态</th>
            <th>耗时</th>
            <th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="check in checks" :key="check.name">
            <td>{{ check.name }}</td>
            <td>
              <span class="system__criticality" :class="`system__criticality--${check.criticality}`">
                {{ CRITICALITY_LABELS[check.criticality] }}
              </span>
            </td>
            <td>
              <span class="system__dot" :class="`system__dot--${check.status}`" aria-hidden="true" />
              {{ check.status }}
            </td>
            <td>{{ check.duration_ms }} ms</td>
            <td class="system__detail">{{ check.detail }}</td>
          </tr>
        </tbody>
      </table>

      <p class="nx-muted system__note">
        MySQL 是关键依赖：不可用时整个 API 返回 503。Redis 为重要依赖：功能降级但服务可用。
        Qdrant / LLM / 重排 / MCP / 存储为可降级依赖：分别只影响检索、对话、排序质量、外部集成与上传。
      </p>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.system {
  display: flex;
  flex-direction: column;
  gap: 14px;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }

  &__head-actions {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  &__summary {
    border-left: 3px solid var(--nx-border-strong);

    &--success {
      border-left-color: var(--nx-success);
    }

    &--warning {
      border-left-color: var(--nx-warning);
    }

    &--danger {
      border-left-color: var(--nx-danger);
    }
  }

  &__overall {
    margin: 0 0 4px;
    font-size: 15px;
  }


  &__criticality {
    padding: 1px 8px;
    border-radius: var(--nx-radius-pill);
    font-size: 11px;

    &--critical {
      background: var(--nx-danger-soft);
      color: var(--nx-danger);
    }

    &--important {
      background: var(--nx-warning-soft);
      color: var(--nx-warning);
    }

    &--degradable {
      background: var(--nx-surface-sunken);
      color: var(--nx-text-muted);
    }
  }

  &__dot {
    display: inline-block;
    width: 7px;
    height: 7px;
    margin-right: 6px;
    border-radius: 50%;
    background: var(--nx-text-muted);

    &--up {
      background: var(--nx-success);
    }

    &--down {
      background: var(--nx-danger);
    }

    &--skipped {
      background: var(--nx-border-strong);
    }
  }

  &__detail {
    color: var(--nx-text-muted);
    max-width: 380px;
  }

  &__note {
    font-size: 11.5px;
    line-height: 1.7;
  }
}
</style>
