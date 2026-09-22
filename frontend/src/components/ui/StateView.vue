<script lang="ts">
/**
 * Exported so pages and composables can type their status without importing the
 * component. Kept in a normal `<script>` block because `<script setup>` cannot
 * export types.
 */
export type UiState =
  | 'loading'
  | 'success'
  | 'empty'
  | 'error'
  | 'permission_denied'
  | 'streaming'
  | 'waiting_approval'
  | 'failed'
  | 'cancelled'
  | 'processing'
</script>

<script setup lang="ts">
/**
 * The ONE reusable implementation of the §108 UI-state contract.
 *
 * Core pages must implement: Loading, Success, Empty, Error, PermissionDenied.
 * Agent surfaces add: Streaming, WaitingApproval, Failed, Cancelled.
 * Knowledge surfaces add: Processing, Failed.
 *
 * `success` is the only state that renders the default slot; every other state
 * renders an explanation plus an action. Putting all of them in one component means
 * a page cannot quietly forget one — and reviewers can audit the visual language in
 * a single file.
 */
import { computed } from 'vue'
import type { NormalizedApiError } from '@/types/api'

const props = withDefaults(
  defineProps<{
    state: UiState
    /** Overrides the built-in copy for the state. */
    title?: string
    description?: string
    /** Normalized error; its `message`/`traceId` are shown instead of generic copy. */
    error?: NormalizedApiError | null
    /** Hide the retry button even for retryable states. */
    hideRetry?: boolean
    /** Compact vertical rhythm for embedding inside a card or table cell. */
    compact?: boolean
  }>(),
  { title: '', description: '', error: null, hideRetry: false, compact: false },
)

const emit = defineEmits<{
  (e: 'retry'): void
  (e: 'cancel'): void
  (e: 'login'): void
}>()

/** A permission failure needs a different affordance than a network failure. */
const isPermission = computed(() => props.state === 'permission_denied')

const copy = computed(() => {
  if (props.title || props.description) {
    return { title: props.title, description: props.description }
  }
  switch (props.state) {
    case 'loading':
      return { title: '加载中', description: '正在获取最新数据…' }
    case 'empty':
      return { title: '暂无数据', description: '这里还没有内容，换个筛选条件试试。' }
    case 'error':
      return { title: '加载失败', description: props.error?.message ?? '请稍后重试。' }
    case 'permission_denied':
      return {
        title: '没有访问权限',
        description: '当前账号无权查看该内容。如确需访问，请联系管理员开通权限。',
      }
    case 'streaming':
      return { title: '正在生成', description: '智能体正在处理，请稍候…' }
    case 'waiting_approval':
      return { title: '等待人工审批', description: '该操作存在风险，需审批通过后才会执行。' }
    case 'failed':
      return { title: '运行失败', description: props.error?.message ?? '本次运行未能完成。' }
    case 'cancelled':
      return { title: '已取消', description: '本次运行已被取消，不会产生任何副作用。' }
    case 'processing':
      return { title: '处理中', description: '文档正在解析与向量化，稍后自动刷新。' }
    default:
      return { title: '', description: '' }
  }
})

const showRetry = computed(
  () =>
    !props.hideRetry &&
    (props.state === 'error' || props.state === 'failed' || props.state === 'processing'),
)
const showCancel = computed(() => props.state === 'streaming' || props.state === 'waiting_approval')
/** A denied session can log in again; a denied role cannot. */
const showLogin = computed(
  () => isPermission.value && Boolean(props.error?.unauthenticated),
)
</script>

<template>
  <div
    v-if="state !== 'success'"
    class="nx-state"
    :class="[`nx-state--${state}`, { 'nx-state--compact': compact }]"
    role="status"
    aria-live="polite"
  >
    <div class="nx-state__icon" aria-hidden="true">
      <span v-if="state === 'loading' || state === 'streaming' || state === 'processing'" class="nx-state__spinner" />
      <span v-else-if="state === 'empty'">∅</span>
      <span v-else-if="isPermission">🔒</span>
      <span v-else-if="state === 'cancelled'">⏹</span>
      <span v-else-if="state === 'waiting_approval'">⏳</span>
      <span v-else>!</span>
    </div>

    <h4 class="nx-state__title">{{ copy.title }}</h4>
    <p class="nx-state__description">{{ copy.description }}</p>

    <p v-if="error?.traceId" class="nx-state__trace">
      trace_id: <code>{{ error.traceId }}</code>
    </p>

    <div v-if="showRetry || showCancel || showLogin" class="nx-state__actions">
      <button v-if="showRetry" type="button" class="nx-btn nx-btn--primary" @click="emit('retry')">
        重试
      </button>
      <button v-if="showLogin" type="button" class="nx-btn nx-btn--primary" @click="emit('login')">
        重新登录
      </button>
      <button v-if="showCancel" type="button" class="nx-btn" @click="emit('cancel')">取消运行</button>
    </div>
  </div>

  <slot v-else />
</template>

<style scoped lang="scss">
.nx-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 48px 24px;
  text-align: center;
  color: var(--nx-text);

  &--compact {
    padding: 20px 12px;
    gap: 4px;
  }

  &__icon {
    font-size: 24px;
    line-height: 1;
    opacity: 0.75;
  }

  &__title {
    margin: 0;
    font-size: 15px;
    font-weight: 600;
  }

  &__description {
    margin: 0;
    max-width: 46ch;
    font-size: 13px;
    color: var(--nx-text-muted);
  }

  &__trace {
    margin: 2px 0 0;
    font-size: 12px;
    color: var(--nx-text-muted);

    code {
      font-size: 12px;
    }
  }

  &__actions {
    display: flex;
    gap: 8px;
    margin-top: 6px;
  }

  &__spinner {
    display: inline-block;
    width: 22px;
    height: 22px;
    border: 2px solid var(--nx-border);
    border-top-color: var(--nx-primary);
    border-radius: 50%;
    animation: nx-spin 0.7s linear infinite;
  }
}

@keyframes nx-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
