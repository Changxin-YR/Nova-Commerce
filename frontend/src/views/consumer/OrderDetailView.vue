<script setup lang="ts">
/**
 * Order detail.
 *
 * Renders the order SNAPSHOT, not live catalog data: the line items frozen onto the order at
 * creation are what the buyer agreed to, and they must not change when the merchant edits the
 * product afterwards.
 *
 * FROZEN SHAPE (API_CONTRACT.md §6): items live at `order.items` (NOT `order.snapshot.items`),
 * money is FLAT on the order (`original_amount` / `promotion_discount_amount` /
 * `coupon_discount_amount` / `shipping_amount` / `payable_amount`), the state field is
 * `order_status`, and `receiver_name` / `receiver_phone` arrive ALREADY MASKED (§94) — this view
 * must never attempt to un-mask them.
 *
 * The refund ceiling is derived as `paid_amount - refunded_amount`, because the frozen payload has
 * no `refundable_amount` field; the backend independently rejects an over-refund
 * (REFUND_EXCEEDS_PAID_AMOUNT / _ITEM_AMOUNT).
 */
import { computed, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { afterSaleApi, orderApi, paymentApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { refundableAmount } from '@/domain/orders/availability'
import { normalizeError } from '@/api/error'
import { newTraceId } from '@/utils/trace'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'

const route = useRoute()
const router = useRouter()
const notifications = useNotificationStore()

const orderNo = computed(() => String(route.params.orderNo ?? ''))
const busy = ref(false)

const {
  data: order,
  status,
  error,
  execute,
} = useAsyncState(() => orderApi.detail(orderNo.value), { immediate: true })

const {
  data: shipments,
  execute: loadShipments,
} = useAsyncState(() => orderApi.shipments(orderNo.value), { immediate: true })

const canPay = computed(() => order.value?.order_status === 'PENDING_PAYMENT')
const canCancel = computed(
  () =>
    order.value?.order_status === 'PENDING_PAYMENT' || order.value?.order_status === 'PROCESSING',
)
const canConfirm = computed(() => order.value?.order_status === 'PROCESSING')
const canApplyAfterSale = computed(
  () =>
    (order.value?.order_status === 'PROCESSING' || order.value?.order_status === 'COMPLETED') &&
    order.value?.after_sale_status === 'NONE',
)
/**
 * The ceiling is DERIVED: `paid_amount - refunded_amount`. The frozen payload carries no
 * `refundable_amount`, and reading one would yield `undefined` — which compares false against
 * every bound, silently disabling refunds. The form uses this as a `max`, never as authorization;
 * the backend rejects an over-refund independently.
 */
const refundable = computed(() => (order.value ? refundableAmount(order.value) : 0))

async function pay(): Promise<void> {
  busy.value = true
  try {
    const payment = await paymentApi.byOrder(orderNo.value)
    await router.push({ name: 'mock-pay', params: { paymentId: payment.id } })
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('无法进入支付', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busy.value = false
  }
}

async function cancel(): Promise<void> {
  busy.value = true
  try {
    await orderApi.cancel(orderNo.value, { reason: '用户主动取消' })
    notifications.success('订单已取消')
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('取消失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busy.value = false
  }
}

async function confirm(): Promise<void> {
  busy.value = true
  try {
    await orderApi.confirmReceipt(orderNo.value)
    notifications.success('已确认收货')
    await execute()
    await loadShipments()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('确认收货失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busy.value = false
  }
}

/** Quick full-refund application, still capped by the server's refundable amount. */
async function applyRefundForRemaining(): Promise<void> {
  if (!order.value || refundable.value <= 0) return
  busy.value = true
  try {
    await afterSaleApi.apply({
      order_no: order.value.order_no,
      type: 'REFUND_ONLY',
      items: order.value.items.map((item) => ({
        order_item_id: item.id,
        quantity: item.quantity,
      })),
      requested_amount: refundable.value,
      reason: '用户申请退款',
      client_request_id: `as-${newTraceId()}`,
    })
    notifications.success('售后申请已提交', '等待商家处理')
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('提交售后失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="nx-container order-detail">
    <StateView :state="status" :error="error" @retry="execute()">
      <div v-if="order" class="order-detail__layout">
        <div class="order-detail__main">
          <section class="nx-card">
            <div class="nx-card__body">
              <header class="order-detail__head">
                <div>
                  <h1 class="nx-page-title">订单 {{ order.order_no }}</h1>
                  <p class="nx-muted">下单时间 {{ new Date(order.created_at).toLocaleString() }}</p>
                </div>
                <div class="order-detail__chips">
                  <StatusChip :status="order.order_status" kind="order" />
                  <StatusChip :status="order.payment_status" kind="payment" />
                  <StatusChip :status="order.fulfillment_status" kind="fulfillment" />
                  <StatusChip :status="order.after_sale_status" kind="aftersale" />
                </div>
              </header>

              <p
                v-if="order.expires_at && order.order_status === 'PENDING_PAYMENT'"
                class="order-detail__expire"
              >
                请在 {{ new Date(order.expires_at).toLocaleString() }} 前完成支付，超时后订单将自动关闭。
              </p>
            </div>
          </section>

          <section class="nx-card">
            <div class="nx-card__body">
              <h2 class="nx-section-title">商品快照</h2>
              <ul class="order-detail__items">
                <li v-for="item in order.items" :key="item.id">
                  <img v-if="item.image_url" :src="item.image_url" :alt="item.product_name" />
                  <span v-else class="order-detail__placeholder" aria-hidden="true">无图</span>
                  <span class="order-detail__item-info">
                    <strong>{{ item.product_name }}</strong>
                    <span class="nx-muted">{{ item.sku_name }}</span>
                    <span v-if="item.refunded_amount > 0" class="order-detail__refunded">
                      已退 <PriceText :amount="item.refunded_amount" size="sm" />
                    </span>
                  </span>
                  <span class="nx-muted">
                    <PriceText :amount="item.unit_price" size="sm" muted /> × {{ item.quantity }}
                  </span>
                  <PriceText :amount="item.payable_amount" size="md" />
                </li>
              </ul>
            </div>
          </section>

          <section class="nx-card">
            <div class="nx-card__body">
              <h2 class="nx-section-title">收货信息</h2>
              <!--
                §94: `receiver_name` / `receiver_phone` arrive ALREADY MASKED (张** / 138****5678).
                They are shown as received; un-masking is neither possible nor attempted.
              -->
              <dl class="order-detail__snapshot">
                <div><dt>收货人</dt><dd>{{ order.receiver_name }}</dd></div>
                <div><dt>联系电话</dt><dd>{{ order.receiver_phone }}</dd></div>
                <div v-if="order.full_address"><dt>收货地址</dt><dd>{{ order.full_address }}</dd></div>
                <div><dt>商品金额</dt><dd><PriceText :amount="order.original_amount" size="sm" muted /></dd></div>
                <div><dt>活动优惠</dt><dd>−<PriceText :amount="order.promotion_discount_amount" size="sm" muted /></dd></div>
                <div><dt>优惠券</dt><dd>−<PriceText :amount="order.coupon_discount_amount" size="sm" muted /></dd></div>
                <div><dt>运费</dt><dd><PriceText :amount="order.shipping_amount" size="sm" muted /></dd></div>
                <div class="order-detail__snapshot-total">
                  <dt>应付</dt>
                  <dd><PriceText :amount="order.payable_amount" size="lg" /></dd>
                </div>
                <div><dt>实付</dt><dd><PriceText :amount="order.paid_amount" size="sm" muted /></dd></div>
                <div><dt>已退款</dt><dd><PriceText :amount="order.refunded_amount" size="sm" muted /></dd></div>
                <div><dt>可退余额</dt><dd><PriceText :amount="refundable" size="sm" muted /></dd></div>
              </dl>
            </div>
          </section>

          <section v-if="(shipments ?? []).length" class="nx-card">
            <div class="nx-card__body">
              <h2 class="nx-section-title">物流信息</h2>
              <ul class="order-detail__shipments">
                <li v-for="shipment in shipments ?? []" :key="shipment.id">
                  <div class="order-detail__shipment-head">
                    <strong>{{ shipment.carrier }}</strong>
                    <code>{{ shipment.tracking_no }}</code>
                    <StatusChip :status="shipment.fulfillment_status" kind="fulfillment" />
                  </div>
                  <p class="nx-muted">
                    发货 {{ shipment.shipped_at ? new Date(shipment.shipped_at).toLocaleString() : '—' }}
                    <template v-if="shipment.delivered_at">
                      · 签收 {{ new Date(shipment.delivered_at).toLocaleString() }}
                    </template>
                  </p>
                </li>
              </ul>
            </div>
          </section>
        </div>

        <aside class="order-detail__aside nx-card">
          <div class="nx-card__body">
            <h2 class="nx-section-title">可执行操作</h2>
            <div class="order-detail__actions">
              <button v-if="canPay" type="button" class="nx-btn nx-btn--primary" :disabled="busy" @click="pay()">
                去支付
              </button>
              <button v-if="canCancel" type="button" class="nx-btn" :disabled="busy" @click="cancel()">
                取消订单
              </button>
              <button
                v-if="canConfirm"
                type="button"
                class="nx-btn nx-btn--primary"
                :disabled="busy"
                @click="confirm()"
              >
                确认收货
              </button>
              <button
                v-if="canApplyAfterSale && refundable > 0"
                type="button"
                class="nx-btn"
                :disabled="busy"
                @click="applyRefundForRemaining()"
              >
                申请退款 <PriceText :amount="refundable" size="sm" />
              </button>
              <RouterLink
                v-if="order.after_sale_status !== 'NONE'"
                :to="{ name: 'after-sales', query: { order_no: order.order_no } }"
                class="nx-btn nx-btn--ghost"
              >
                查看售后进度
              </RouterLink>
            </div>

            <!--
              §11 addendum: `cancel_reason` is defined on `OrderDetail` and is `null` unless the
              order is CANCELLED/CLOSED. It is recorded by the cancel workflow, so the client cannot
              infer it — and the cancel endpoint accepts the reason as its writer. This is the page
              that holds an `OrderDetail`, so this is where it belongs.
            -->
            <p
              v-if="order.cancel_reason && order.order_status === 'CANCELLED'"
              class="nx-muted order-detail__note"
            >
              取消原因：{{ order.cancel_reason }}
            </p>
            <p class="nx-muted order-detail__note">
              金额与状态均来自服务端，收货人信息由服务端脱敏。可退余额由服务端下发（服务端持有该口径）。
            </p>
          </div>
        </aside>
      </div>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.order-detail {
  &__layout {
    display: grid;
    grid-template-columns: 1fr 300px;
    gap: 18px;
    margin-top: 12px;
  }

  &__main {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  &__head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }

  &__chips {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
  }

  &__expire {
    margin: 12px 0 0;
    padding: 8px 12px;
    border-radius: var(--nx-radius-control);
    background: var(--nx-warning-soft);
    color: var(--nx-warning);
    font-size: 13px;
  }

  &__items {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin: 0;
    padding: 0;
    list-style: none;

    li {
      display: grid;
      grid-template-columns: 52px 1fr auto auto;
      align-items: center;
      gap: 12px;
      font-size: 13px;
    }

    img,
    .order-detail__placeholder {
      width: 52px;
      height: 52px;
      border-radius: 8px;
      background: var(--nx-surface-stage);
      object-fit: contain;
    }

    .order-detail__placeholder {
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--nx-text-muted);
      font-size: 10px;
    }
  }

  &__item-info {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  &__refunded {
    color: var(--nx-info);
    font-size: 12px;
  }

  &__snapshot {
    margin: 0;

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
    }
  }

  &__snapshot-total dd {
    color: var(--nx-danger);
    font-size: 16px;
  }

  &__shipments {
    margin: 0;
    padding: 0;
    list-style: none;

    li + li {
      margin-top: 12px;
    }
  }

  &__shipment-head {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 13px;
  }

  &__actions {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  &__note {
    margin: 14px 0 0;
    font-size: 12px;
    line-height: 1.6;
  }
}

@media (max-width: 960px) {
  .order-detail__layout {
    grid-template-columns: 1fr;
  }
}
</style>
