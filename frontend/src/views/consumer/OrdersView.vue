<script setup lang="ts">
/**
 * My orders — organised under status tabs, the way shoppers expect.
 *
 * IMPORTANT: the tab VALUES are our frozen `OrderStatus` values (§105) —
 * PENDING_PAYMENT / PROCESSING / COMPLETED / CANCELLED — and the LABELS are our own
 * Chinese copy. The tabs are derived from the state machine, NOT copied from another
 * retailer's vocabulary, so the UI can never drift away from what the backend accepts as
 * a filter. `PROCESSING` intentionally groups "to ship" and "shipped" because that
 * distinction lives in `fulfillment_status`, which is rendered per order.
 *
 * Prices render via `<PriceText>`; the payable amount shown is always the server's
 * `snapshot.payable_amount`.
 */
import { computed, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { orderApi, paymentApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'
import PriceText from '@/components/ui/PriceText.vue'
import { refundableAmount } from '@/domain/orders/availability'
import type { OrderSummary, OrderStatus } from '@/types/domain'

const router = useRouter()
const notifications = useNotificationStore()

/** Tab definitions keyed by our frozen status enum. */
const TABS: { value: '' | OrderStatus; label: string }[] = [
  { value: '', label: '全部订单' },
  { value: 'PENDING_PAYMENT', label: '待付款' },
  { value: 'PROCESSING', label: '待收货 / 处理中' },
  { value: 'COMPLETED', label: '已完成' },
  { value: 'CANCELLED', label: '已取消' },
]

const activeStatus = ref<'' | OrderStatus>('')

const { data: orderData, status, error, execute } = useAsyncState(
  () => orderApi.list(activeStatus.value ? { status: activeStatus.value } : {}),
  { immediate: true },
)

const orders = computed(() => orderData.value?.items ?? [])
const busyOrderNo = ref('')

function changeTab(value: '' | OrderStatus): void {
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

/** Looks the payment record up server-side; the client never fabricates a payment id. */
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

/**
 * The list payload is `OrderSummary` (API_CONTRACT.md §6), so the state field is `order_status`
 * and money is FLAT on the order. Reading `order.status` here would be `undefined`, every
 * comparison would be false, and the whole action bar would silently disappear.
 */
function canCancel(order: OrderSummary): boolean {
  return order.order_status === 'PENDING_PAYMENT' || order.order_status === 'PROCESSING'
}
function canConfirm(order: OrderSummary): boolean {
  return order.order_status === 'PROCESSING' && order.fulfillment_status !== 'UNFULFILLED'
}
function canApplyAfterSale(order: OrderSummary): boolean {
  return (
    (order.order_status === 'PROCESSING' || order.order_status === 'COMPLETED') &&
    order.after_sale_status === 'NONE'
  )
}

/** The frozen payload has no `refundable_amount`; it is `paid − refunded`, derived in one place. */
function refundableOf(order: OrderSummary): number {
  return refundableAmount(order)
}

/** Chinese weekday-free timestamp, compact for a dense list. */
function stamp(iso: string): string {
  const date = new Date(iso)
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
</script>

<template>
  <div class="orders">
    <div class="nx-container">
      <h1 class="orders__page-title">我的订单</h1>

      <!-- status tabs -->
      <nav class="nx-tabs orders__tabs" role="tablist" aria-label="订单状态">
        <button
          v-for="tab in TABS"
          :key="tab.value || 'all'"
          type="button"
          role="tab"
          class="nx-tab"
          :class="{ 'nx-tab--active': activeStatus === tab.value }"
          :aria-selected="activeStatus === tab.value"
          @click="changeTab(tab.value)"
        >
          {{ tab.label }}
        </button>
      </nav>

      <StateView
        :state="status"
        :error="error"
        :title="status === 'empty' ? '该状态下暂无订单' : undefined"
        :description="status === 'empty' ? '换个状态看看，或先去挑选商品。' : undefined"
        @retry="execute()"
      >
        <div class="orders__list">
          <article v-for="order in orders" :key="order.id" class="orders__item">
            <!-- order strip -->
            <header class="orders__strip">
              <span class="orders__time">{{ stamp(order.created_at) }}</span>
              <span class="orders__no">订单号：{{ order.order_no }}</span>
              <span class="orders__shop">Nova 官方旗舰店</span>
              <span class="orders__strip-right">
                <StatusChip :status="order.fulfillment_status" kind="fulfillment" dot />
                <StatusChip :status="order.order_status" kind="order" />
              </span>
            </header>

            <div class="orders__body">
              <!--
                ITEMS COLUMN — deliberately NOT a per-line list.
                The frozen list payload is `OrderSummary`, which carries NO `items[]`
                (API_CONTRACT.md §6: the list/detail split exists so a page of orders does not drag
                every line item across the wire). The old code rendered `order.snapshot.items`
                here, which on a frozen payload is simply `undefined`.
                Real line items would need one detail call PER ROW, so the list shows the
                order-level figures and routes to the detail page, which does have `items[]`.
              -->
              <div class="orders__items">
                <div class="orders__row">
                  <div class="orders__info">
                    <RouterLink
                      :to="{ name: 'order-detail', params: { orderNo: order.order_no } }"
                      class="orders__title"
                    >
                      订单 {{ order.order_no }}
                    </RouterLink>
                    <p class="nx-muted orders__specs">
                      收货人 {{ order.receiver_name }} · 商品金额
                      <PriceText :amount="order.original_amount" size="sm" muted />
                    </p>
                  </div>

                  <div class="orders__unit">
                    <PriceText :amount="order.payable_amount" size="sm" muted />
                  </div>
                </div>
              </div>

              <!-- side column: amounts + actions -->
              <div class="orders__side">
                <dl class="nx-rows orders__amounts">
                  <div>
                    <dt>应付</dt>
                    <dd><PriceText :amount="order.payable_amount" size="md" /></dd>
                  </div>
                  <div>
                    <dt>实付</dt>
                    <dd><PriceText :amount="order.paid_amount" size="sm" muted /></dd>
                  </div>
                  <div>
                    <dt>已退款</dt>
                    <dd><PriceText :amount="order.refunded_amount" size="sm" muted /></dd>
                  </div>
                  <div v-if="refundableOf(order) > 0">
                    <dt>可退余额</dt>
                    <dd><PriceText :amount="refundableOf(order)" size="sm" muted /></dd>
                  </div>
                </dl>

                <p class="orders__pay-status">
                  <StatusChip :status="order.payment_status" kind="payment" />
                  <StatusChip v-if="order.after_sale_status !== 'NONE'" :status="order.after_sale_status" kind="aftersale" />
                </p>

                <div class="orders__actions">
                  <button
                    v-if="order.order_status === 'PENDING_PAYMENT'"
                    type="button"
                    class="nx-btn nx-btn--primary nx-btn--sm"
                    :disabled="busyOrderNo === order.order_no"
                    @click="goPay(order.order_no)"
                  >
                    立即付款
                  </button>
                  <button
                    v-if="canConfirm(order)"
                    type="button"
                    class="nx-btn nx-btn--sm"
                    :disabled="busyOrderNo === order.order_no"
                    @click="confirmReceipt(order.order_no)"
                  >
                    确认收货
                  </button>
                  <button
                    v-if="canCancel(order)"
                    type="button"
                    class="nx-btn nx-btn--sm"
                    :disabled="busyOrderNo === order.order_no"
                    @click="cancelOrder(order.order_no)"
                  >
                    取消订单
                  </button>
                  <RouterLink
                    :to="{ name: 'order-detail', params: { orderNo: order.order_no } }"
                    class="nx-btn nx-btn--sm"
                  >
                    订单详情
                  </RouterLink>
                  <RouterLink
                    v-if="canApplyAfterSale(order)"
                    :to="{ name: 'after-sales', query: { order_no: order.order_no } }"
                    class="nx-btn nx-btn--sm"
                  >
                    申请售后
                  </RouterLink>
                </div>

                <!--
                  NOTE: a "取消原因" line used to render here from `order.cancel_reason`.
                  `API_CONTRACT.md` §6 does NOT define that field on `OrderSummary`/`OrderDetail`,
                  so on a frozen payload it is `undefined` and the line could never appear. The
                  field is removed rather than kept as decoration over a value the server never
                  sends. If the backend adds a cancel reason later it should be added to the
                  contract first and this block restored from it (see the migration report).
                -->
              </div>
            </div>
          </article>
        </div>
      </StateView>
    </div>
  </div>
</template>

<style scoped lang="scss">
.orders {
  &__page-title {
    margin: 0 0 10px;
    font-size: 18px;
    font-weight: 700;
  }

  &__tabs {
    margin-bottom: 10px;
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
    border-bottom: none;
  }

  &__list {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin-top: 10px;
  }

  &__item {
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
  }

  /* -- order strip -------------------------------------------------------- */
  &__strip {
    display: flex;
    align-items: center;
    gap: 16px;
    height: 34px;
    padding: 0 12px;
    background: var(--nx-surface-sunken);
    border-bottom: 1px solid var(--nx-border);
    font-size: 12px;
    color: var(--nx-text-secondary);
  }

  &__time {
    flex: 0 0 auto;
  }

  &__no {
    font-variant-numeric: tabular-nums;
  }

  &__shop {
    color: var(--nx-text);
  }

  &__strip-right {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-left: auto;
  }

  /* -- body: items table + side column ------------------------------------ */
  &__body {
    display: grid;
    grid-template-columns: 1fr 220px;
  }

  &__items {
    min-width: 0;
  }

  &__row {
    display: grid;
    grid-template-columns: 80px 1fr 110px;
    gap: 10px;
    align-items: center;
    padding: 12px;
    border-bottom: 1px solid var(--nx-border);

    &:last-child {
      border-bottom: none;
    }
  }

  &__thumb-link {
    display: block;
  }

  &__thumb {
    width: 80px;
    height: 80px;
    border: 1px solid var(--nx-border);
    background: var(--nx-surface-stage);
    object-fit: contain;

    &--empty {
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--nx-text-muted);
      font-size: 12px;
    }
  }

  &__info {
    min-width: 0;
  }

  &__title {
    display: -webkit-box;
    overflow: hidden;
    color: var(--nx-text);
    font-size: 12px;
    line-height: 18px;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;

    &:hover {
      color: var(--nx-brand);
    }
  }

  &__specs {
    margin: 4px 0 0;
    font-size: 12px;
  }

  &__refunded {
    display: flex;
    align-items: center;
    gap: 4px;
    margin: 4px 0 0;
    color: var(--nx-info);
    font-size: 12px;
  }

  &__unit {
    text-align: right;
  }

  /* -- side column -------------------------------------------------------- */
  &__side {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 12px;
    border-left: 1px solid var(--nx-border);
    background: var(--nx-surface);
  }

  &__amounts {
    > div {
      padding: 3px 0;
    }
  }

  &__pay-status {
    display: flex;
    gap: 6px;
    margin: 0;
  }

  &__actions {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  &__cancel {
    margin: 0;
    font-size: 12px;
    line-height: 1.5;
  }
}

@media (max-width: 1000px) {
  .orders__body {
    grid-template-columns: 1fr;
  }

  .orders__side {
    border-left: none;
    border-top: 1px solid var(--nx-border);
  }
}
</style>
