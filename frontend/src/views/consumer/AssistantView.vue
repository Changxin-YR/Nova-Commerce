<script setup lang="ts">
/**
 * Consumer AI assistant — a thin surface over the shared thread store.
 *
 * It renders the SAME store the console workspace uses, so a conversation started
 * here can be continued there. All streaming/cancel/approval state lives in
 * `useAiThreadStore`; this page only draws it (§105: no UI state in stores, no
 * stream state duplicated in a component).
 */
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { useAiThreadStore } from '@/stores/aiThread'
import StateView from '@/components/ui/StateView.vue'
import MessageBlocks from '@/components/agent/MessageBlocks.vue'

const thread = useAiThreadStore()
const draft = ref('')

/** §108 agent states mapped onto the shared StateView. */
const surfaceState = computed(() => {
  if (thread.status === 'waiting_approval') return 'waiting_approval' as const
  if (thread.status === 'failed') return 'failed' as const
  if (thread.status === 'cancelled') return 'cancelled' as const
  return null
})

const messages = computed(() => thread.messages)

function send(): void {
  const text = draft.value.trim()
  if (!text || thread.isRunning) return
  draft.value = ''
  thread.send(text, 'assistant')
}

function onKeydown(event: KeyboardEvent): void {
  // Enter sends; Shift+Enter inserts a newline.
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    send()
  }
}
</script>

<template>
  <div class="assistant">
    <div class="assistant__inner nx-container">
      <header class="assistant__head">
        <div>
          <h1 class="nx-page-title">AI 助手</h1>
          <p class="nx-muted">
            可以咨询商品、订单、售后政策。涉及写操作会先给出审批卡片，不会直接执行。
          </p>
        </div>
        <RouterLink :to="{ name: 'search' }" class="nx-btn nx-btn--ghost">去逛商品</RouterLink>
      </header>

      <div class="assistant__thread">
        <div v-if="messages.length === 0" class="assistant__placeholder">
          <span class="assistant__placeholder-icon" aria-hidden="true">✳</span>
          <p class="nx-muted">试试问：「我的订单什么时候发货？」或「这款笔记本支持多少瓦快充？」</p>
        </div>

        <article
          v-for="message in messages"
          :key="message.id"
          class="assistant__message"
          :class="`assistant__message--${message.role}`"
        >
          <div class="assistant__avatar" aria-hidden="true">
            {{ message.role === 'user' ? '我' : 'AI' }}
          </div>
          <div class="assistant__bubble">
            <MessageBlocks :blocks="message.blocks" />
          </div>
        </article>

        <StateView
          v-if="surfaceState"
          :state="surfaceState"
          :error="thread.error"
          compact
          hide-retry
          @cancel="thread.cancel()"
        />
      </div>

      <form class="assistant__composer" @submit.prevent="send">
        <textarea
          v-model="draft"
          rows="2"
          class="assistant__input"
          placeholder="输入问题，Enter 发送，Shift+Enter 换行"
          :disabled="thread.isRunning"
          @keydown="onKeydown"
        />
        <div class="assistant__composer-actions">
          <button v-if="thread.isRunning" type="button" class="nx-btn" @click="thread.cancel()">
            取消
          </button>
          <button type="submit" class="nx-btn nx-btn--primary" :disabled="thread.isRunning || !draft.trim()">
            发送
          </button>
        </div>
      </form>

      <p class="nx-muted assistant__note">
        回复中的引用均来自知识库检索结果；界面不会展示模型的隐藏推理过程。
      </p>
    </div>
  </div>
</template>

<style scoped lang="scss">
.assistant {
  padding: 24px 0 32px;

  &__inner {
    display: flex;
    flex-direction: column;
    gap: 16px;
    max-width: 860px;
  }

  &__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  &__thread {
    display: flex;
    flex-direction: column;
    gap: 14px;
    min-height: 320px;
    padding: 18px;
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
    border-radius: var(--nx-radius-card);
  }

  &__placeholder {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 40px 0;
    text-align: center;
  }

  &__message {
    display: flex;
    gap: 10px;
    align-items: flex-start;

    &--user {
      flex-direction: row-reverse;

      .assistant__bubble {
        background: var(--nx-primary-soft);
      }
    }
  }

  &__avatar {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    flex: 0 0 30px;
    border-radius: 50%;
    background: var(--nx-surface-sunken);
    font-size: 12px;
    color: var(--nx-text-secondary);
  }

  &__bubble {
    max-width: min(100%, 680px);
    padding: 10px 14px;
    border-radius: var(--nx-radius-stage);
    background: var(--nx-surface-sunken);
    font-size: 14px;
    line-height: 1.65;
  }

  &__composer {
    display: flex;
    gap: 10px;
    align-items: flex-end;
  }

  &__input {
    flex: 1;
    padding: 10px 12px;
    border: 1px solid var(--nx-border-strong);
    border-radius: var(--nx-radius-control);
    background: var(--nx-surface);
    color: var(--nx-text);
    font-family: inherit;
    font-size: 14px;
    resize: vertical;

    &:focus {
      border-color: var(--nx-primary);
      outline: none;
    }
  }

  &__composer-actions {
    display: flex;
    gap: 8px;
  }

  &__note {
    font-size: 11.5px;
  }
}
</style>
