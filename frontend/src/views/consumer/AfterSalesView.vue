<script setup lang="ts">
/**
 * Buyer-side after-sales list + application form.
 *
 * The requested amount is capped by the server's `refundable_amount` on the order.
 * The input's `max` is a convenience: the backend re-validates and answers
 * `REFUND_EXCEEDS_PAID_AMOUNT` / `REFUND_EXCEEDS_ITEM_AMOUNT` regardless.
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { afterSaleApi, orderApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { fromMajorString, toMajorString } from '@/utils/money'
import { normalizeError } from '@/api/error'
import { newTraceId } from '@/utils/trace'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'

const route = useRoute()
const notifications = useNotificationStore()

const FILTERS = [
  { value: '', label: '全部' },
  { value: 'PROCESSING', label: '处理中' },
  { value: 'REFUNDED', label: '已退款' },
  { value: 'PARTIAL_REFUNDED', label: '部分退款' },
]

const activeStatus = ref('')
const formOpen = ref(false)
const busy = ref(false)

const {
  data: afterSaleData,
  status,
  error,
  execute,
} = useAsyncState(
  () => afterSaleApi.list(activeStatus.value ? { status: activeStatus.value } : {}),
  { immediate: true },
)

const afterSales = computed(() => afterSaleData.value ?? [])
const orderNoInput = ref(typeof route.query.order_no === 'string' ? route.query.order_no : '')

const form = reactive({
  type: 'REFUND_ONLY' as 'REFUND_ONLY' | 'RETURN_REFUND',
  amountYuan: '',
  reason: '',
  description: '',
})

/** Server-provided ceiling for the order being applied against. */
const {
  data: targetOrder,
  execute: loadTargetOrder,
} = useAsyncState(
  () => (orderNoInput.value ? orderApi.detail(orderNoInput.value) : Promise.resolve(null)),
  { immediate: false },
)

const refundable = computed(() => targetOrder.value?.refundable_amount ?? 0)

onMounted(async () => {
  if (orderNoInput.value) {
    await loadTargetOrder()
    form.amountYuan = toMajorString(refundable.value)
    formOpen.value = true
  }
})

function changeFilter(value: string): void {
  activeStatus.value = value
  void execute()
}

async function lookupOrder(): Promise<void> {
  await loadTargetOrder()
  if (targetOrder.value) {
    form.amountYuan = toMajorString(refundable.value)
    formOpen.value = true
  }
}

async function submit(): Promise<void> {
  if (!targetOrder.value) {
    notifications.warning('请先查询需要售后的订单')
    return
  }
  const amount = fromMajorString(form.amountYuan)
  if (amount === null || amount <= 0) {
    notifications.warning('请输入有效的退款金额')
    return
  }
  if (amount > refundable.value) {
    // Friendly pre-check only; the server still enforces the cap.
    notifications.warning('退款金额超过可退余额', `最多可退 ${refundable.value} 分`)
    return
  }

  busy.value = true
  try {
    await afterSaleApi.apply({
      order_no: targetOrder.value.order_no,
      type: form.type,
      items: targetOrder.value.snapshot.items.map((item) => ({
        order_item_id: item.id,
        quantity: item.quantity,
      })),
      requested_amount: amount,
      reason: form.reason || '用户申请售后',
      description: form.description || undefined,
      client_request_id: `as-${newTraceId()}`,
    })
    notifications.success('售后申请已提交', '商家审核后会在订单中体现进度')
    formOpen.value = false
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('提交失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="nx-container aftersales">
    <div class="aftersales__head">
      <h1 class="nx-page-title">售后服务</h1>
      <button type="button" class="nx-btn nx-btn--primary" @click="formOpen = !formOpen">
        {{ formOpen ? '收起申请' : '申请售后' }}
      </button>
    </div>

    <section v-if="formOpen" class="nx-card aftersales__form">
      <div class="nx-card__body">
        <h2 class="nx-section-title">发起售后申请</h2>

        <div class="aftersales__row">
          <label class="aftersales__field">
            <span>订单号</span>
            <input v-model="orderNoInput" placeholder="例如 NX20250101..." />
          </label>
          <button type="button" class="nx-btn" :disabled="!orderNoInput" @click="lookupOrder()">查询订单</button>
        </div>

        <p v-if="targetOrder" class="nx-muted aftersales__hint">
          可退余额 <PriceText :amount="refundable" size="sm" />（实付 <PriceText :amount="targetOrder.paid_amount" size="sm" muted /> − 已退
          <PriceText :amount="targetOrder.refunded_amount" size="sm" muted />）
        </p>
        <p v-else-if="orderNoInput" class="aftersales__hint aftersales__hint--warn">
          未查询到该订单，请检查订单号。
        </p>

        <div class="aftersales__row">
          <label class="aftersales__field">
            <span>售后类型</span>
            <select v-model="form.type">
              <option value="REFUND_ONLY">仅退款</option>
              <option value="RETURN_REFUND">退货退款</option>
            </select>
          </label>
          <label class="aftersales__field">
            <span>退款金额（元）</span>
            <input v-model="form.amountYuan" inputmode="decimal" />
          </label>
        </div>

        <label class="aftersales__field">
          <span>原因</span>
          <input v-model="form.reason" maxlength="100" placeholder="例如：商品与描述不符" />
        </label>

        <label class="aftersales__field">
          <span>补充说明</span>
          <textarea v-model="form.description" rows="3" maxlength="500" />
        </label>

        <button type="button" class="nx-btn nx-btn--primary" :disabled="busy || !targetOrder" @click="submit()">
          提交申请
        </button>
        <p class="nx-muted aftersales__note">
          金额以「分」为单位提交；服务端会独立校验是否超过可退上限，前端校验只是提示。
        </p>
      </div>
    </section>

    <div class="aftersales__filters">
      <div class="nx-pills">
        <button
          v-for="filter in FILTERS"
          :key="filter.value"
          type="button"
          class="nx-pill"
          :class="{ 'nx-pill--active': activeStatus === filter.value }"
          @click="changeFilter(filter.value)"
        >
          {{ filter.label }}
        </button>
      </div>
    </div>

    <StateView
      :state="status"
      :error="error"
      :title="status === 'empty' ? '暂无售后记录' : undefined"
      :description="status === 'empty' ? '订单发生问题时可在这里发起退款或退货。' : undefined"
      @retry="execute()"
    >
      <div class="aftersales__list">
        <article v-for="record in afterSales" :key="record.id" class="aftersales__item nx-card">
          <div class="nx-card__body">
            <header class="aftersales__item-head">
              <span class="aftersales__no">售后单 {{ record.after_sale_no }}</span>
              <StatusChip :status="record.status" kind="aftersale" />
              <span class="nx-muted">{{ new Date(record.created_at).toLocaleString() }}</span>
            </header>

            <p class="aftersales__reason">原因：{{ record.reason }}</p>

            <dl class="aftersales__amounts">
              <div><dt>申请金额</dt><dd><PriceText :amount="record.requested_amount" size="sm" muted /></dd></div>
              <div><dt>核准金额</dt><dd><PriceText :amount="record.approved_amount" size="sm" muted /></dd></div>
              <div><dt>已退金额</dt><dd><PriceText :amount="record.refunded_amount" size="sm" muted /></dd></div>
            </dl>

            <p v-if="record.reject_reason" class="aftersales__reject">驳回原因：{{ record.reject_reason }}</p>

            <RouterLink
              :to="{ name: 'after-sale-detail', params: { afterSaleNo: record.after_sale_no } }"
              class="nx-btn nx-btn--ghost"
            >
              查看详情
            </RouterLink>
          </div>
        </article>
      </div>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.aftersales {
  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
  }

  &__form {
    margin-bottom: 18px;
  }

  &__row {
    display: flex;
    gap: 12px;
    align-items: flex-end;
    margin-bottom: 12px;
    flex-wrap: wrap;
  }

  &__field {
    display: flex;
    flex-direction: column;
    gap: 5px;
    flex: 1;
    min-width: 180px;
    font-size: 13px;

    input,
    select,
    textarea {
      padding: 8px 10px;
      border: 1px solid var(--nx-border-strong);
      border-radius: var(--nx-radius-control);
      background: var(--nx-surface);
      color: var(--nx-text);
      font-family: inherit;
      font-size: 13px;
    }

    textarea {
      resize: vertical;
    }
  }

  &__hint {
    margin: 0 0 12px;
    font-size: 12px;

    &--warn {
      color: var(--nx-warning);
    }
  }

  &__note {
    margin: 10px 0 0;
    font-size: 11.5px;
  }

  &__filters {
    margin-bottom: 14px;
  }

  &__list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  &__item-head {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 13px;
  }

  &__no {
    font-weight: 600;
  }

  &__item-head > :last-child {
    margin-left: auto;
  }

  &__reason {
    margin: 10px 0;
    font-size: 13px;
  }

  &__amounts {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 8px;
    margin: 0 0 10px;

    > div {
      display: flex;
      flex-direction: column;
      gap: 2px;
      font-size: 12px;
    }

    dt {
      color: var(--nx-text-muted);
    }

    dd {
      margin: 0;
      font-size: 14px;
    }
  }

  &__reject {
    margin: 0 0 10px;
    color: var(--nx-danger);
    font-size: 12.5px;
  }
}
</style>
