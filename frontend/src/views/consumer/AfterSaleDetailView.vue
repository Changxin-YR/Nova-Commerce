<script setup lang="ts">
/** Buyer-side after-sales detail: request, refund records and merchant decision. */
import { computed, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { afterSaleApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'

const route = useRoute()
const notifications = useNotificationStore()

const afterSaleNo = computed(() => String(route.params.afterSaleNo ?? ''))
const busy = ref(false)

const {
  data: record,
  status,
  error,
  execute,
} = useAsyncState(() => afterSaleApi.detail(afterSaleNo.value), { immediate: true })

const canCancel = computed(() => record.value?.status === 'PROCESSING')

async function cancel(): Promise<void> {
  busy.value = true
  try {
    await afterSaleApi.cancel(afterSaleNo.value)
    notifications.success('售后申请已撤销')
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('撤销失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="nx-container as-detail">
    <StateView :state="status" :error="error" @retry="execute()">
      <div v-if="record" class="as-detail__body">
        <header class="as-detail__head">
          <div>
            <h1 class="nx-page-title">售后单 {{ record.after_sale_no }}</h1>
            <p class="nx-muted">
              关联订单
              <RouterLink :to="{ name: 'order-detail', params: { orderNo: record.order_no } }">
                {{ record.order_no }}
              </RouterLink>
            </p>
          </div>
          <StatusChip :status="record.status" kind="aftersale" />
        </header>

        <section class="nx-card">
          <div class="nx-card__body">
            <h2 class="nx-section-title">申请内容</h2>
            <dl class="as-detail__list">
              <div><dt>类型</dt><dd>{{ record.type === 'REFUND_ONLY' ? '仅退款' : '退货退款' }}</dd></div>
              <div><dt>原因</dt><dd>{{ record.reason }}</dd></div>
              <div v-if="record.description"><dt>说明</dt><dd>{{ record.description }}</dd></div>
              <div><dt>申请金额</dt><dd><PriceText :amount="record.requested_amount" size="sm" muted /></dd></div>
              <div><dt>核准金额</dt><dd><PriceText :amount="record.approved_amount" size="sm" muted /></dd></div>
              <div><dt>已退金额</dt><dd><PriceText :amount="record.refunded_amount" size="sm" muted /></dd></div>
              <div v-if="record.reject_reason"><dt>驳回原因</dt><dd>{{ record.reject_reason }}</dd></div>
            </dl>
          </div>
        </section>

        <section class="nx-card">
          <div class="nx-card__body">
            <h2 class="nx-section-title">退款记录</h2>
            <ul v-if="record.refunds.length" class="as-detail__refunds">
              <li v-for="refund in record.refunds" :key="refund.id">
                <div>
                  <strong><PriceText :amount="refund.amount" size="md" /></strong>
                  <StatusChip :status="refund.status" />
                </div>
                <p class="nx-muted">
                  {{ new Date(refund.created_at).toLocaleString() }}
                  <template v-if="refund.completed_at">
                    · 完成 {{ new Date(refund.completed_at).toLocaleString() }}
                  </template>
                </p>
                <p v-if="refund.reason" class="nx-muted">{{ refund.reason }}</p>
              </li>
            </ul>
            <p v-else class="nx-muted">暂无退款流水。</p>
          </div>
        </section>

        <div class="as-detail__actions">
          <button v-if="canCancel" type="button" class="nx-btn" :disabled="busy" @click="cancel()">
            撤销申请
          </button>
          <RouterLink :to="{ name: 'after-sales' }" class="nx-btn nx-btn--ghost">返回列表</RouterLink>
        </div>
      </div>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.as-detail {
  &__body {
    display: flex;
    flex-direction: column;
    gap: 14px;
    margin-top: 12px;
  }

  &__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  &__list {
    margin: 0;

    > div {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      padding: 7px 0;
      border-bottom: 1px dashed var(--nx-border);
      font-size: 13px;
    }

    dt {
      color: var(--nx-text-muted);
    }

    dd {
      margin: 0;
      text-align: right;
    }
  }

  &__refunds {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin: 0;
    padding: 0;
    list-style: none;

    li {
      padding: 10px 12px;
      border-radius: var(--nx-radius-stage);
      background: var(--nx-surface-sunken);
      font-size: 13px;
    }

    p {
      margin: 4px 0 0;
      font-size: 12px;
    }

    > li > div {
      display: flex;
      align-items: center;
      gap: 10px;
    }
  }

  &__actions {
    display: flex;
    gap: 8px;
  }
}
</style>
