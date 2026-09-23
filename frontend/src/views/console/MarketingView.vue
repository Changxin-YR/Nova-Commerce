<script setup lang="ts">
/**
 * Console · Marketing — coupons and promotions, dense 京麦 layout.
 *
 * TWO DISTINCT VOCABULARIES
 *  A coupon lifecycle (UNUSED -> LOCKED -> USED, plus EXPIRED) is not a promotion lifecycle
 *  (DRAFT -> ACTIVE -> ENDED). They are rendered on separate tabs with their own columns rather than
 *  one merged "status" column, because the same word would otherwise mean two different things.
 *
 * PREVIEW BEFORE CREATE (§47, §110)
 *  Promotion creation must be previewed first, so creation is NEVER a single submit. The coupon form
 *  here is therefore a two-step flow: 预览 computes and shows exactly what will be sent, and only an
 *  explicit 确认提交 performs the write. Nothing is posted from the form step.
 *
 * WHAT THIS PAGE DELIBERATELY DOES NOT DO
 *  There is no promotion-create flow. §47 requires a preview step and `PROMOTION_PREVIEW_REQUIRED`
 *  (90003) exists as an error code, but no preview or create endpoint is frozen, so building the
 *  form would mean inventing an endpoint — reported instead (see the migration report).
 *
 * §104: the availability rules below decide only what is OFFERED. The server is the authority and
 * answers `PROMOTION_CONFLICT` (90001) / `COUPON_ALREADY_LOCKED` (90008).
 */
import { computed, reactive, ref } from 'vue'
import { marketingAdminApi, type CouponPreviewResult } from '@/api'

import { useAsyncState } from '@/composables/useAsyncState'
import {
  canPublishPromotion,
  canUnpublishPromotion,
  isCouponAvailable,
  isPromotionTerminal,
  promotionActionBlockedReason,
} from '@/domain/marketing/availability'
import { useNotificationStore } from '@/stores/notification'
import { fromMajorString, toMajorString } from '@/utils/money'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'
import PriceText from '@/components/ui/PriceText.vue'

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

function switchTab(next: 'coupons' | 'promotions'): void {
  tab.value = next
  if (next === 'promotions') void loadPromotions()
}

/* -- coupon creation: preview -> confirm (§47) ----------------------------- */

const formOpen = ref(false)
/** `preview` is NOT the same as "ready to submit" — an explicit confirm step follows it. */
const step = ref<'form' | 'preview'>('form')

/**
 * The SERVER's preview response, including `preview_token`.
 *
 * The token is the mechanism §12.1 describes: the create call must echo the token the preview
 * returned, so the server can prove the operator approved the exact thing being written. Holding it
 * here (rather than re-deriving it) is what makes the two steps one flow instead of two requests.
 */
const serverPreview = ref<CouponPreviewResult | null>(null)
const busy = ref(false)
const form = reactive({
  code: '',
  name: '',
  discountYuan: '',
  thresholdYuan: '',
  validFrom: '',
  validTo: '',
})

/** The exact request that WOULD be sent, built once so preview and submit cannot diverge. */
const draftPayload = computed(() => {
  const discount = fromMajorString(form.discountYuan)
  const threshold = fromMajorString(form.thresholdYuan)
  if (discount === null || threshold === null) return null
  return {
    code: form.code.trim(),
    name: form.name.trim(),
    discount_amount: discount,
    threshold_amount: threshold,
    valid_from: form.validFrom || new Date().toISOString(),
    valid_to: form.validTo || new Date(Date.now() + 30 * 86400_000).toISOString(),
  }
})

const draftProblem = computed(() => {
  if (!form.code.trim()) return '请填写券码'
  if (!form.name.trim()) return '请填写券名称'
  const discount = fromMajorString(form.discountYuan)
  const threshold = fromMajorString(form.thresholdYuan)
  if (discount === null) return '面额必须是数字'
  if (threshold === null) return '使用门槛必须是数字'
  if (discount <= 0) return '面额必须大于 0'
  if (threshold < 0) return '使用门槛不能为负'
  return null
})

function openForm(): void {
  formOpen.value = !formOpen.value
  step.value = 'form'
}

/**
 * Step 1 of 2: calls `POST /marketing/coupons/preview` (§12.1). It NEVER writes — the endpoint
 * exists so the operator reviews what the server will accept before anything is committed, and
 * `PROMOTION_PREVIEW_REQUIRED` (90003) is the server-side enforcement of that (§47).
 */
async function preview(): Promise<void> {
  const payload = draftPayload.value
  if (!payload || draftProblem.value) {
    notifications.warning('请先修正表单', draftProblem.value ?? '表单不完整')
    return
  }
  busy.value = true
  try {
    serverPreview.value = await marketingAdminApi.previewCoupon(payload)
    step.value = 'preview'
  } catch (e) {
    const normalized = normalizeError(e)
    // 90003 means the server wants a preview first: that is this flow, so it should not happen —
    // report it rather than silently retrying, because it signals the two sides disagree.
    notifications.error('预览失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    busy.value = false
  }
}

function backToForm(): void {
  step.value = 'form'
  // Editing invalidates the approval: the token covers the values the operator SAW, so keeping it
  // across an edit is exactly the drift §12.1 exists to prevent.
  serverPreview.value = null
}

/** Step 2: the ONLY place a write happens, from the payload the operator just saw. */
async function confirmCreate(): Promise<void> {
  const payload = draftPayload.value
  const token = serverPreview.value?.preview_token
  // No token means no approval yet, so there is nothing legitimate to write.
  if (!payload || !token) {
    notifications.warning('请先预览', '创建需要预览返回的 token，确保写入内容与审核内容一致')
    step.value = 'form'
    return
  }
  busy.value = true
  try {
    await marketingAdminApi.createCoupon({ ...payload, preview_token: token })
    notifications.success('优惠券已创建')
    formOpen.value = false
    step.value = 'form'
    serverPreview.value = null
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

/* -- promotion task actions ------------------------------------------------ */

const busyPromotionId = ref('')

async function runPromotionAction(action: 'publish' | 'unpublish', id: string | number): Promise<void> {
  busyPromotionId.value = String(id)
  try {
    if (action === 'publish') {
      await marketingAdminApi.publishPromotion(id)
      notifications.success('活动已上线')
    } else {
      await marketingAdminApi.unpublishPromotion(id)
      notifications.success('活动已下线')
    }
    await loadPromotions()
  } catch (e) {
    const normalized = normalizeError(e)
    if (normalized.forbidden) {
      notifications.error(
        '权限不足',
        '服务端拒绝了该操作：界面权限与服务端不一致，请刷新后重试或联系管理员。',
        normalized.code,
        normalized.traceId,
      )
      await loadPromotions()
    } else if (normalized.code === 90_001) {
      notifications.warning('活动状态已变化', '该活动已被其他操作改变，列表已刷新，请重试。')
      await loadPromotions()
    } else {
      notifications.error(action === 'publish' ? '上线失败' : '下线失败', normalized.message, normalized.code, normalized.traceId)
    }
  } finally {
    busyPromotionId.value = ''
  }
}

function stamp(iso: string): string {
  const date = new Date(iso)
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
}

/** Template hint: shows the minor-unit convention instead of a bare example. */
const sampleDiscount = toMajorString(1000)
</script>

<template>
  <div class="marketing">
    <div class="nx-block">
      <div class="marketing__head">
        <div class="nx-tabs">
          <button
            type="button"
            class="nx-tab"
            :class="{ 'nx-tab--active': tab === 'coupons' }"
            @click="switchTab('coupons')"
          >
            优惠券
          </button>
          <button
            type="button"
            class="nx-tab"
            :class="{ 'nx-tab--active': tab === 'promotions' }"
            @click="switchTab('promotions')"
          >
            促销活动
          </button>
        </div>
        <button v-if="tab === 'coupons'" type="button" class="nx-btn nx-btn--primary nx-btn--sm" @click="openForm()">
          {{ formOpen ? '收起' : '新建优惠券' }}
        </button>
      </div>

      <!-- ============ coupons ============ -->
      <template v-if="tab === 'coupons'">
        <div v-if="formOpen" class="marketing__form nx-block">
          <div class="nx-block__head">
            <h3 class="nx-block__title">
              {{ step === 'form' ? '新建优惠券' : '确认提交（预览）' }}
            </h3>
          </div>
          <div class="nx-block__body">
            <!-- STEP 1: describe it. Nothing is written from here. -->
            <template v-if="step === 'form'">
              <div class="marketing__fields">
                <label>
                  券码
                  <input v-model="form.code" class="nx-input" maxlength="32" placeholder="例如 NOVA100" />
                </label>
                <label>
                  名称
                  <input v-model="form.name" class="nx-input" maxlength="60" placeholder="例如 满 1000 减 10" />
                </label>
                <label>
                  面额（元）
                  <input v-model="form.discountYuan" class="nx-input" inputmode="decimal" />
                </label>
                <label>
                  使用门槛（元）
                  <input v-model="form.thresholdYuan" class="nx-input" inputmode="decimal" />
                </label>
                <label>
                  生效日期
                  <input v-model="form.validFrom" class="nx-input" type="date" />
                </label>
                <label>
                  失效日期
                  <input v-model="form.validTo" class="nx-input" type="date" />
                </label>
              </div>
              <p class="nx-muted marketing__hint">
                示例：满 {{ sampleDiscount }} 元可用。金额以整数分提交，界面上是元。
              </p>
              <p v-if="draftProblem" class="marketing__problem">{{ draftProblem }}</p>
              <div class="marketing__actions">
                <button type="button" class="nx-btn nx-btn--primary nx-btn--sm" :disabled="busy" @click="preview()">
                  {{ busy ? '预览中…' : '预览' }}
                </button>
              </div>
            </template>

            <!-- STEP 2: the review step. Creation cannot happen without it (§47). -->
            <template v-else>
              <p class="nx-muted marketing__hint">
                以下为服务端预览结果（含 preview_token），确认前不会写入。
              </p>
              <!--
                The server's own findings. A preview that could not disagree with the form would not be
                worth a round trip, so warnings are rendered rather than swallowed.
              -->
              <ul v-if="serverPreview?.warnings?.length" class="marketing__warnings">
                <li v-for="(warning, index) in serverPreview.warnings" :key="index">{{ warning }}</li>
              </ul>
              <dl v-if="draftPayload" class="marketing__preview">
                <div><dt>券码</dt><dd><code>{{ draftPayload.code }}</code></dd></div>
                <div><dt>名称</dt><dd>{{ draftPayload.name }}</dd></div>
                <div>
                  <dt>面额</dt>
                  <dd><PriceText :amount="draftPayload.discount_amount" size="sm" /></dd>
                </div>
                <div>
                  <dt>使用门槛</dt>
                  <dd><PriceText :amount="draftPayload.threshold_amount" size="sm" /></dd>
                </div>
                <div><dt>生效</dt><dd>{{ stamp(draftPayload.valid_from) }}</dd></div>
                <div><dt>失效</dt><dd>{{ stamp(draftPayload.valid_to) }}</dd></div>
              </dl>
              <div class="marketing__actions">
                <button
                  type="button"
                  class="nx-btn nx-btn--primary nx-btn--sm"
                  :disabled="busy || !serverPreview"
                  @click="confirmCreate()"
                >
                  确认提交
                </button>
                <button type="button" class="nx-btn nx-btn--sm" :disabled="busy" @click="backToForm()">
                  返回修改
                </button>
              </div>
            </template>
          </div>
        </div>

        <StateView :state="couponStatus" :error="couponError" @retry="loadCoupons()">
          <table class="nx-table">
            <thead>
              <tr>
                <th style="width: 140px">券码</th>
                <th style="width: 200px">名称</th>
                <th style="width: 90px">券状态</th>
                <th style="width: 110px; text-align: right">面额</th>
                <th style="width: 110px; text-align: right">门槛</th>
                <th style="width: 190px">有效期</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="coupon in coupons" :key="coupon.id">
                <td><code>{{ coupon.code }}</code></td>
                <td>{{ coupon.name }}</td>
                <td>
                  <StatusChip :status="coupon.status" kind="doc" dot />
                  <!-- LOCKED means another order holds it; it must not read as available. -->
                  <span v-if="!isCouponAvailable(coupon.status)" class="marketing__hint">不可用</span>
                </td>
                <td style="text-align: right"><PriceText :amount="coupon.discount_amount" size="sm" :grouping="false" /></td>
                <td style="text-align: right"><PriceText :amount="coupon.threshold_amount" size="sm" muted :grouping="false" /></td>
                <td class="nx-muted">{{ stamp(coupon.valid_from) }} ~ {{ stamp(coupon.valid_to) }}</td>
              </tr>
            </tbody>
          </table>
        </StateView>
      </template>

      <!-- ============ promotions ============ -->
      <template v-else>
        <p class="nx-muted marketing__hint marketing__hint--block">
          活动创建需要先预览（§47），但预览与创建端点均未冻结，故此页只提供状态流转，不提供创建表单。
        </p>
        <StateView :state="promotionStatus" :error="promotionError" @retry="loadPromotions()">
          <table class="nx-table">
            <thead>
              <tr>
                <th style="width: 220px">活动名称</th>
                <th style="width: 120px">类型</th>
                <th style="width: 100px">活动状态</th>
                <th style="width: 80px; text-align: right">优先级</th>
                <th style="width: 190px">起止时间</th>
                <th style="width: 160px">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="promotion in promotions" :key="promotion.id">
                <td>{{ promotion.name }}</td>
                <td>{{ promotion.type }}</td>
                <td><StatusChip :status="promotion.status" kind="doc" dot /></td>
                <td style="text-align: right">{{ promotion.priority }}</td>
                <td class="nx-muted">{{ stamp(promotion.start_at) }} ~ {{ stamp(promotion.end_at) }}</td>
                <td>
                  <div class="marketing__row-actions">
                    <button
                      v-if="canPublishPromotion(promotion.status)"
                      type="button"
                      class="nx-btn nx-btn--text"
                      :disabled="busyPromotionId === String(promotion.id)"
                      @click="runPromotionAction('publish', promotion.id)"
                    >
                      上线
                    </button>
                    <button
                      v-if="canUnpublishPromotion(promotion.status)"
                      type="button"
                      class="nx-btn nx-btn--text"
                      :disabled="busyPromotionId === String(promotion.id)"
                      @click="runPromotionAction('unpublish', promotion.id)"
                    >
                      下线
                    </button>
                    <span
                      v-if="isPromotionTerminal(promotion.status)"
                      class="marketing__hint"
                      :title="promotionActionBlockedReason('publish', promotion.status)"
                    >
                      已结束
                    </span>
                  </div>
                </td>
              </tr>
            </tbody>
          </table>
        </StateView>
      </template>
    </div>
  </div>
</template>

<style scoped lang="scss">
.marketing {
  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 10px;
    border-bottom: 1px solid var(--nx-border);
  }

  &__form {
    margin: 10px;
  }

  &__fields {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 10px;

    label {
      display: block;
      font-size: 12px;
      color: var(--nx-text-muted);

      input {
        width: 100%;
        margin-top: 4px;
      }
    }
  }

  &__preview {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 6px 16px;
    margin: 0 0 10px;
    font-size: 12px;

    div {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      border-bottom: 1px solid var(--nx-border);
      padding: 4px 0;
    }

    dt {
      color: var(--nx-text-muted);
    }

    dd {
      margin: 0;
    }
  }

  &__hint {
    margin-left: 6px;
    color: var(--nx-text-muted);
    font-size: 11.5px;

    &--block {
      display: block;
      margin: 10px;
    }
  }

  &__warnings {
    margin: 0 0 10px;
    padding-left: 18px;
    color: var(--nx-price);
    font-size: 12px;
  }

  &__problem {
    margin: 8px 0 0;
    color: var(--nx-price);
    font-size: 12px;
  }

  &__actions {
    display: flex;
    gap: 8px;
    margin-top: 10px;
  }

  &__row-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    white-space: nowrap;
  }
}
</style>
