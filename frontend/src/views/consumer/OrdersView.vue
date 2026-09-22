<script setup lang="ts">
/**
 * My orders. The cancelled/closed states matter here: a cancelled order must show
 * WHY it was cancelled rather than looking like an error.
 */
import { computed, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { orderApi, paymentApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { formatMoney } from '@/utils/money'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'
import type { OrderStatus } from '@/types/domain'

const router = useRouter()
const notifications = useNotificationStore()

const FILTERS: { value: string; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'PENDING_PAYMENT', label: '待付款' },
  { value: 'PROCESSING', label: '处理中' },
  { value: 'COMPLETED', label: '已完成' },
  { value: 'CANCELLED', label: '已取消' },
]

const activeStatus = ref('')

const {
  data: orderData,
  status,
  error,
  execute,
} = useAsyncState(
  () => orderApi.list(activeStatus.value ? { status: activeStatus.value } : {}),
  { immediate: true },
)

const orders = computed(() => orderData.value ?? [])
const busyOrderNo = ref('')

function changeFilter(value: string): void {
  activeStatus.value = value
  void execute()
}

async function cancelOrder(orderNo: string): Promise<void> {
  busyOrderNo.value = orderNo
  try {
    await orderApi.cancel(orderNo, { reason: '用户主动取消' })
    notifications.success('订单已取消')
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('取消失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busyOrderNo.value = ''
  }
}

async function confirmReceipt(orderNo: string): Promise<void> {
  busyOrderNo.value = orderNo
  try {
    await orderApi.confirmReceipt(orderNo)
    notifications.success('已确认收货')
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('确认收货失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busyOrderNo.value = ''
  }
}

function canCancel(status_: OrderStatus): boolean {
  return status_ === 'PENDING_PAYMENT' || status_ === 'PROCESSING'
}

function canConfirm(status_: OrderStatus): boolean {
  return status_ === 'PROCESSING'
}

/**
 * "Pay" looks up the payment record for the order and forwards to the mock channel.
 * The page never constructs a payment id or a paid state by itself.
 */
async function goPay(orderNo: string): Promise<void> {
  busyOrderNo.value = orderNo
  try {
    const payment = await paymentApi.byOrder(orderNo)
    await router.push({ name: 'mock-pay', params: { paymentId: payment.id } })
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('无法进入支付', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busyOrderNo.value = ''
  }
}
</script>

<template>
  <div class="nx-container orders">
    <div class="orders__head">
      <h1 class="nx-page-title">我的订单</h1>
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
      :title="status === 'empty' ? '还没有订单' : undefined"
      :description="status === 'empty' ? '下单后可以在这里查看物流与售后进度。' : undefined"
      @retry="execute()"
    >
      <div class="orders__list">
        <article v-for="order in orders" :key="order.id" class="orders__item nx-card">
          <div class="nx-card__body">
            <header class="orders__item-head">
              <span class="orders__no">订单号 {{ order.order_no }}</span>
              <span class="nx-muted">{{ new Date(order.created_at).toLocaleString() }}</span>
              <StatusChip :status="order.status" kind="order" />
            </header>

            <ul class="orders__items">
              <li v-for="item in order.snapshot.items.slice(0, 3)" :key="item.id">
                <img v-if="item.cover_url" :src="item.cover_url" :alt="item.product_title" />
                <span class="orders__item-info">
                  <strong>{{ item.product_title }}</strong>
                  <span class="nx-muted">
                    {{ Object.values(item.sku_specs).join(' / ') }} × {{ item.quantity }}
                  </span>
                </span>
                <span class="nx-money">{{ formatMoney(item.subtotal_amount) }}</span>
              </li>
            </ul>
            <p v-if="order.snapshot.items.length > 3" class="nx-muted">
              等 {{ order.snapshot.items.length }} 件商品
            </p>

            <footer class="orders__item-foot">
              <span class="nx-muted">收货人 {{ order.snapshot.receiver_name }}</span>
              <span class="nx-muted">已退款 {{ formatMoney(order.refunded_amount) }}</span>
              <span class="orders__total">
                应付 <span class="nx-money">{{ formatMoney(order.snapshot.payable_amount) }}</span>
              </span>
            </footer>

            <p v-if="order.cancel_reason" class="orders__cancel-reason">
              取消原因：{{ order.cancel_reason }}
            </p>

            <div class="orders__actions">
              <RouterLink
                :to="{ name: 'order-detail', params: { orderNo: order.order_no } }"
                class="nx-btn"
              >
                查看详情
              </RouterLink>
              <button
                v-if="order.status === 'PENDING_PAYMENT'"
                type="button"
                class="nx-btn nx-btn--primary"
                :disabled="busyOrderNo === order.order_no"
                @click="goPay(order.order_no)"
              >
                去支付
              </button>
              <button
                v-if="canCancel(order.status)"
                type="button"
                class="nx-btn"
                :disabled="busyOrderNo === order.order_no"
                @click="cancelOrder(order.order_no)"
              >
                取消订单
              </button>
              <button
                v-if="canConfirm(order.status)"
                type="button"
                class="nx-btn nx-btn--primary"
                :disabled="busyOrderNo === order.order_no"
                @click="confirmReceipt(order.order_no)"
              >
                确认收货
              </button>
              <RouterLink
                v-if="order.status === 'COMPLETED' || order.status === 'PROCESSING'"
                :to="{ name: 'after-sales', query: { order_no: order.order_no } }"
                class="nx-btn nx-btn--ghost"
              >
                申请售后
              </RouterLink>
            </div>
          </div>
        </article>
      </div>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.orders {
  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 16px;
  }

  &__list {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  &__item-head {
    display: flex;
    align-items: center;
    gap: 12px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--nx-border);
    font-size: 13px;
  }

  &__no {
    font-weight: 600;
  }

  &__item-head > :last-child {
    margin-left: auto;
  }

  &__items {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin: 12px 0;
    padding: 0;
    list-style: none;

    li {
      display: grid;
      grid-template-columns: 44px 1fr auto;
      align-items: center;
      gap: 12px;
      font-size: 13px;
    }

    img {
      width: 44px;
      height: 44px;
      border-radius: 8px;
      background: var(--nx-surface-stage);
      object-fit: contain;
    }
  }

  &__item-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  &__item-foot {
    display: flex;
    align-items: center;
    gap: 16px;
    padding-top: 10px;
    border-top: 1px dashed var(--nx-border);
    font-size: 13px;
    flex-wrap: wrap;
  }

  &__total {
    margin-left: auto;
    color: var(--nx-text-secondary);

    .nx-money {
      color: var(--nx-danger);
      font-size: 16px;
    }
  }

  &__cancel-reason {
    margin: 8px 0 0;
    color: var(--nx-text-muted);
    font-size: 12px;
  }

  &__actions {
    display: flex;
    gap: 8px;
    margin-top: 12px;
    flex-wrap: wrap;
  }
}
</style>
