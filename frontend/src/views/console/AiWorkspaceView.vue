<script setup lang="ts">
/**
 * AI Workspace (§101).
 *
 * Five tabs: Assistant · Operations · Analytics · Pending Actions · Agent Runs.
 *
 * Everything live comes from `useAiThreadStore`, which consumes the 12 frozen SSE
 * events. This page adds NO new event handling and NEVER displays reasoning: there is
 * no chain-of-thought channel to display (§132).
 *
 * ASSUMED FIELD SHAPES \u2014 READ BEFORE TRUSTING THIS LIST (API_CONTRACT.md \u00a710)
 *  \u00a710 states plainly that the full `AgentRun` and `PendingAction` shapes are NOT frozen; they land
 *  with the rest of the agent contract in Phase 10/13. Everything this page reads off those two
 *  entities is therefore an ASSUMPTION, not a contract:
 *
 *    AgentRun:      id, thread_id, agent_name, status, query, tokens_used, cost_amount, started_at,
 *                   finished_at, pending_action_id, error_code, error_message
 *    PendingAction: id, agent_run_id, action_type, tool_name, summary, risk_level, status, payload,
 *                   payload_hash, diff, requested_by, decided_by, expires_at, created_at
 *
 *  They live in `src/types/domain.ts` today. The list is kept deliberately NARROW for that reason:
 *  no rich shape is invented here that a Phase 10/13 freeze would contradict, so reconciling this
 *  page later means touching the field list above and nothing else.
 *
 * The \u00a7108 agent states are surfaced explicitly:
 *   Streaming · WaitingApproval · Failed · Cancelled
 */
import { computed, ref, watch } from 'vue'
import { agentApi, governanceApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useAiThreadStore } from '@/stores/aiThread'
import { useNotificationStore } from '@/stores/notification'
import { pendingActionBlockedReason, pendingActionFlags } from '@/domain/governance/availability'
import { normalizeError } from '@/api/error'
import { AGENT_TABS, type AgentName } from '@/config/app'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'
import MessageBlocks from '@/components/agent/MessageBlocks.vue'

type Tab = 'assistant' | 'operations' | 'analytics' | 'pending' | 'runs'

const thread = useAiThreadStore()
const notifications = useNotificationStore()

const activeTab = ref<Tab>('assistant')
const draft = ref('')

const TABS: { value: Tab; label: string }[] = [
  { value: 'assistant', label: 'Assistant' },
  { value: 'operations', label: 'Operations' },
  { value: 'analytics', label: 'Analytics' },
  { value: 'pending', label: 'Pending Actions' },
  { value: 'runs', label: 'Agent Runs' },
]

/** Tab -> agent graph entry point. Pending/Runs tabs do not start a run. */
const AGENT_FOR_TAB: Partial<Record<Tab, AgentName>> = {
  assistant: 'assistant',
  operations: 'operations',
  analytics: 'analytics',
}

const agentForTab = computed<AgentName | null>(() => AGENT_FOR_TAB[activeTab.value] ?? null)

// -- Pending actions tab ------------------------------------------------------
const {
  data: pendingData,
  status: pendingStatus,
  error: pendingError,
  execute: loadPending,
} = useAsyncState(() => governanceApi.pendingActions({ page: 1, page_size: 20 }), {
  immediate: false,
})

// -- Agent runs tab ----------------------------------------------------------
const {
  data: runsData,
  status: runsStatus,
  error: runsError,
  execute: loadRuns,
} = useAsyncState(() => agentApi.runs({ page: 1, page_size: 20 }), { immediate: false })

const {
  data: toolsData,
  execute: loadTools,
} = useAsyncState(() => agentApi.tools(), { immediate: false })

watch(activeTab, (tab) => {
  if (tab === 'pending') void loadPending()
  if (tab === 'runs') void loadRuns()
  if (tab === 'operations') void loadTools()
}, { immediate: true })

// -- Streaming surface state (§108) -------------------------------------------
const surfaceState = computed(() => {
  if (thread.status === 'waiting_approval') return 'waiting_approval' as const
  if (thread.status === 'failed') return 'failed' as const
  if (thread.status === 'cancelled') return 'cancelled' as const
  if (thread.isStreaming) return 'streaming' as const
  return null
})

const canAsk = computed(() => Boolean(agentForTab.value) && !thread.isRunning)

function ask(): void {
  const text = draft.value.trim()
  const agent = agentForTab.value
  if (!text || !agent || thread.isRunning) return
  draft.value = ''
  thread.send(text, agent)
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    ask()
  }
}

const pendingList = computed(() => pendingData.value?.items ?? [])
const runs = computed(() => runsData.value?.items ?? [])

async function decideFromList(actionId: string, approved: boolean, payloadHash: string): Promise<void> {
  try {
    const payload = {
      payload_hash: payloadHash,
      decision_reason: approved ? '列表内快速审批' : '列表内快速拒绝',
    }
    if (approved) await governanceApi.approve(actionId, payload)
    else await governanceApi.reject(actionId, payload)
    notifications.success(approved ? '已批准' : '已拒绝')
    await loadPending()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('审批失败', normalized.message, normalized.code, normalized.traceId)
  }
}
</script>

<template>
  <div class="ai">
    <nav class="ai__tabs" role="tablist" aria-label="AI 工作台标签">
      <button
        v-for="tab in TABS"
        :key="tab.value"
        type="button"
        role="tab"
        class="nx-pill"
        :class="{ 'nx-pill--active': activeTab === tab.value }"
        :aria-selected="activeTab === tab.value"
        @click="activeTab = tab.value"
      >
        {{ tab.label }}
        <span v-if="tab.value === 'pending' && pendingList.length" class="ai__tab-count">
          {{ pendingList.length }}
        </span>
      </button>
    </nav>

    <!-- Conversation tabs ------------------------------------------------- -->
    <div v-if="agentForTab" class="ai__layout">
      <section class="ai__chat nx-card">
        <div class="nx-card__body ai__chat-body">
          <div v-if="thread.messages.length === 0" class="ai__placeholder">
            <p class="nx-muted">
              {{ AGENT_TABS.find((t) => t.value === agentForTab)?.description }}
            </p>
            <p class="nx-muted ai__placeholder-hint">
              对话通过 SSE 流式返回；写操作会先进入待审批队列，不会直接落库。
            </p>
          </div>

          <article
            v-for="message in thread.messages"
            :key="message.id"
            class="ai__message"
            :class="`ai__message--${message.role}`"
          >
            <div class="ai__message-role" aria-hidden="true">
              {{ message.role === 'user' ? '我' : 'AI' }}
            </div>
            <div class="ai__message-body">
              <MessageBlocks :blocks="message.blocks" />
            </div>
          </article>

          <StateView
            v-if="surfaceState"
            :state="surfaceState"
            :error="thread.error"
            compact
            @cancel="thread.cancel()"
          />
        </div>

        <form class="ai__composer" @submit.prevent="ask">
          <textarea
            v-model="draft"
            rows="2"
            class="ai__input"
            :placeholder="`向 ${agentForTab} 提问…（Enter 发送）`"
            :disabled="!canAsk"
            @keydown="onKeydown"
          />
          <button v-if="thread.isRunning" type="button" class="nx-btn" @click="thread.cancel()">取消</button>
          <button type="submit" class="nx-btn nx-btn--primary" :disabled="!canAsk || !draft.trim()">
            发送
          </button>
        </form>
      </section>

      <aside class="ai__aside">
        <section class="nx-card">
          <div class="nx-card__body">
            <h3 class="nx-section-title">本次运行</h3>
            <dl class="ai__meta">
              <div><dt>状态</dt><dd>{{ thread.status }}</dd></div>
              <div v-if="thread.routeInfo"><dt>路由</dt><dd>{{ thread.routeInfo.route }}</dd></div>
              <div v-if="thread.activeRunId"><dt>Run ID</dt><dd><code>{{ thread.activeRunId }}</code></dd></div>
            </dl>
            <p v-if="thread.routeInfo" class="nx-muted ai__route-reason">
              {{ thread.routeInfo.reason }}
            </p>
          </div>
        </section>

        <section v-if="thread.plan.length" class="nx-card">
          <div class="nx-card__body">
            <h3 class="nx-section-title">执行计划</h3>
            <ol class="ai__plan">
              <li v-for="step in thread.plan" :key="step.step_id" :class="`ai__plan--${step.status}`">
                <span class="ai__plan-title">{{ step.title }}</span>
                <span v-if="step.tool_name" class="nx-muted">{{ step.tool_name }}</span>
              </li>
            </ol>
          </div>
        </section>

        <section v-if="thread.toolCalls.length" class="nx-card">
          <div class="nx-card__body">
            <h3 class="nx-section-title">工具调用</h3>
            <ul class="ai__tools">
              <li v-for="tool in thread.toolCalls" :key="tool.tool_call_id">
                <span class="ai__tool-name">{{ tool.tool_name }}</span>
                <StatusChip :status="tool.status === 'succeeded' ? 'SUCCEEDED' : tool.status === 'failed' ? 'FAILED' : tool.status === 'running' ? 'EXECUTING' : 'PENDING'" kind="action" />
                <span v-if="tool.duration_ms !== undefined" class="nx-muted">{{ tool.duration_ms }} ms</span>
              </li>
            </ul>
            <p class="nx-muted ai__note">
              界面只展示工具的名称、状态与安全摘要；不会展示模型的隐藏推理内容。
            </p>
          </div>
        </section>

        <section v-if="activeTab === 'operations' && (toolsData ?? []).length" class="nx-card">
          <div class="nx-card__body">
            <h3 class="nx-section-title">可用工具</h3>
            <ul class="ai__tool-list">
              <li v-for="tool in toolsData ?? []" :key="tool.name">
                <div class="ai__tool-list-head">
                  <code>{{ tool.name }}</code>
                  <StatusChip :status="tool.risk_level" kind="risk" />
                </div>
                <p class="nx-muted">{{ tool.description }}</p>
                <p v-if="tool.requires_approval" class="ai__tool-approval">该工具需要人工审批</p>
              </li>
            </ul>
          </div>
        </section>
      </aside>
    </div>

    <!-- Pending actions tab ----------------------------------------------- -->
    <StateView
      v-else-if="activeTab === 'pending'"
      :state="pendingStatus"
      :error="pendingError"
      :title="pendingStatus === 'empty' ? '没有待审批操作' : undefined"
      :description="pendingStatus === 'empty' ? '智能体提出的写操作会出现在这里等待人工决策。' : undefined"
      @retry="loadPending()"
    >
      <div class="ai__pending">
        <article v-for="action in pendingList" :key="action.id" class="nx-card">
          <div class="nx-card__body">
            <header class="ai__pending-head">
              <strong>{{ action.summary }}</strong>
              <StatusChip :status="action.risk_level" kind="risk" />
              <StatusChip :status="action.status" kind="action" />
            </header>

            <dl class="ai__meta">
              <div><dt>工具</dt><dd><code>{{ action.tool_name }}</code></dd></div>
              <div><dt>类型</dt><dd>{{ action.action_type }}</dd></div>
              <div><dt>发起人</dt><dd>{{ action.requested_by }}</dd></div>
              <div><dt>过期时间</dt><dd>{{ new Date(action.expires_at).toLocaleString() }}</dd></div>
            </dl>

            <ul v-if="action.diff?.length" class="ai__diff">
              <li v-for="entry in action.diff" :key="entry.field">
                <span class="nx-muted">{{ entry.field }}</span>
                <span class="ai__diff-before">{{ entry.before }}</span>
                <span aria-hidden="true">→</span>
                <span class="ai__diff-after">{{ entry.after }}</span>
              </li>
            </ul>

            <div class="ai__pending-actions">
              <!--
                Decidability comes from the TESTED `pendingActionFlags`, not an inline status
                comparison: only PENDING is decidable (section 101), and that module is the single
                place the rule lives, so this row cannot drift from it.
              -->
              <button
                v-if="pendingActionFlags(action).approve"
                type="button"
                class="nx-btn nx-btn--primary"
                @click="decideFromList(action.id, true, action.payload_hash)"
              >
                批准
              </button>
              <button
                v-if="pendingActionFlags(action).reject"
                type="button"
                class="nx-btn"
                @click="decideFromList(action.id, false, action.payload_hash)"
              >
                拒绝
              </button>
              <span
                v-else
                class="nx-muted"
                :title="pendingActionBlockedReason(action.status)"
              >
                已处理（{{ action.decided_by ?? '—' }}）
              </span>
            </div>
          </div>
        </article>
      </div>
    </StateView>

    <!-- Agent runs tab ---------------------------------------------------- -->
    <StateView
      v-else
      :state="runsStatus"
      :error="runsError"
      :title="runsStatus === 'empty' ? '暂无运行记录' : undefined"
      @retry="loadRuns()"
    >
      <table class="nx-table">
        <thead>
          <tr>
            <th>Run ID</th>
            <th>智能体</th>
            <th>状态</th>
            <th>问题</th>
            <th>Token</th>
            <th>成本</th>
            <th>开始时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="run in runs" :key="run.id">
            <td><code>{{ run.id.slice(0, 8) }}</code></td>
            <td>{{ run.agent_name }}</td>
            <td><StatusChip :status="run.status" kind="action" /></td>
            <td class="ai__runs-query">{{ run.query }}</td>
            <td>{{ run.tokens_used ?? '—' }}</td>
            <td>
            <PriceText v-if="run.cost_amount !== undefined" :amount="run.cost_amount" size="sm" muted :grouping="false" />
            <span v-else>—</span>
          </td>
            <td>{{ new Date(run.started_at).toLocaleString() }}</td>
          </tr>
        </tbody>
      </table>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.ai {
  &__tabs {
    display: flex;
    gap: 4px;
    margin-bottom: 14px;
    flex-wrap: wrap;
  }

  &__tab-count {
    margin-left: 6px;
    padding: 0 6px;
    border-radius: var(--nx-radius-pill);
    background: var(--nx-danger);
    color: #fff;
    font-size: 11px;
  }

  &__layout {
    display: grid;
    grid-template-columns: 1fr 320px;
    gap: 16px;
    align-items: start;
  }

  &__chat {
    display: flex;
    flex-direction: column;
    min-height: 520px;
  }

  &__chat-body {
    display: flex;
    flex-direction: column;
    gap: 14px;
    flex: 1;
    max-height: 62vh;
    overflow-y: auto;
  }

  &__placeholder {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 48px 16px;
    text-align: center;
  }

  &__placeholder-hint {
    font-size: 12px;
  }

  &__message {
    display: flex;
    gap: 10px;

    &--user {
      flex-direction: row-reverse;

      .ai__message-body {
        background: var(--nx-primary-soft);
      }
    }
  }

  &__message-role {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 28px;
    height: 28px;
    flex: 0 0 28px;
    border-radius: 50%;
    background: var(--nx-surface-sunken);
    font-size: 11px;
    color: var(--nx-text-secondary);
  }

  &__message-body {
    max-width: min(100%, 760px);
    padding: 10px 14px;
    border-radius: var(--nx-radius-stage);
    background: var(--nx-surface-sunken);
    font-size: 13.5px;
    line-height: 1.65;
  }

  &__composer {
    display: flex;
    gap: 8px;
    align-items: flex-end;
    padding: 12px 16px;
    border-top: 1px solid var(--nx-border);
  }

  &__input {
    flex: 1;
    padding: 9px 12px;
    border: 1px solid var(--nx-border-strong);
    border-radius: var(--nx-radius-control);
    background: var(--nx-surface);
    color: var(--nx-text);
    font-family: inherit;
    font-size: 13.5px;
    resize: vertical;
  }

  &__aside {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  &__meta {
    margin: 0;

    > div {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      padding: 5px 0;
      font-size: 12.5px;
    }

    dt {
      color: var(--nx-text-muted);
    }

    dd {
      margin: 0;
      word-break: break-all;
      text-align: right;
    }
  }

  &__route-reason {
    margin: 8px 0 0;
    font-size: 11.5px;
  }

  &__plan {
    margin: 0;
    padding-left: 18px;
    font-size: 12.5px;

    li {
      padding: 3px 0;
    }

    &--succeeded {
      color: var(--nx-success);
    }

    &--running {
      color: var(--nx-primary);
      font-weight: 600;
    }

    &--failed {
      color: var(--nx-danger);
    }
  }

  &__plan-title {
    margin-right: 6px;
  }

  &__tools {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin: 0;
    padding: 0;
    list-style: none;

    li {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 12.5px;
    }
  }

  &__tool-name {
    font-weight: 600;
  }

  &__note {
    margin: 10px 0 0;
    font-size: 11px;
    line-height: 1.6;
  }

  &__tool-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin: 0;
    padding: 0;
    list-style: none;

    li {
      padding-bottom: 8px;
      border-bottom: 1px dashed var(--nx-border);
      font-size: 12.5px;
    }

    p {
      margin: 4px 0 0;
      font-size: 11.5px;
    }
  }

  &__tool-list-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  &__tool-approval {
    color: var(--nx-warning);
  }

  &__pending {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 14px;
  }

  &__pending-head {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 10px;
    font-size: 13.5px;
  }

  &__diff {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin: 10px 0;
    padding: 10px;
    border-radius: var(--nx-radius-control);
    background: var(--nx-surface-sunken);
    list-style: none;
    font-size: 12px;

    li {
      display: flex;
      align-items: center;
      gap: 8px;
    }
  }

  &__diff-before {
    color: var(--nx-danger);
    text-decoration: line-through;
  }

  &__diff-after {
    color: var(--nx-success);
  }

  &__pending-actions {
    display: flex;
    gap: 8px;
    margin-top: 10px;
  }


  &__runs-query {
    max-width: 320px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

@media (max-width: 1100px) {
  .ai__layout {
    grid-template-columns: 1fr;
  }
}
</style>
