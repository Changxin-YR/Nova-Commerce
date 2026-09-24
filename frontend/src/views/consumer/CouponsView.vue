<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { marketingApi } from '@/api'
import { normalizeError } from '@/api/error'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import PriceText from '@/components/ui/PriceText.vue'
import StateView from '@/components/ui/StateView.vue'
import type { CouponTemplate } from '@/types/frozen-contract'

const notifications = useNotificationStore()
const page = ref(1)
const busyId = ref<number | null>(null)
const { data: offers, status, error, execute: loadOffers } = useAsyncState(
  () => marketingApi.availableCoupons({ page: page.value, page_size: 12 }),
  { immediate: true },
)
const { data: mine, status: mineStatus, execute: loadMine } = useAsyncState(
  () => marketingApi.myCoupons(),
  { immediate: true },
)
const available = computed(() => offers.value?.items ?? [])
const meta = computed(() => offers.value?.meta ?? null)

function discountPercent(coupon: CouponTemplate): string {
  const value = (10000 - (coupon.discount_bps ?? 10000)) / 100
  return `${Number(value.toFixed(2))}%`
}

function validity(coupon: CouponTemplate): string {
  if (coupon.validity_type === 'RELATIVE') return `领取后 ${coupon.valid_days} 天内有效`
  return `${coupon.valid_from?.slice(0, 10)} 至 ${coupon.valid_to?.slice(0, 10)}`
}

const statusLabels: Record<string, string> = {
  UNUSED: '可使用',
  LOCKED: '订单占用中',
  USED: '已使用',
  EXPIRED: '已过期',
}

async function claim(templateId: number): Promise<void> {
  busyId.value = templateId
  try {
    await marketingApi.claim(templateId)
    notifications.success('优惠券已领取', '结算时可选择使用')
    await Promise.all([loadOffers(), loadMine()])
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('领取失败', normalized.message, normalized.code, normalized.traceId)
    await loadOffers()
  } finally {
    busyId.value = null
  }
}

function changePage(delta: number): void {
  const next = page.value + delta
  if (next < 1 || (meta.value && next > meta.value.total_pages)) return
  page.value = next
  void loadOffers()
}
</script>

<template>
  <div class="nx-container coupons">
    <div class="coupons__head">
      <div>
        <p class="coupons__eyebrow">NOVA BENEFITS</p>
        <h1 class="nx-page-title">优惠券</h1>
        <p class="nx-muted">领取后可在结算页选择。实际优惠金额以订单预览为准。</p>
      </div>
      <RouterLink :to="{ name: 'cart' }" class="nx-btn">前往购物车</RouterLink>
    </div>

    <section class="coupons__section">
      <h2 class="nx-section-title">可领取</h2>
      <StateView :state="status" :error="error" @retry="loadOffers()">
        <div class="coupons__grid">
          <article v-for="coupon in available" :key="coupon.id" class="nx-card coupons__card">
            <div class="nx-card__body">
              <p class="coupons__merchant">商家 #{{ coupon.merchant_id }}</p>
              <h3>{{ coupon.name }}</h3>
              <p class="coupons__value">
                <PriceText
                  v-if="coupon.coupon_type === 'FIXED_AMOUNT'"
                  :amount="coupon.face_value_amount ?? 0"
                  size="xl"
                />
                <span v-else>立减 {{ discountPercent(coupon) }}</span>
              </p>
              <p class="nx-muted">
                <template v-if="coupon.threshold_amount > 0">
                  满 <PriceText :amount="coupon.threshold_amount" size="sm" /> 可用 ·
                </template>
                {{ validity(coupon) }}
              </p>
              <button
                type="button"
                class="nx-btn nx-btn--primary coupons__claim"
                :disabled="busyId === coupon.id"
                @click="claim(coupon.id)"
              >
                {{ busyId === coupon.id ? '领取中…' : '立即领取' }}
              </button>
            </div>
          </article>
        </div>
      </StateView>
      <div v-if="meta && meta.total_pages > 1" class="coupons__pager">
        <button class="nx-btn nx-btn--sm" :disabled="page === 1" @click="changePage(-1)">上一页</button>
        <span>第 {{ meta.page }} / {{ meta.total_pages }} 页</span>
        <button class="nx-btn nx-btn--sm" :disabled="page >= meta.total_pages" @click="changePage(1)">下一页</button>
      </div>
    </section>

    <section class="coupons__section">
      <h2 class="nx-section-title">我的优惠券</h2>
      <StateView :state="mineStatus" @retry="loadMine()">
        <div class="coupons__owned">
          <div v-for="coupon in mine ?? []" :key="coupon.id" class="nx-card coupons__owned-card">
            <div class="nx-card__body">
              <strong>优惠券 #{{ coupon.id }}</strong>
              <span>{{ statusLabels[coupon.status] ?? coupon.status }}</span>
              <small class="nx-muted">有效期至 {{ coupon.valid_to.slice(0, 10) }}</small>
            </div>
          </div>
        </div>
      </StateView>
    </section>
  </div>
</template>

<style scoped lang="scss">
.coupons {
  padding-top: 28px;
  padding-bottom: 48px;

  &__head {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 20px;
    padding-bottom: 24px;
    border-bottom: 1px solid var(--nx-border);
  }

  &__eyebrow {
    margin: 0 0 6px;
    color: var(--nx-primary);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
  }

  &__section {
    margin-top: 30px;
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 14px;
    margin-top: 14px;
  }

  &__card .nx-card__body {
    display: flex;
    flex-direction: column;
    min-height: 228px;
  }

  &__card h3 {
    margin: 8px 0 0;
    font-size: 17px;
  }

  &__merchant {
    margin: 0;
    color: var(--nx-text-muted);
    font-size: 11px;
  }

  &__value {
    margin: 14px 0 8px;
    color: var(--nx-primary);
    font-size: 25px;
    font-weight: 700;
  }

  &__claim {
    align-self: flex-start;
    margin-top: auto;
  }

  &__pager {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 18px;
    color: var(--nx-text-muted);
    font-size: 12px;
  }

  &__owned {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
    gap: 10px;
    margin-top: 14px;
  }

  &__owned-card .nx-card__body {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
}
</style>
