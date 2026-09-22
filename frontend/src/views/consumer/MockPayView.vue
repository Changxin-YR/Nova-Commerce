<script setup lang="ts">
/**
 * MockPay (§100).
 *
 * A deliberately honest payment page: it does NOT mark anything paid. Pressing
 * "模拟支付成功" asks the backend to settle the payment through the SAME callback path
 * a real provider would use, and the page then RE-READS the payment and the order.
 * That is what makes "idempotent payment" demonstrable — pressing the button twice
 * produces one payment and `PAYMENT_ALREADY_PAID` on the second attempt.
 */
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { paymentApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { formatMoney } from '@/utils/money'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'

const route = useRoute()
const router = useRouter()
const notifications = useNotificationStore()

const paymentId = computed(() => String(route.params.paymentId ?? ''))
const paying = ref(false)
/** Set when the backend reports the payment is already settled. */
const alreadyPaid = ref(false)

const {
  data: payment,
  status,
  error,
  execute,
} = useAsyncState(() => paymentApi.detail(paymentId.value), { immediate: true })

const isPaid = computed(
  () => alreadyPaid.value || payment.value?.status === 'PAID' || payment.value?.status === 'REFUNDED',
)

async function pay(): Promise<void> {
  paying.value = true
  try {
    await paymentApi.mockPay(paymentId.value)
    // Re-read rather than trusting the write response's shape: the order page and
    // the payment page must agree with the database.
    await execute()
    notifications.success('支付成功', '支付回调已由服务端处理')
    if (payment.value?.order_no) {
      await router.push({ name: 'order-detail', params: { orderNo: payment.value.order_no } })
    }
  } catch (e) {
    const normalized = normalizeError(e)
    if (normalized.code === 60_001) {
      // PAYMENT_ALREADY_PAID: idempotency worked, this is not an error for the user.
      alreadyPaid.value = true
      notifications.info('该支付单已完成', '重复支付已被服务端拒绝（幂等生效）')
      await execute()
    } else {
      notifications.error('支付失败', normalized.message, normalized.code, normalized.traceId)
    }
  } finally {
    paying.value = false
  }
}

function goToOrder(): void {
  if (!payment.value?.order_no) return
  void router.push({ name: 'order-detail', params: { orderNo: payment.value.order_no } })
}

async function checkStatus(): Promise<void> {
  await execute()
}
</script>

<template>
  <div class="nx-container mock-pay">
    <StateView :state="status" :error="error" @retry="execute()">
      <div class="mock-pay__card nx-card">
        <div class="nx-card__body">
          <p class="mock-pay__badge">模拟支付通道</p>
          <h1 class="nx-page-title">订单 {{ payment?.order_no }}</h1>

          <p class="mock-pay__amount nx-money">{{ formatMoney(payment?.paid_amount || payment?.amount || 0) }}</p>

          <dl class="mock-pay__meta">
            <div>
              <dt>支付单号</dt>
              <dd><code>{{ payment?.id }}</code></dd>
            </div>
            <div>
              <dt>支付状态</dt>
              <dd>{{ payment?.status }}</dd>
            </div>
            <div v-if="payment?.expires_at">
              <dt>支付截止</dt>
              <dd>{{ new Date(payment.expires_at).toLocaleString() }}</dd>
            </div>
          </dl>

          <div class="mock-pay__actions">
            <button
              type="button"
              class="nx-btn nx-btn--primary"
              :disabled="paying || isPaid"
              @click="pay()"
            >
              {{ isPaid ? '已完成支付' : paying ? '处理中…' : '模拟支付成功' }}
            </button>
            <button type="button" class="nx-btn" :disabled="paying" @click="checkStatus()">
              刷新支付状态
            </button>
            <button type="button" class="nx-btn nx-btn--ghost" @click="goToOrder()">查看订单</button>
          </div>

          <p class="nx-muted mock-pay__note">
            该按钮不会在浏览器里修改任何状态：它请求服务端走一遍支付回调，再由服务端在校验金额与幂等后落库。
            重复点击只会得到一次支付结果。
          </p>
        </div>
      </div>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.mock-pay {
  &__card {
    max-width: 560px;
    margin: 24px auto 0;
  }

  &__badge {
    display: inline-block;
    margin: 0 0 10px;
    padding: 2px 10px;
    border-radius: var(--nx-radius-pill);
    background: var(--nx-warning-soft);
    color: var(--nx-warning);
    font-size: 11px;
  }

  &__amount {
    margin: 14px 0 18px;
    font-size: 30px;
    color: var(--nx-danger);
  }

  &__meta {
    margin: 0 0 20px;

    > div {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      padding: 6px 0;
      border-bottom: 1px dashed var(--nx-border);
      font-size: 13px;
    }

    dt {
      color: var(--nx-text-muted);
    }

    dd {
      margin: 0;
      text-align: right;
      word-break: break-all;
    }
  }

  &__actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }

  &__note {
    margin: 16px 0 0;
    font-size: 12px;
    line-height: 1.6;
  }
}
</style>
