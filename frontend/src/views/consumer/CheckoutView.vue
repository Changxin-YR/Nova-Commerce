<script setup lang="ts">
/**
 * Checkout: address + coupon + SERVER-COMPUTED totals.
 *
 * Two rules this page exists to demonstrate:
 *  1. Every number shown (`items_amount`, `discount_amount`, `shipping_amount`,
 *     `payable_amount`) comes from `POST /orders/preview`. The page never adds,
 *     subtracts or rounds money itself.
 *  2. Creating the order sends BOTH an `Idempotency-Key` header and a
 *     `client_request_id`. A double-clicked button therefore creates ONE order; the
 *     client retries nothing on its own, and a network retry is safe.
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { addressApi, orderApi, paymentApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useCartStore } from '@/stores/cart'
import { useNotificationStore } from '@/stores/notification'
import { formatMoney } from '@/utils/money'
import { normalizeError } from '@/api/error'
import { newTraceId } from '@/utils/trace'
import StateView from '@/components/ui/StateView.vue'

const router = useRouter()
const cart = useCartStore()
const notifications = useNotificationStore()

const selectedAddressId = ref('')
const couponCode = ref('')

const {
  data: addresses,
  status: addressStatus,
  error: addressError,
  execute: loadAddresses,
} = useAsyncState(() => addressApi.list(), { immediate: true })

const addressesList = computed(() => addresses.value ?? [])

const {
  data: preview,
  status: previewStatus,
  error: previewError,
  execute: loadPreview,
} = useAsyncState(
  () =>
    orderApi.preview({
      source: 'cart',
      address_id: selectedAddressId.value || undefined,
      coupon_code: couponCode.value || undefined,
    }),
  { immediate: false },
)

const submitting = ref(false)
// One idempotency key per checkout attempt. It is regenerated only after a
// successful order, so a retry of the SAME attempt reuses it.
const clientRequestId = ref(newTraceId())
const idempotencyKey = computed(() => `order-${clientRequestId.value}`)

onMounted(async () => {
  if (cart.isEmpty) await cart.load().catch(() => undefined)
  await loadAddresses()
  const preferred = addressesList.value.find((address) => address.is_default) ?? addressesList.value[0]
  if (preferred) selectedAddressId.value = preferred.id
  await loadPreview()
})

async function createOrder(): Promise<void> {
  if (!selectedAddressId.value) {
    notifications.warning('请先选择收货地址')
    return
  }
  submitting.value = true
  try {
    const order = await orderApi.create({
      address_id: selectedAddressId.value,
      client_request_id: idempotencyKey.value,
      source: 'cart',
      coupon_code: couponCode.value || undefined,
    })

    // The payment record is created server-side; the client only navigates to the
    // mock pay page and then RE-READS server state.
    const payment = await paymentApi.create({
      order_no: order.order_no,
      channel: 'MOCK',
      client_request_id: `pay-${clientRequestId.value}`,
    })

    // A new attempt must use a new key.
    clientRequestId.value = newTraceId()
    notifications.success('订单已创建', `订单号 ${order.order_no}`)
    await router.push({ name: 'mock-pay', params: { paymentId: payment.id } })
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('下单失败', normalized.message, normalized.code, normalized.traceId)
    // A changed price or a stock race is not a bug in the UI: refresh the preview
    // so the user sees the authoritative numbers instead of a stale total.
    await loadPreview()
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="nx-container checkout">
    <h1 class="nx-page-title">确认订单</h1>

    <StateView
      :state="previewStatus"
      :error="previewError"
      @retry="loadPreview()"
    >
      <div class="checkout__layout">
        <div class="checkout__main">
          <section class="nx-card checkout__section">
            <div class="nx-card__body">
              <h2 class="nx-section-title">收货地址</h2>

              <StateView
                :state="addressStatus"
                :error="addressError"
                compact
                @retry="loadAddresses()"
              >
                <div v-if="addressesList.length === 0" class="checkout__empty-address">
                  <p class="nx-muted">还没有收货地址，请先添加一个。</p>
                  <button type="button" class="nx-btn" @click="router.push({ name: 'addresses' })">
                    去添加地址
                  </button>
                </div>

                <div v-else class="checkout__addresses">
                  <label
                    v-for="address in addressesList"
                    :key="address.id"
                    class="checkout__address"
                    :class="{ 'checkout__address--active': address.id === selectedAddressId }"
                  >
                    <input
                      v-model="selectedAddressId"
                      type="radio"
                      name="address"
                      :value="address.id"
                      @change="loadPreview()"
                    />
                    <span class="checkout__address-body">
                      <strong>{{ address.receiver_name }}</strong>
                      <span class="nx-muted">{{ address.receiver_phone }}</span>
                      <span class="nx-muted">
                        {{ address.province }}{{ address.city }}{{ address.district }}{{ address.detail }}
                      </span>
                      <span v-if="address.is_default" class="checkout__tag">默认</span>
                    </span>
                  </label>
                </div>
              </StateView>
            </div>
          </section>

          <section class="nx-card checkout__section">
            <div class="nx-card__body">
              <h2 class="nx-section-title">商品清单</h2>
              <ul class="checkout__items">
                <li v-for="(item, index) in preview?.items ?? []" :key="`${item.sku_id}-${index}`">
                  <img v-if="item.cover_url" :src="item.cover_url" :alt="item.product_title" />
                  <span v-else class="checkout__item-placeholder" aria-hidden="true">无图</span>
                  <span class="checkout__item-info">
                    <strong>{{ item.product_title }}</strong>
                    <span class="nx-muted">{{ Object.values(item.sku_specs).join(' / ') }}</span>
                  </span>
                  <span class="nx-muted">× {{ item.quantity }}</span>
                  <span class="nx-money">{{ formatMoney(item.subtotal_amount) }}</span>
                </li>
              </ul>
            </div>
          </section>

          <section class="nx-card checkout__section">
            <div class="nx-card__body">
              <h2 class="nx-section-title">优惠券</h2>
              <div class="checkout__coupon">
                <input
                  v-model="couponCode"
                  type="text"
                  placeholder="输入优惠券码后重新预览"
                  class="checkout__input"
                />
                <button type="button" class="nx-btn" @click="loadPreview()">应用</button>
              </div>
              <p v-if="preview?.coupon" class="nx-muted">
                已应用：{{ preview.coupon.name }}，抵扣 {{ formatMoney(preview.coupon.discount_amount) }}
              </p>
              <p v-for="warning in preview?.warnings ?? []" :key="warning" class="checkout__warning">
                {{ warning }}
              </p>
            </div>
          </section>
        </div>

        <aside class="checkout__summary nx-card">
          <div class="nx-card__body">
            <h2 class="nx-section-title">金额明细</h2>
            <dl class="checkout__totals">
              <div>
                <dt>商品金额</dt>
                <dd class="nx-money">{{ formatMoney(preview?.items_amount ?? 0) }}</dd>
              </div>
              <div>
                <dt>优惠</dt>
                <dd class="nx-money">−{{ formatMoney(preview?.discount_amount ?? 0) }}</dd>
              </div>
              <div>
                <dt>运费</dt>
                <dd class="nx-money">{{ formatMoney(preview?.shipping_amount ?? 0) }}</dd>
              </div>
              <div class="checkout__totals-total">
                <dt>应付</dt>
                <dd class="nx-money">{{ formatMoney(preview?.payable_amount ?? 0) }}</dd>
              </div>
            </dl>

            <button
              type="button"
              class="nx-btn nx-btn--primary checkout__submit"
              :disabled="submitting || !selectedAddressId || (preview?.items?.length ?? 0) === 0"
              @click="createOrder()"
            >
              {{ submitting ? '提交中…' : '提交订单' }}
            </button>
            <p class="nx-muted checkout__hint">
              提交时携带幂等键 <code>{{ idempotencyKey }}</code>，重复点击不会重复下单。
            </p>
          </div>
        </aside>
      </div>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.checkout {
  &__layout {
    display: grid;
    grid-template-columns: 1fr 320px;
    gap: 20px;
    margin-top: 16px;
  }

  &__main {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  &__addresses {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  &__address {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    padding: 10px 12px;
    border: 1px solid var(--nx-border);
    border-radius: var(--nx-radius-stage);
    cursor: pointer;

    &--active {
      border-color: var(--nx-primary);
      background: var(--nx-primary-soft);
    }
  }

  &__address-body {
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: 13px;
  }

  &__tag {
    align-self: flex-start;
    margin-top: 2px;
    padding: 1px 6px;
    border-radius: var(--nx-radius-pill);
    background: var(--nx-primary);
    color: #fff;
    font-size: 10px;
  }

  &__empty-address {
    display: flex;
    align-items: center;
    gap: 12px;
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
      grid-template-columns: 48px 1fr auto auto;
      align-items: center;
      gap: 12px;
      font-size: 13px;
    }

    img,
    .checkout__item-placeholder {
      width: 48px;
      height: 48px;
      border-radius: 8px;
      background: var(--nx-surface-stage);
      object-fit: contain;
    }

    .checkout__item-placeholder {
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

  &__coupon {
    display: flex;
    gap: 8px;
  }

  &__input {
    flex: 1;
    height: 34px;
    padding: 0 12px;
    border: 1px solid var(--nx-border-strong);
    border-radius: var(--nx-radius-control);
    background: var(--nx-surface);
    color: var(--nx-text);
    font-family: inherit;
    font-size: 13px;
  }

  &__warning {
    margin: 6px 0 0;
    color: var(--nx-warning);
    font-size: 12px;
  }

  &__totals {
    margin: 0 0 18px;

    > div {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 7px 0;
      font-size: 13px;
    }

    dt {
      color: var(--nx-text-muted);
    }

    dd {
      margin: 0;
    }
  }

  &__totals-total {
    margin-top: 6px;
    padding-top: 12px !important;
    border-top: 1px solid var(--nx-border);
    font-size: 15px !important;

    dd {
      color: var(--nx-danger);
      font-size: 18px;
    }
  }

  &__submit {
    width: 100%;
    min-height: 42px;
  }

  &__hint {
    margin: 8px 0 0;
    font-size: 11px;
    word-break: break-all;
  }
}

@media (max-width: 960px) {
  .checkout__layout {
    grid-template-columns: 1fr;
  }
}
</style>
