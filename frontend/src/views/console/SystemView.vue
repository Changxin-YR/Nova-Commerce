<script setup lang="ts">
/**
 * Console · System — dependency health, audit trail, and the permission boundary.
 *
 * WHY THE CRITICALITY COLUMN IS THE POINT (§130)
 *  MySQL down means the SHOP is down; Qdrant / LLM / reranker / MCP down only degrades features.
 *  Collapsing those into one red light is the mistake the dependency hierarchy exists to avoid, so
 *  criticality is a first-class column and the summary distinguishes "degraded" from "unhealthy".
 *
 * WHY THERE IS NO PERMISSION EDITOR HERE (§65)
 *  §65 forbids a CRITICAL write tool being downgradeable to READ by an ordinary console user. A
 *  role/permission editor is exactly that kind of write, so it is NOT offered as a casual toggle —
 *  and no role or permission endpoint is frozen yet either. Building one would mean inventing both
 *  the shape and the guard, so this page states the boundary instead of shipping a control that
 *  cannot honour it. Recorded in the migration report.
 *
 * §104: nothing on this page mutates domain state. The probe is read-only, and the audit trail is
 * append-only and server-written — the client can only read it.
 */
import { computed, onBeforeUnmount, onMounted, reactive } from 'vue'
import { systemApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { POLL_INTERVAL_MS } from '@/config/app'
import StateView from '@/components/ui/StateView.vue'
import type { DependencyCriticality, DependencyStatus } from '@/api/system'

const {
  data: health,
  status,
  error,
  execute,
} = useAsyncState(() => systemApi.ready(), { immediate: true })

const { data: live, execute: loadLive } = useAsyncState(() => systemApi.live(), { immediate: true })

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

const STATUS_LABELS: Record<DependencyStatus, string> = {
  up: '正常',
  down: '不可用',
  skipped: '未探测',
}

const checks = computed(() => health.value?.checks ?? [])

/**
 * A `critical` dependency that is down is an outage; a `degradable` one that is down is a feature
 * gap. They must not render the same way — that distinction is why `criticality` is on the wire.
 */
function rowTone(check: { criticality: DependencyCriticality; status: DependencyStatus }): string {
  if (check.status === 'up') return 'ok'
  if (check.status === 'skipped') return 'muted'
  return check.criticality === 'critical' ? 'danger' : 'warning'
}

const overallTone = computed(() => {
  switch (health.value?.status) {
    case 'ok':
      return 'ok'
    case 'degraded':
      return 'warning'
    case 'unhealthy':
      return 'danger'
    default:
      return 'muted'
  }
})

/** A critical dependency being down is what makes the overall state "unhealthy" rather than "degraded". */
const criticalDown = computed(() =>
  checks.value.filter((c) => c.criticality === 'critical' && c.status === 'down'),
)

/* -- audit trail (a paged list, so it gets the dense table treatment) ------ */

const auditFilters = reactive({ resourceType: '', page: 1, page_size: 20 })

const {
  data: auditData,
  status: auditStatus,
  error: auditError,
  execute: loadAudit,
} = useAsyncState(
  () =>
    systemApi.audit({
      page: auditFilters.page,
      page_size: auditFilters.page_size,
      // Empty select -> ABSENT key, never `resource_type=`.
      resource_type: auditFilters.resourceType.trim() || undefined,
    }),
  { immediate: true },
)

const auditRecords = computed(() => auditData.value?.items ?? [])
const auditMeta = computed(() => auditData.value?.meta ?? null)

function applyAuditFilters(): void {
  auditFilters.page = 1
  void loadAudit()
}

function resetAuditFilters(): void {
  auditFilters.resourceType = ''
  auditFilters.page = 1
  void loadAudit()
}

function changeAuditPage(delta: number): void {
  const next = auditFilters.page + delta
  if (next < 1) return
  if (auditMeta.value && next > auditMeta.value.total_pages) return
  auditFilters.page = next
  void loadAudit()
}

const RESULT_LABELS: Record<string, string> = {
  SUCCESS: '成功',
  FAILURE: '失败',
  BLOCKED: '已拦截',
}

const ACTOR_LABELS: Record<string, string> = {
  USER: '用户',
  AGENT: '智能体',
  SYSTEM: '系统',
}

function stamp(iso: string): string {
  const date = new Date(iso)
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
</script>

<template>
  <div class="system">
    <div class="nx-block">
      <div class="system__head">
        <h2 class="nx-block__title">系统状态</h2>
        <div class="system__head-actions">
          <span class="nx-muted">环境：{{ health?.environment ?? '—' }}</span>
          <span class="nx-muted">版本：{{ health?.version ?? '—' }}</span>
          <button type="button" class="nx-btn nx-btn--sm" @click="execute(); loadLive()">立即探测</button>
        </div>
      </div>

      <StateView :state="status" :error="error" @retry="execute()">
        <div class="system__summary" :data-tone="overallTone">
          <p class="system__overall">
            总体状态：<strong>{{ health?.status ?? '—' }}</strong>
          </p>
          <p class="nx-muted">
            live 探针：{{ live?.status ?? '—' }} · ready 探针：{{ health?.status ?? '—' }} ·
            检测时间：{{ health?.checked_at ? stamp(health.checked_at) : '—' }}
          </p>
          <!--
            The distinction the hierarchy exists for: an outage vs a feature gap. Surfaced explicitly
            rather than left to the reader to infer from a colour.
          -->
          <p v-if="criticalDown.length" class="system__critical">
            关键依赖不可用：{{ criticalDown.map((c) => c.name).join('、') }} —— 交易链路受影响。
          </p>
          <p v-else class="nx-muted">关键依赖（MySQL / Redis）全部正常，交易链路可用。</p>
        </div>

        <table class="nx-table">
          <thead>
            <tr>
              <th style="width: 180px">依赖</th>
              <th style="width: 100px">关键性</th>
              <th style="width: 100px">状态</th>
              <th style="width: 90px; text-align: right">耗时</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="check in checks" :key="check.name" :data-tone="rowTone(check)">
              <td><code>{{ check.name }}</code></td>
              <td>{{ CRITICALITY_LABELS[check.criticality] }}</td>
              <td>{{ STATUS_LABELS[check.status] }}</td>
              <td style="text-align: right">{{ check.duration_ms }} ms</td>
              <td class="nx-muted">{{ check.detail }}</td>
            </tr>
          </tbody>
        </table>
      </StateView>
    </div>

    <!-- audit trail ---------------------------------------------------------- -->
    <div class="nx-block">
      <div class="nx-block__head">
        <h2 class="nx-block__title">审计日志</h2>
      </div>

      <div class="nx-filterbar">
        <label>
          资源类型
          <input
            v-model="auditFilters.resourceType"
            class="nx-input"
            placeholder="例如 order / product"
            @keydown.enter="applyAuditFilters()"
          />
        </label>
        <button type="button" class="nx-btn nx-btn--primary nx-btn--sm" @click="applyAuditFilters()">查询</button>
        <button type="button" class="nx-btn nx-btn--sm" @click="resetAuditFilters()">重置</button>
      </div>

      <StateView :state="auditStatus" :error="auditError" @retry="loadAudit()">
        <div v-if="auditMeta" class="system__count nx-muted">共 {{ auditMeta.total }} 条</div>
        <table class="nx-table">
          <thead>
            <tr>
              <th style="width: 140px">时间</th>
              <th style="width: 90px">操作者</th>
              <th style="width: 170px">动作</th>
              <th style="width: 140px">资源</th>
              <th style="width: 90px">结果</th>
              <th>trace_id</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="record in auditRecords" :key="record.id">
              <td>{{ stamp(record.created_at) }}</td>
              <td>{{ ACTOR_LABELS[record.actor_type] ?? record.actor_type }}</td>
              <td><code>{{ record.action }}</code></td>
              <td class="nx-muted">{{ record.resource_type }}#{{ record.resource_id }}</td>
              <td>{{ RESULT_LABELS[record.result] ?? record.result }}</td>
              <td><code class="system__trace">{{ record.trace_id }}</code></td>
            </tr>
          </tbody>
        </table>

        <div v-if="auditMeta" class="system__pager">
          <button type="button" class="nx-btn nx-btn--sm" :disabled="auditFilters.page <= 1" @click="changeAuditPage(-1)">
            上一页
          </button>
          <span class="nx-muted">第 {{ auditMeta.page }} / {{ auditMeta.total_pages }} 页</span>
          <button
            type="button"
            class="nx-btn nx-btn--sm"
            :disabled="auditMeta.total_pages > 0 && auditFilters.page >= auditMeta.total_pages"
            @click="changeAuditPage(1)"
          >
            下一页
          </button>
        </div>
      </StateView>
    </div>

    <!-- permission boundary (§65) -------------------------------------------- -->
    <div class="nx-block">
      <div class="nx-block__head">
        <h2 class="nx-block__title">权限管理边界</h2>
      </div>
      <div class="nx-block__body">
        <p class="nx-muted">
          §65 规定：关键写入工具的权限不得被普通控制台用户降级为只读。角色与权限编辑正属于这类写入，
          因此本页不提供「一键切换」式的权限开关——它既缺少冻结的端点，也无法在客户端保证该约束。
        </p>
        <p class="nx-muted">
          换句话说：权限是 UX，服务端才是权威（§104）。角色变更应走独立的高风险审批流程，而不是这一页上的一个复选框。
        </p>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.system {
  display: grid;
  gap: 12px;

  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px;
    border-bottom: 1px solid var(--nx-border);
  }

  &__head-actions {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 12px;
  }

  &__summary {
    padding: 10px;
    font-size: 12px;

    &[data-tone='ok'] {
      border-left: 3px solid #52c41a;
    }

    &[data-tone='warning'] {
      border-left: 3px solid #faad14;
    }

    &[data-tone='danger'] {
      border-left: 3px solid var(--nx-price);
    }
  }

  &__overall {
    margin: 0 0 4px;
    font-size: 14px;
  }

  &__critical {
    margin: 6px 0 0;
    color: var(--nx-price);
    font-weight: 600;
  }

  &__count {
    padding: 8px 10px;
    font-size: 12px;
  }

  &__trace {
    font-size: 11px;
  }

  &__pager {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px;
    font-size: 12px;
  }
}
</style>
