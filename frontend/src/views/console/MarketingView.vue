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
import {
  marketingAdminApi,
  type CouponPreviewResult,
  type PromotionDraft,
  type PromotionPreview,
} from '@/api'

import { useAsyncState } from '@/composables/useAsyncState'
import type { PromotionType } from '@/types/frozen-contract'
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

const PROMOTION_TYPE_LABELS: Record<PromotionType, string> = {
  DIRECT_DISCOUNT: '直降',
  PERCENT_DISCOUNT: '折扣',
  FULL_REDUCTION: '满减',
}

/* -- promotion creation: preview -> confirm (§47, §13.2) -------------------- */

const promoFormOpen = ref(false)
const promoStep = ref<'form' | 'preview'>('form')
const promoBusy = ref(false)
/** The SERVER's preview, including the `preview_token` the create call must echo. */
const promoPreview = ref<PromotionPreview | null>(null)

const promoForm = reactive({
  name: '',
  description: '',
  promotionType: 'FULL_REDUCTION' as PromotionType,
  priority: 100,
  stackable: false,
  totalQuota: 1000,
  startsAt: '',
  endsAt: '',
  // DIRECT_DISCOUNT
  discountYuan: '',
  // PERCENT_DISCOUNT (basis points — never a float, §13.1)
  discountBps: '',
  // FULL_REDUCTION
  thresholdYuan: '',
  reductionYuan: '',
  // shared cap
  maxDiscountYuan: '',
  // scope is EXPLICIT: `all_products: true` rather than "empty arrays means everything"
  allProducts: true,
  productIds: '',
  brandIds: '',
})

/** Split a comma-separated id list, ignoring blanks. */
function parseIds(raw: string): number[] {
  return raw
    .split(',')
    .map((part) => Number(part.trim()))
    .filter((value) => Number.isInteger(value) && value > 0)
}

/**
 * The exact body the create call would send, assembled once so the preview and the submit cannot
 * diverge. `rule_config` is built as the DISCRIMINATED variant the chosen `promotion_type` implies,
 * because §13.1 defines one variant per type rather than a bag of nullable keys.
 */
const promotionDraft = computed<PromotionDraft | null>(() => {
  const maxDiscountAmountMinor = promoForm.maxDiscountYuan ? fromMajorString(promoForm.maxDiscountYuan) : null

  let rule_config: PromotionDraft['rule_config'] | null = null
  if (promoForm.promotionType === 'DIRECT_DISCOUNT') {
    const amount = fromMajorString(promoForm.discountYuan)
    if (amount !== null) rule_config = { discount_amount: amount }
  } else if (promoForm.promotionType === 'PERCENT_DISCOUNT') {
    const bps = Number(promoForm.discountBps.trim())
    // BASIS POINTS, not a percentage or a float: 12.5% is exactly 1250.
    if (Number.isInteger(bps) && bps > 0) {
      rule_config = { discount_bps: bps, max_discount_amount: maxDiscountAmountMinor }
    }
  } else {
    const threshold = fromMajorString(promoForm.thresholdYuan)
    const reduction = fromMajorString(promoForm.reductionYuan)
    if (threshold !== null && reduction !== null) {
      rule_config = {
        threshold_amount: threshold,
        reduction_amount: reduction,
        max_discount_amount: maxDiscountAmountMinor,
      }
    }
  }

  if (!rule_config) return null
  if (!promoForm.name.trim() || !promoForm.startsAt || !promoForm.endsAt) return null

  return {
    name: promoForm.name.trim(),
    description: promoForm.description.trim() || undefined,
    promotion_type: promoForm.promotionType,
    priority: Number(promoForm.priority) || 0,
    stackable: promoForm.stackable,
    rule_config,
    scope: {
      all_products: promoForm.allProducts,
      product_ids: promoForm.allProducts ? [] : parseIds(promoForm.productIds),
      category_ids: [],
      brand_ids: promoForm.allProducts ? [] : parseIds(promoForm.brandIds),
    },
    starts_at: new Date(promoForm.startsAt).toISOString(),
    ends_at: new Date(promoForm.endsAt).toISOString(),
    total_quota: Number(promoForm.totalQuota) || 0,
  }
})

const promoProblem = computed(() => {
  if (!promoForm.name.trim()) return '请填写活动名称'
  if (!promoForm.startsAt) return '请选择开始时间'
  if (!promoForm.endsAt) return '请选择结束时间'
  if (new Date(promoForm.endsAt) <= new Date(promoForm.startsAt)) return '结束时间必须晚于开始时间'
  if (promoForm.allProducts) return null
  if (parseIds(promoForm.productIds).length === 0 && parseIds(promoForm.brandIds).length === 0) {
    return '未勾选全场时，至少填写一个商品或品牌范围'
  }
  return null
})

function openPromoForm(): void {
  promoFormOpen.value = !promoFormOpen.value
  promoStep.value = 'form'
  promoPreview.value = null
}

/** Step 1 of 2: previews on the server. NEVER writes. */
async function previewPromotion(): Promise<void> {
  const payload = promotionDraft.value
  if (!payload || promoProblem.value) {
    notifications.warning('请先修正表单', promoProblem.value ?? '表单不完整')
    return
  }
  promoBusy.value = true
  try {
    promoPreview.value = await marketingAdminApi.previewPromotion(payload)
    promoStep.value = 'preview'
  } catch (e) {
    const normalized = normalizeError(e)
    notifications.error('预览失败', normalized.message, normalized.code, normalized.traceId)
  } finally {
    promoBusy.value = false
  }
}

function backToPromoForm(): void {
  promoStep.value = 'form'
  // Editing invalidates the approval: the token covers the values the operator SAW.
  promoPreview.value = null
}

/** Step 2 of 2: the ONLY place a promotion is written. */
async function confirmCreatePromotion(): Promise<void> {
  const payload = promotionDraft.value
  const token = promoPreview.value?.preview_token
  if (!payload || !token) {
    notifications.warning('请先预览', '创建需要预览返回的 token，确保写入内容与审核内容一致')
    promoStep.value = 'form'
    return
  }
  promoBusy.value = true
  try {
    await marketingAdminApi.createPromotion({ ...payload, preview_token: token })
    notifications.success('促销活动已创建')
    promoFormOpen.value = false
    promoStep.value = 'form'
    promoPreview.value = null
    await loadPromotions()
  } catch (e) {
    const normalized = normalizeError(e)
    if (normalized.code === 90_003) {
      notifications.warning('需要重新预览', '服务端要求携带有效的预览 token，请重新预览后提交。')
      promoStep.value = 'form'
    } else {
      notifications.error('创建失败', normalized.message, normalized.code, normalized.traceId)
    }
  } finally {
    promoBusy.value = false
  }
}
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
        <div class="marketing__toolbar">
          <button type="button" class="nx-btn nx-btn--primary nx-btn--sm" @click="openPromoForm()">
            {{ promoFormOpen ? '收起' : '新建促销活动' }}
          </button>
          <span class="nx-muted">创建需先预览（§47）：预览结果含 preview_token，确认后才写入。</span>
        </div>

        <div v-if="promoFormOpen" class="marketing__form nx-block">
          <div class="nx-block__head">
            <h3 class="nx-block__title">
              {{ promoStep === 'form' ? '新建促销活动' : '确认提交（服务端预览）' }}
            </h3>
          </div>
          <div class="nx-block__body">
            <template v-if="promoStep === 'form'">
              <div class="marketing__fields">
                <label>
                  活动名称
                  <input v-model="promoForm.name" class="nx-input" maxlength="60" />
                </label>
                <label>
                  促销类型
                  <select v-model="promoForm.promotionType" class="nx-input">
                    <option value="FULL_REDUCTION">满减</option>
                    <option value="DIRECT_DISCOUNT">直降</option>
                    <option value="PERCENT_DISCOUNT">折扣</option>
                  </select>
                </label>
                <label>
                  开始时间
                  <input v-model="promoForm.startsAt" class="nx-input" type="datetime-local" />
                </label>
                <label>
                  结束时间
                  <input v-model="promoForm.endsAt" class="nx-input" type="datetime-local" />
                </label>
                <label>
                  优先级
                  <input v-model.number="promoForm.priority" class="nx-input" type="number" min="0" />
                </label>
                <label>
                  总名额
                  <input v-model.number="promoForm.totalQuota" class="nx-input" type="number" min="0" />
                </label>

                <!--
                  `rule_config` is DISCRIMINATED by promotion_type (§13.1): the fields shown depend on
                  the type, because the server defines one variant per type rather than a bag of
                  nullable keys.
                -->
                <template v-if="promoForm.promotionType === 'FULL_REDUCTION'">
                  <label>
                    门槛（元）
                    <input v-model="promoForm.thresholdYuan" class="nx-input" inputmode="decimal" />
                  </label>
                  <label>
                    减免（元）
                    <input v-model="promoForm.reductionYuan" class="nx-input" inputmode="decimal" />
                  </label>
                </template>
                <label v-else-if="promoForm.promotionType === 'DIRECT_DISCOUNT'">
                  直降（元/件）
                  <input v-model="promoForm.discountYuan" class="nx-input" inputmode="decimal" />
                </label>
                <label v-else>
                  折扣（基点 bps，1250 = 12.5%）
                  <input v-model="promoForm.discountBps" class="nx-input" inputmode="numeric" />
                </label>

                <label>
                  折扣上限（元，可空）
                  <input v-model="promoForm.maxDiscountYuan" class="nx-input" inputmode="decimal" />
                </label>
                <label>
                  冲突时是否叠加
                  <input v-model="promoForm.stackable" type="checkbox" />
                </label>
              </div>

              <!--
                `scope` is EXPLICIT rather than inferred from empty arrays (§13.1): "no scope means
                everything" is how a promotion accidentally covers the whole catalogue.
              -->
              <div class="marketing__scope">
                <label>
                  <input v-model="promoForm.allProducts" type="checkbox" />
                  全场商品
                </label>
                <template v-if="!promoForm.allProducts">
                  <label>
                    商品 ID（逗号分隔）
                    <input v-model="promoForm.productIds" class="nx-input" placeholder="3,7,11" />
                  </label>
                  <label>
                    品牌 ID（逗号分隔）
                    <input v-model="promoForm.brandIds" class="nx-input" placeholder="2" />
                  </label>
                </template>
              </div>

              <p v-if="promoProblem" class="marketing__problem">{{ promoProblem }}</p>
              <div class="marketing__actions">
                <button type="button" class="nx-btn nx-btn--primary nx-btn--sm" :disabled="promoBusy" @click="previewPromotion()">
                  {{ promoBusy ? '预览中…' : '预览' }}
                </button>
              </div>
            </template>

            <template v-else>
              <p class="nx-muted marketing__hint">
                以下为服务端预览结果（含 preview_token），确认前不会写入。
              </p>
              <dl v-if="promoPreview" class="marketing__preview">
                <div><dt>令牌</dt><dd><code>{{ promoPreview.preview_token }}</code></dd></div>
                <div><dt>过期</dt><dd>{{ stamp(promoPreview.expires_at) }}</dd></div>
                <div>
                  <dt>预计影响 SKU</dt>
                  <dd>{{ promoPreview.estimated_impact.affected_sku_count }}</dd>
                </div>
                <div>
                  <dt>预计 30 天折扣</dt>
                  <dd>
                    <PriceText :amount="promoPreview.estimated_impact.estimated_discount_amount_30d" size="sm" />
                  </dd>
                </div>
              </dl>
              <!--
                `conflicts` is a POPULATED LIST rather than a 409 (§13.2): a conflict is information the
                operator needs in order to decide, not an error that stops them looking.
              -->
              <ul v-if="promoPreview?.conflicts?.length" class="marketing__conflicts">
                <li v-for="conflict in promoPreview.conflicts" :key="conflict.promotion_no">
                  与 <code>{{ conflict.promotion_no }}</code> 冲突（{{ conflict.reason }}）
                </li>
              </ul>
              <ul v-if="promoPreview?.warnings?.length" class="marketing__warnings">
                <li v-for="(warning, index) in promoPreview.warnings" :key="index">{{ warning }}</li>
              </ul>
              <div class="marketing__actions">
                <button
                  type="button"
                  class="nx-btn nx-btn--primary nx-btn--sm"
                  :disabled="promoBusy || !promoPreview"
                  @click="confirmCreatePromotion()"
                >
                  确认提交
                </button>
                <button type="button" class="nx-btn nx-btn--sm" :disabled="promoBusy" @click="backToPromoForm()">
                  返回修改
                </button>
              </div>
            </template>
          </div>
        </div>
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
                <td>{{ PROMOTION_TYPE_LABELS[promotion.promotion_type] }}</td>
                <td><StatusChip :status="promotion.status" kind="doc" dot /></td>
                <td style="text-align: right">{{ promotion.priority }}</td>
                <td class="nx-muted">{{ stamp(promotion.starts_at) }} ~ {{ stamp(promotion.ends_at) }}</td>
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

  &__toolbar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 10px;
    font-size: 12px;
  }

  &__scope {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 10px;
    margin-top: 10px;
    font-size: 12px;

    label {
      display: block;
      color: var(--nx-text-muted);

      input[type='text'],
      input:not([type]) {
        width: 100%;
        margin-top: 4px;
      }
    }
  }

  &__conflicts {
    margin: 0 0 10px;
    padding-left: 18px;
    color: var(--nx-price);
    font-size: 12px;
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
