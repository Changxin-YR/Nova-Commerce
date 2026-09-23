<script setup lang="ts">
/**
 * Checkout — the highest-stakes page, so every number on it is SERVER-COMPUTED:
 * `items_amount`, `discount_amount`, `shipping_amount`, `payable_amount` all come from
 * `POST /orders/preview`. This page never adds, subtracts or rounds money; `<PriceText>`
 * only formats. The amount panel lists each line separately (商品金额 / 促销优惠 / 优惠券 /
 * 运费 / 应付) because a shopper must be able to audit the total.
 *
 * Order creation sends BOTH an `Idempotency-Key` header and a `client_request_id`, so a
 * double-clicked submit creates ONE order and a network retry is safe.
 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { addressApi, orderApi, paymentApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useCartStore } from '@/stores/cart'
import { useNotificationStore } from '@/stores/notification'
import { normalizeError } from '@/api/error'
import { newTraceId } from '@/utils/trace'
import StateView from '@/components/ui/StateView.vue'
import PriceText from '@/components/ui/PriceText.vue'

const router = useRouter()
const cart = useCartStore()
const notifications = useNotificationStore()

const selectedAddressId = ref('')
const couponCode = ref('')
const remark = ref('')
/** Our own payment-channel labels; the mock channel drives the server callback path. */
const channel = ref<'MOCK' | 'ALIPAY' | 'WECHAT'>('MOCK')

const CHANNELS = [
  { value: 'MOCK' as const, label: '模拟支付（演示用）', hint: '走服务端回调校验，可验证幂等' },
  { value: 'ALIPAY' as const, label: '在线支付 A', hint: '未接入真实通道' },
  { value: 'WECHAT' as const, label: '在线支付 B', hint: '未接入真实通道' },
]

const {
  data: addresses,
  status: addressStatus,
  error: addressError,
  execute: loadAddresses,
} = useAsyncState(() => addressApi.list(), { immediate: true })

const addressList = computed(() => addresses.value ?? [])

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
/** One idempotency key per checkout attempt; regenerated only after success. */
const clientRequestId = ref(newTraceId())
const idempotencyKey = computed(() => `order-${clientRequestId.value}`)

onMounted(async () => {
  if (cart.isEmpty) await cart.load().catch(() => undefined)
  await loadAddresses()
  const preferred = addressList.value.find((a) => a.is_default) ?? addressList.value[0]
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
      remark: remark.value || undefined,
    })

    const payment = await paymentApi.create({
      order_no: order.order_no,
      channel: channel.value,
      client_request_id: `pay-${clientRequestId.value}`,
    })

    clientRequestId.value = newTraceId()
    notifications.success('订单已创建', `订单号 ${order.order_no}`)
    await router.push({ name: 'mock-pay', params: { paymentId: payment.id } })
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('下单失败', normalized.message, normalized.code, normalized.traceId)
    // A price change or a stock race is not a UI bug: refresh the preview so the shopper
    // sees the authoritative numbers instead of a stale total.
    await loadPreview()
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="checkout">
    <div class="nx-container">
      <h1 class="checkout__page-title">确认订单</h1>

      <StateView :state="previewStatus" :error="previewError" @retry="loadPreview()">
        <!-- step 1: address ------------------------------------------------- -->
        <section class="nx-block checkout__step">
          <div class="nx-block__head">
            <h2 class="nx-block__title">收货地址</h2>
            <button type="button" class="nx-btn nx-btn--sm" @click="router.push({ name: 'addresses' })">
              管理地址
            </button>
          </div>
          <div class="nx-block__body">
            <StateView :state="addressStatus" :error="addressError" compact @retry="loadAddresses()">
              <p v-if="addressList.length === 0" class="checkout__empty">
                还没有收货地址，请先
                <RouterLink :to="{ name: 'addresses' }">添加一个地址</RouterLink>
                。
              </p>

              <div v-else class="checkout__addresses">
                <label
                  v-for="address in addressList"
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
                    <span class="checkout__address-line">
                      <b>{{ address.receiver_name }}</b>
                      <span>{{ address.receiver_phone }}</span>
                      <span v-if="address.is_default" class="nx-badge nx-badge--self">默认</span>
                    </span>
                    <span class="nx-muted">
                      {{ address.province }}{{ address.city }}{{ address.district }}{{ address.detail }}
                    </span>
                  </span>
                </label>
              </div>
            </StateView>
          </div>
        </section>

        <!-- step 2: payment method ------------------------------------------ -->
        <section class="nx-block checkout__step">
          <div class="nx-block__head">
            <h2 class="nx-block__title">支付方式</h2>
          </div>
          <div class="nx-block__body">
            <div class="checkout__channels">
              <label
                v-for="item in CHANNELS"
                :key="item.value"
                class="checkout__channel"
                :class="{ 'checkout__channel--active': channel === item.value }"
              >
                <input v-model="channel" type="radio" name="channel" :value="item.value" />
                <span>
                  <b>{{ item.label }}</b>
                  <em class="nx-muted">{{ item.hint }}</em>
                </span>
              </label>
            </div>
          </div>
        </section>

        <!-- step 3: item table --------------------------------------------- -->
        <section class="nx-block checkout__step">
          <div class="nx-block__head">
            <h2 class="nx-block__title">商品清单</h2>
            <span class="nx-muted">共 {{ preview?.items?.length ?? 0 }} 种商品</span>
          </div>

          <div class="checkout__table">
            <div class="checkout__thead">
              <span class="checkout__th checkout__th--product">商品信息</span>
              <span class="checkout__th checkout__th--num">单价</span>
              <span class="checkout__th checkout__th--num">数量</span>
              <span class="checkout__th checkout__th--num">小计</span>
            </div>

            <div v-for="(item, index) in preview?.items ?? []" :key="`${item.sku_id}-${index}`" class="checkout__trow">
              <div class="checkout__td checkout__td--product">
                <img v-if="item.cover_url" :src="item.cover_url" :alt="item.product_title" class="checkout__thumb" />
                <span v-else class="checkout__thumb checkout__thumb--empty" aria-hidden="true">暂无图片</span>
                <span class="checkout__product-info">
                  <b>{{ item.product_title }}</b>
                  <em class="nx-muted">{{ Object.values(item.sku_specs).join(' / ') }}</em>
                </span>
              </div>
              <span class="checkout__td checkout__td--num">
                <PriceText :amount="item.unit_price_amount" size="sm" muted />
              </span>
              <span class="checkout__td checkout__td--num">× {{ item.quantity }}</span>
              <span class="checkout__td checkout__td--num">
                <PriceText :amount="item.subtotal_amount" size="sm" />
              </span>
            </div>
          </div>

          <div class="nx-block__body checkout__extras">
            <label class="checkout__extra">
              <span class="checkout__extra-label">优惠券</span>
              <input v-model="couponCode" class="nx-input checkout__coupon" placeholder="输入优惠券码后重新计价" />
              <button type="button" class="nx-btn nx-btn--sm" @click="loadPreview()">使用</button>
            </label>

            <p v-if="preview?.coupon" class="checkout__coupon-ok">
              已使用「{{ preview.coupon.name }}」，抵扣
              <PriceText :amount="preview.coupon.discount_amount" size="sm" />
            </p>

            <label class="checkout__extra">
              <span class="checkout__extra-label">订单备注</span>
              <input v-model="remark" class="nx-input checkout__remark" maxlength="120" placeholder="选填，如送货时间要求" />
            </label>

            <p v-for="warning in preview?.warnings ?? []" :key="warning" class="checkout__warning">
              {{ warning }}
            </p>
          </div>
        </section>

        <!-- step 4: sticky amount panel ------------------------------------ -->
        <div class="checkout__settle">
          <div class="checkout__settle-inner">
            <div class="checkout__settle-left">
              <p class="nx-muted">
                提交时携带幂等键 <code class="checkout__idem">{{ idempotencyKey }}</code>
                ，重复点击不会重复下单。
              </p>
            </div>

            <dl class="nx-rows checkout__amounts">
              <div>
                <dt>商品金额</dt>
                <dd><PriceText :amount="preview?.items_amount ?? 0" size="sm" muted /></dd>
              </div>
              <div>
                <dt>促销优惠</dt>
                <dd>
                  −<PriceText :amount="preview?.discount_amount ?? 0" size="sm" muted />
                </dd>
              </div>
              <div>
                <dt>运费</dt>
                <dd><PriceText :amount="preview?.shipping_amount ?? 0" size="sm" muted /></dd>
              </div>
              <div class="nx-rows--total">
                <dt>应付总额</dt>
                <dd><PriceText :amount="preview?.payable_amount ?? 0" size="xl" /></dd>
              </div>
            </dl>

            <button
              type="button"
              class="nx-btn nx-btn--primary nx-btn--lg checkout__submit"
              :disabled="submitting || !selectedAddressId || (preview?.items?.length ?? 0) === 0"
              @click="createOrder()"
            >
              {{ submitting ? '提交中…' : '提交订单' }}
            </button>
          </div>
        </div>
      </StateView>
    </div>
  </div>
</template>

<style scoped lang="scss">
.checkout {
  &__page-title {
    margin: 0 0 10px;
    font-size: 18px;
    font-weight: 700;
  }

  &__step {
    margin-bottom: 10px;
  }

  /* -- address ------------------------------------------------------------ */
  &__addresses {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 8px;
  }

  &__address {
    display: flex;
    gap: 8px;
    padding: 8px 10px;
    border: 1px solid var(--nx-border);
    cursor: pointer;

    &:hover {
      border-color: var(--nx-border-strong);
    }

    &--active {
      border-color: var(--nx-brand);
      background: var(--nx-brand-soft);
    }
  }

  &__address-body {
    display: flex;
    flex-direction: column;
    gap: 3px;
    font-size: 12px;
  }

  &__address-line {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  &__empty {
    margin: 0;
    font-size: 12px;
    color: var(--nx-text-secondary);
  }

  /* -- payment channels --------------------------------------------------- */
  &__channels {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }

  &__channel {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    min-width: 220px;
    padding: 8px 10px;
    border: 1px solid var(--nx-border);
    cursor: pointer;

    &--active {
      border-color: var(--nx-brand);
      background: var(--nx-brand-soft);
    }

    span {
      display: flex;
      flex-direction: column;
      gap: 2px;
      font-size: 12px;
    }

    em {
      font-style: normal;
      font-size: 12px;
    }
  }

  /* -- item table --------------------------------------------------------- */
  &__table {
    border-top: 1px solid var(--nx-border);
  }

  &__thead,
  &__trow {
    display: grid;
    grid-template-columns: 1fr 110px 90px 120px;
    gap: 10px;
    padding: 0 12px;
    align-items: center;
  }

  &__thead {
    height: 32px;
    background: var(--nx-surface-sunken);
    border-bottom: 1px solid var(--nx-border);
    font-size: 12px;
    color: var(--nx-text-secondary);
  }

  &__th--num,
  &__td--num {
    text-align: right;
  }

  &__trow {
    padding-top: 10px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--nx-border);
    font-size: 12px;
  }

  &__td--product {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
  }

  &__thumb {
    width: 60px;
    height: 60px;
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

  &__product-info {
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;

    b {
      font-size: 12px;
      font-weight: 400;
      line-height: 18px;
    }

    em {
      font-style: normal;
    }
  }

  /* -- extras ------------------------------------------------------------- */
  &__extras {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  &__extra {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
  }

  &__extra-label {
    flex: 0 0 60px;
    color: var(--nx-text-secondary);
  }

  &__coupon {
    width: 220px;
  }

  &__remark {
    flex: 1;
    max-width: 420px;
  }

  &__coupon-ok {
    margin: 0;
    display: flex;
    align-items: center;
    gap: 4px;
    color: var(--nx-brand);
    font-size: 12px;
  }

  &__warning {
    margin: 0;
    color: var(--nx-warning);
    font-size: 12px;
  }

  /* -- sticky settlement bar --------------------------------------------- */
  &__settle {
    position: sticky;
    bottom: 0;
    z-index: 20;
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
    box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.06);
  }

  &__settle-inner {
    display: flex;
    align-items: center;
    gap: 20px;
    padding: 10px 12px;
  }

  &__settle-left {
    flex: 1;
    min-width: 0;

    p {
      margin: 0;
      font-size: 12px;
      line-height: 1.6;
    }
  }

  &__idem {
    word-break: break-all;
  }

  &__amounts {
    flex: 0 0 260px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    text-align: right;

    dd {
      text-align: right;
    }
  }

  &__submit {
    flex: 0 0 180px;
  }
}

@media (max-width: 1000px) {
  .checkout__settle-inner {
    flex-direction: column;
    align-items: stretch;
  }

  .checkout__amounts,
  .checkout__submit {
    flex: 1 1 auto;
  }
}
</style>
