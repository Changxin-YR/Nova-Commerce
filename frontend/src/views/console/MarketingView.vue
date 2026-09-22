<script setup lang="ts">
/**
 * Marketing: coupons and promotions.
 *
 * The coupon status chips make the LOCKED lifecycle legible: a coupon committed to
 * another order shows as unavailable rather than being offered again, and the server
 * enforces that with `COUPON_ALREADY_LOCKED` / `COUPON_ALREADY_USED`.
 */
import { computed, reactive, ref } from 'vue'
import { marketingAdminApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { formatMoney, fromMajorString, toMajorString } from '@/utils/money'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'

const notifications = useNotificationStore()

const tab = ref<'coupons' | 'promotions'>('coupons')

const {
  data: couponData,
  status: couponStatus,
  error: couponError,
  execute: loadCoupons,
} = useAsyncState(() => marketingAdminApi.coupons({ page: 1, page_size: 20 }), { immediate: true })

const {
  data: promotionData,
  status: promotionStatus,
  error: promotionError,
  execute: loadPromotions,
} = useAsyncState(() => marketingAdminApi.promotions({ page: 1, page_size: 20 }), { immediate: false })

const coupons = computed(() => couponData.value?.items ?? [])
const promotions = computed(() => promotionData.value?.items ?? [])

const formOpen = ref(false)
const busy = ref(false)
const form = reactive({
  code: '',
  name: '',
  discountYuan: '',
  thresholdYuan: '',
  validFrom: '',
  validTo: '',
})

async function createCoupon(): Promise<void> {
  const discount = fromMajorString(form.discountYuan)
  const threshold = fromMajorString(form.thresholdYuan)
  if (!form.code.trim() || !form.name.trim() || discount === null || threshold === null) {
    notifications.warning('请完整填写券码、名称与金额')
    return
  }
  busy.value = true
  try {
    await marketingAdminApi.createCoupon({
      code: form.code.trim(),
      name: form.name.trim(),
      discount_amount: discount,
      threshold_amount: threshold,
      valid_from: form.validFrom || new Date().toISOString(),
      valid_to: form.validTo || new Date(Date.now() + 30 * 86400_000).toISOString(),
    })
    notifications.success('优惠券已创建')
    formOpen.value = false
    form.code = ''
    form.name = ''
    form.discountYuan = ''
    form.thresholdYuan = ''
    await loadCoupons()
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('创建失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busy.value = false
  }
}

function switchTab(next: 'coupons' | 'promotions'): void {
  tab.value = next
  if (next === 'promotions') void loadPromotions()
}

/** Template hint: shows the minor-unit convention instead of a bare example. */
const sampleDiscount = toMajorString(1000)
</script>

<template>
  <div class="marketing">
    <div class="marketing__head">
      <h2 class="nx-section-title">营销中心</h2>
      <div class="nx-pills">
        <button
          type="button"
          class="nx-pill"
          :class="{ 'nx-pill--active': tab === 'coupons' }"
          @click="switchTab('coupons')"
        >
          优惠券
        </button>
        <button
          type="button"
          class="nx-pill"
          :class="{ 'nx-pill--active': tab === 'promotions' }"
          @click="switchTab('promotions')"
        >
          促销活动
        </button>
      </div>
    </div>

    <template v-if="tab === 'coupons'">
      <div class="marketing__toolbar">
        <button type="button" class="nx-btn nx-btn--primary" @click="formOpen = !formOpen">
          {{ formOpen ? '取消' : '新建优惠券' }}
        </button>
        <span class="nx-muted">示例：满 {{ sampleDiscount }} 元可用</span>
      </div>

      <form v-if="formOpen" class="nx-card marketing__form" @submit.prevent="createCoupon">
        <div class="nx-card__body">
          <div class="marketing__grid">
            <label><span>券码</span><input v-model="form.code" maxlength="32" /></label>
            <label><span>名称</span><input v-model="form.name" maxlength="60" /></label>
            <label><span>面额（元）</span><input v-model="form.discountYuan" inputmode="decimal" /></label>
            <label><span>门槛（元）</span><input v-model="form.thresholdYuan" inputmode="decimal" /></label>
            <label><span>生效时间</span><input v-model="form.validFrom" type="datetime-local" /></label>
            <label><span>失效时间</span><input v-model="form.validTo" type="datetime-local" /></label>
          </div>
          <button type="submit" class="nx-btn nx-btn--primary" :disabled="busy">创建</button>
        </div>
      </form>

      <StateView
        :state="couponStatus"
        :error="couponError"
        :title="couponStatus === 'empty' ? '还没有优惠券' : undefined"
        @retry="loadCoupons()"
      >
        <table class="marketing__table">
          <thead>
            <tr>
              <th>券码</th>
              <th>名称</th>
              <th>状态</th>
              <th>面额</th>
              <th>门槛</th>
              <th>有效期</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="coupon in coupons" :key="coupon.id">
              <td><code>{{ coupon.code }}</code></td>
              <td>{{ coupon.name }}</td>
              <td><StatusChip :status="coupon.status" /></td>
              <td class="nx-money">{{ formatMoney(coupon.discount_amount) }}</td>
              <td class="nx-money">{{ formatMoney(coupon.threshold_amount) }}</td>
              <td class="marketing__dates">
                {{ new Date(coupon.valid_from).toLocaleDateString() }} –
                {{ new Date(coupon.valid_to).toLocaleDateString() }}
              </td>
            </tr>
          </tbody>
        </table>
      </StateView>
    </template>

    <StateView
      v-else
      :state="promotionStatus"
      :error="promotionError"
      :title="promotionStatus === 'empty' ? '还没有促销活动' : undefined"
      @retry="loadPromotions()"
    >
      <table class="marketing__table">
        <thead>
          <tr>
            <th>名称</th>
            <th>类型</th>
            <th>状态</th>
            <th>优先级</th>
            <th>起止时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="promotion in promotions" :key="promotion.id">
            <td>{{ promotion.name }}</td>
            <td>{{ promotion.type }}</td>
            <td><StatusChip :status="promotion.status" /></td>
            <td>{{ promotion.priority }}</td>
            <td class="marketing__dates">
              {{ new Date(promotion.start_at).toLocaleString() }} –
              {{ new Date(promotion.end_at).toLocaleString() }}
            </td>
          </tr>
        </tbody>
      </table>
    </StateView>
  </div>
</template>

<style scoped lang="scss">
.marketing {
  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }

  &__toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 14px;
  }

  &__form {
    margin-bottom: 16px;
  }

  &__grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
    margin-bottom: 14px;

    label {
      display: flex;
      flex-direction: column;
      gap: 5px;
      font-size: 13px;
    }

    input {
      height: 34px;
      padding: 0 10px;
      border: 1px solid var(--nx-border-strong);
      border-radius: var(--nx-radius-control);
      background: var(--nx-surface);
      color: var(--nx-text);
      font-family: inherit;
      font-size: 13px;
    }
  }

  &__table {
    width: 100%;
    border-collapse: collapse;
    background: var(--nx-surface);
    border: 1px solid var(--nx-border);
    border-radius: var(--nx-radius-stage);
    overflow: hidden;
    font-size: 12.5px;

    th,
    td {
      padding: 9px 12px;
      text-align: left;
      border-bottom: 1px solid var(--nx-border);
    }

    th {
      background: var(--nx-surface-sunken);
      font-weight: 600;
      color: var(--nx-text-secondary);
    }

    tr:last-child td {
      border-bottom: none;
    }
  }

  &__dates {
    color: var(--nx-text-muted);
    white-space: nowrap;
  }
}
</style>
