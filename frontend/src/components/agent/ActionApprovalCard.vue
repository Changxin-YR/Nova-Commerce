<script setup lang="ts">
/**
 * Human-in-the-loop approval card (§101).
 *
 * This component is the ONLY path from "the agent proposed a write" to "the write is
 * attempted", and it always requires a deliberate human click.
 *
 * WHY THE HASH MATTERS
 *  The approve/reject call echoes back `payload_hash` — the exact hash the approver was
 *  shown. If the underlying payload changed between render and decision (another
 *  operator edited it, or the agent re-planned), the server answers
 *  `PENDING_ACTION_PAYLOAD_CHANGED` (12004) and we re-render the diff instead of
 *  approving something nobody reviewed.
 *
 * A CRITICAL/HIGH action is never auto-approved here, no matter what the agent asked
 * for; `requires_approval` is displayed, not obeyed.
 */
import { computed, ref } from 'vue'
import { governanceApi } from '@/api'
import { useNotificationStore } from '@/stores/notification'
import { usePermissionStore } from '@/stores/permission'
import { normalizeError } from '@/api/error'
import StatusChip from '@/components/ui/StatusChip.vue'
import type { AgentMessageBlock } from '@/stores/aiThread'

const props = defineProps<{ action: NonNullable<AgentMessageBlock['action']> }>()

const notifications = useNotificationStore()
const permission = usePermissionStore()

const busy = ref(false)
const localStatus = ref(props.action.status)
const decidedBy = ref('')
const showPayload = ref(false)

const isDecided = computed(
  () => localStatus.value !== 'PENDING' && localStatus.value !== 'EXECUTING',
)
const canDecide = computed(() => localStatus.value === 'PENDING')
/**
 * UX-only gate for the Approve button. The server re-checks the operator's permission
 * on the approve endpoint; hiding the button protects nothing (§104).
 */
const mayApprove = computed(() => permission.hasAny(['agent:approve', 'governance:approve']))
const expiresAt = computed(() => new Date(props.action.expiresAt).toLocaleString())

async function decide(approved: boolean): Promise<void> {
  busy.value = true
  try {
    const payload = {
      payload_hash: props.action.payloadHash,
      decision_reason: approved ? '已人工复核，内容与预期一致' : '人工复核未通过',
    }
    const result = approved
      ? await governanceApi.approve(props.action.pendingActionId, payload)
      : await governanceApi.reject(props.action.pendingActionId, payload)

    localStatus.value = result.status
    decidedBy.value = result.decided_by ?? ''
    // `summary` is the action summary the server echoed back; the decision itself
    // already succeeded, so a missing summary must not look like a failure.
    notifications.success(approved ? '已批准执行' : '已拒绝', result.summary)
  } catch (e) {
    const normalized = normalizeError(e)
    if (normalized.code === 12_004) {
      notifications.warning(
        '审批内容已变化',
        '该操作的内容在审批期间被修改，请重新查看后再决定。',
        normalized.code,
        normalized.traceId,
      )
    } else {
      notifications.error('审批失败', normalized.message, normalized.code, normalized.traceId)
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="approval" :class="`approval--${localStatus.toLowerCase()}`">
    <header class="approval__head">
      <span class="approval__title">待审批写操作</span>
      <StatusChip :status="action.riskLevel" kind="risk" />
      <StatusChip :status="localStatus" kind="action" />
    </header>

    <p class="approval__summary">{{ action.summary }}</p>

    <dl class="approval__meta">
      <div><dt>工具</dt><dd><code>{{ action.toolName }}</code></dd></div>
      <div><dt>动作类型</dt><dd>{{ action.actionType }}</dd></div>
      <div><dt>过期时间</dt><dd>{{ expiresAt }}</dd></div>
      <div v-if="decidedBy"><dt>决策人</dt><dd>{{ decidedBy }}</dd></div>
    </dl>

    <button type="button" class="approval__toggle" @click="showPayload = !showPayload">
      {{ showPayload ? '隐藏请求内容' : '查看请求内容' }}
    </button>
    <pre v-if="showPayload" class="approval__payload">{{ JSON.stringify(action, null, 2) }}</pre>

    <footer class="approval__actions">
      <button
        v-if="canDecide"
        type="button"
        class="nx-btn nx-btn--primary"
        :disabled="busy || !mayApprove"
        :title="mayApprove ? undefined : '当前账号没有审批权限'"
        @click="decide(true)"
      >
        批准执行
      </button>
      <button v-if="canDecide" type="button" class="nx-btn" :disabled="busy" @click="decide(false)">
        拒绝
      </button>
      <span v-if="isDecided" class="nx-muted">该审批已处理完毕。</span>
      <span v-else-if="!mayApprove" class="nx-muted">当前账号无审批权限，可查看但不可决策。</span>
    </footer>

    <p class="approval__note">
      批准后服务端会重新校验请求内容（payload hash）与风控规则；内容若已变化，本次审批会被拒绝而不是照旧执行。
    </p>
  </div>
</template>

<style scoped lang="scss">
.approval {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid var(--nx-warning);
  border-radius: var(--nx-radius-stage);
  background: var(--nx-warning-soft);

  &--succeeded {
    border-color: var(--nx-success);
    background: var(--nx-success-soft);
  }

  &--rejected,
  &--expired {
    border-color: var(--nx-border-strong);
    background: var(--nx-surface-sunken);
  }

  &--failed {
    border-color: var(--nx-danger);
    background: var(--nx-danger-soft);
  }

  &__head {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  &__title {
    font-size: 13px;
    font-weight: 600;
  }

  &__summary {
    margin: 0;
    font-size: 13.5px;
    line-height: 1.6;
  }

  &__meta {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 4px 16px;
    margin: 0;
    font-size: 12px;

    > div {
      display: flex;
      gap: 6px;
    }

    dt {
      color: var(--nx-text-muted);
    }

    dd {
      margin: 0;
      word-break: break-all;
    }
  }

  &__toggle {
    align-self: flex-start;
    border: none;
    background: transparent;
    color: var(--nx-primary);
    font-family: inherit;
    font-size: 12px;
    cursor: pointer;
    padding: 0;
  }

  &__payload {
    max-height: 240px;
    margin: 0;
    padding: 10px;
    overflow: auto;
    border-radius: var(--nx-radius-control);
    background: var(--nx-surface);
    font-size: 11.5px;
    line-height: 1.5;
  }

  &__actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }

  &__note {
    margin: 0;
    font-size: 11px;
    color: var(--nx-text-muted);
    line-height: 1.6;
  }
}
</style>
