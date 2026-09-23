<script setup lang="ts">
/**
 * Console · After-sales — approve / reject / refund, dense 京麦 layout.
 *
 * TWO VOCABULARIES, DELIBERATELY NOT MERGED (§34 vs §46)
 *  `AfterSale.status` is the BUSINESS CLAIM: NONE | PROCESSING | PARTIAL_REFUNDED | REFUNDED.
 *  The money fact lives on the `refunds[]` ledger (PENDING | SUCCEEDED | FAILED). An after-sale can
 *  exist with NO refund at all — a claim awaiting review, or one whose earlier refund failed — so
 *  collapsing the two into one column would report a claim as "refunded" when no money has moved.
 *  They are therefore two separate columns, and the money column reads the ledger, not the claim.
 *
 * REFUND CAPS are shown, never enforced as authority: the forms use
 * `afterSaleRefundable()` / `validateRefundAmount()` from `src/domain/afterSales/availability.ts` as
 * a UX ceiling, while the server independently rejects anything above the paid amount or the
 * per-item amount (`REFUND_EXCEEDS_PAID_AMOUNT` / `REFUND_EXCEEDS_ITEM_AMOUNT`) and refuses a repeat
 * payment (`REFUND_ALREADY_COMPLETED`). Every one of those is handled explicitly below.
 *
 * §104: actions here decide only what is OFFERED. The backend is the real authority — a 403 means
 * this UI and the server disagreed, which is a real case and must not crash the page.
 */
import { computed, reactive, ref } from 'vue'
import { afterSaleAdminApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import {
  afterSaleActionFlags,
  afterSaleBlockedReason,
  afterSaleRefundable,
  validateRefundAmount,
} from '@/domain/afterSales/availability'
import { useNotificationStore } from '@/stores/notification'
import { fromMajorString, toMajorString } from '@/utils/money'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'
import PriceText from '@/components/ui/PriceText.vue'
import type { AfterSale, AfterSaleStatus, AfterSaleType } from '@/types/domain'

const notifications = useNotificationStore()

/** Filter draft lives in this page — NOT in a store (§105). */
const filters = reactive({ afterSaleNo: '', status: '' as '' | AfterSaleStatus, page: 1, page_size: 20 })

const {
  data: dataPage,
  status,
  error,
  execute,
} = useAsyncState(
  () =>
    afterSaleAdminApi.list({
      page: filters.page,
      page_size: filters.page_size,
      // Empty select -> ABSENT key, never `status=` (buildAfterSaleListParams contract).
      status: filters.status || undefined,
      order_no: filters.afterSaleNo.trim() || undefined,
    }),
  { immediate: true },
)

const records = computed(() => dataPage.value?.items ?? [])
const meta = computed(() => dataPage.value?.meta ?? null)

/* -- rows ------------------------------------------------------------------ */

/**
 * The money-fact column. Reads the REFUND LEDGER, never the claim status: `SUCCEEDED` means money
 * actually moved, so "no refund yet" and "refunded" cannot be confused.
 */
function moneyFact(record: AfterSale): 'NONE' | 'PENDING' | 'SUCCEEDED' | 'FAILED' | 'PARTIAL' {
  if (record.refunds.length === 0) return 'NONE'
  if (record.refunds.some((r) => r.status === 'PENDING')) return 'PENDING'
  if (record.refunds.some((r) => r.status === 'FAILED')) return 'FAILED'
  // Money moved, but the claim is not fully settled yet.
  return afterSaleRefundable(record) > 0 ? 'PARTIAL' : 'SUCCEEDED'
}

const MONEY_LABEL: Record<string, string> = {
  NONE: '未退款',
  PENDING: '退款中',
  SUCCEEDED: '已退款',
  FAILED: '退款失败',
  PARTIAL: '部分退款',
}

const TYPE_LABEL: Record<AfterSaleType, string> = {
  REFUND_ONLY: '仅退款',
  RETURN_REFUND: '退货退款',
}

function flagsFor(record: AfterSale) {
  return afterSaleActionFlags(record)
}

function blockedReasonFor(status_: AfterSaleStatus): string {
  return afterSaleBlockedReason(status_)
}

/* -- filter bar ------------------------------------------------------------ */

function applyFilters(): void {
  filters.page = 1
  void execute()
}

function resetFilters(): void {
  filters.afterSaleNo = ''
  filters.status = ''
  filters.page = 1
  void execute()
}

function changePage(delta: number): void {
  const next = filters.page + delta
  if (next < 1) return
  if (meta.value && next > meta.value.total_pages) return
  filters.page = next
  void execute()
}

/* -- decide dialog --------------------------------------------------------- */

const actingNo = ref('')
const busy = ref(false)
const rejectReason = ref('')
const form = reactive({ mode: '' as '' | 'approve' | 'reject' | 'refund', amountYuan: '' })

const active = computed(
  () => records.value.find((item) => item.after_sale_no === actingNo.value) ?? null,
)

/** Server-ruled ceiling, via the shared module — not recomputed inline. */
const capMinor = computed(() => (active.value ? afterSaleRefundable(active.value) : 0))

/** Immediate client-side reason, so the operator is not told "invalid" by a round trip. */
const amountProblem = computed(() => {
  const record = active.value
  if (!record) return null
  if (form.mode === 'reject') return null
  const amount = fromMajorString(form.amountYuan)
  if (amount === null) return null
  if (form.mode === 'refund') return validateRefundAmount(record, amount)
  // Approval is capped at what the buyer asked for; approving MORE is never meaningful.
  if (amount > record.requested_amount) return '核准金额不能超过申请金额'
  if (amount <= 0) return '核准金额必须大于 0'
  return null
})

function open(record: AfterSale, mode: 'approve' | 'reject' | 'refund'): void {
  actingNo.value = record.after_sale_no
  form.mode = mode
  rejectReason.value = ''
  // Prefill with the remaining claimable amount: the operator's common case.
  form.amountYuan = toMajorString(mode === 'refund' ? afterSaleRefundable(record) : record.approved_amount > 0 ? record.approved_amount : record.requested_amount)
}

function close(): void {
  actingNo.value = ''
  form.mode = ''
}

function reportFailure(title: string, e: unknown): void {
  const normalized = normalizeError(e)
  if (normalized.forbidden) {
    notifications.error(
      '权限不足',
      '服务端拒绝了该操作：界面权限与服务端不一致，请刷新后重试或联系管理员。',
      normalized.code,
      normalized.traceId,
    )
    void execute()
    return
  }
  notifications.error(title, normalized.message, normalized.code, normalized.traceId)
}

async function submit(): Promise<void> {
  const record = active.value
  if (!record || !form.mode) return
  const mode = form.mode

  if (mode === 'reject') {
    if (!rejectReason.value.trim()) {
      notifications.warning('请填写驳回原因')
      return
    }
  } else {
    const problem = amountProblem.value
    if (problem) {
      notifications.warning('金额不合法', problem)
      return
    }
  }
  const amount = mode === 'reject' ? 0 : fromMajorString(form.amountYuan)
  if (mode !== 'reject' && (amount === null || amount <= 0)) {
    notifications.warning('请输入有效金额')
    return
  }

  busy.value = true
  try {
    if (mode === 'reject') {
      await afterSaleAdminApi.reject(record.after_sale_no, rejectReason.value.trim())
      notifications.success('已驳回')
    } else if (mode === 'approve') {
      await afterSaleAdminApi.approve(record.after_sale_no, { approved_amount: amount as number })
      notifications.success('已核准')
    } else {
      // The refund amount is a server-validated figure; the idempotency key stops a double submit
      // from paying twice (§96).
      await afterSaleAdminApi.refund(record.after_sale_no, {
        amount: amount as number,
        reason: '商家退款',
        idempotency_key: `refund-${record.after_sale_no}`,
      })
      notifications.success('退款已提交')
    }
    close()
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    if (normalized.code === 80_007) {
      notifications.warning('该退款已完成', '重复退款已被服务端拒绝')
      close()
      await execute()
    } else if (normalized.code === 80_004 || normalized.code === 80_005 || normalized.code === 80_006) {
      notifications.error('退款金额超出上限', normalized.message, normalized.code, normalized.traceId)
      await execute()
    } else {
      reportFailure('操作失败', e)
    }
  } finally {
    busy.value = false
  }
}

const MODE_TITLE: Record<'approve' | 'reject' | 'refund', string> = {
  approve: '核准售后',
  reject: '驳回售后',
  refund: '执行退款',
}
</script>

<template>
  <div class="as-list">
    <div class="nx-block">
      <!-- filter bar ------------------------------------------------------ -->
      <div class="nx-filterbar">
        <label>
          售后单号
          <input v-model="filters.afterSaleNo" class="nx-input" placeholder="支持单号查询" @keydown.enter="applyFilters()" />
        </label>
        <label>
          售后状态
          <select v-model="filters.status" class="nx-input">
            <option value="">全部</option>
            <option value="PROCESSING">处理中</option>
            <option value="PARTIAL_REFUNDED">部分退款</option>
            <option value="REFUNDED">已退款</option>
          </select>
        </label>
        <button type="button" class="nx-btn nx-btn--primary nx-btn--sm" @click="applyFilters()">查询</button>
        <button type="button" class="nx-btn nx-btn--sm" @click="resetFilters()">重置</button>
      </div>

      <StateView :state="status" :error="error" @retry="execute()">
        <div v-if="meta" class="as-list__count nx-muted">共 {{ meta.total }} 条</div>

        <table class="nx-table">
          <thead>
            <tr>
              <th style="width: 160px">售后单号</th>
              <th style="width: 160px">订单号</th>
              <th style="width: 90px">类型</th>
              <th style="width: 96px">售后状态</th>
              <th style="width: 96px">资金状态</th>
              <th style="width: 100px; text-align: right">申请金额</th>
              <th style="width: 100px; text-align: right">已核准</th>
              <th style="width: 100px; text-align: right">已退款</th>
              <th style="width: 100px; text-align: right">可退余额</th>
              <th style="width: 180px">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="record in records" :key="record.id">
              <td><code>{{ record.after_sale_no }}</code></td>
              <td><code>{{ record.order_no }}</code></td>
              <td>{{ TYPE_LABEL[record.type] }}</td>
              <!--
                TWO SEPARATE COLUMNS (§46). The left one is the business claim, the right one is the
                money fact read from the refund ledger. They are allowed to disagree — that is the
                whole point: a claim can be PROCESSING while no refund exists yet.
              -->
              <td><StatusChip :status="record.status" kind="aftersale" dot /></td>
              <td>
                <span class="as-list__money" :data-fact="moneyFact(record)">
                  {{ MONEY_LABEL[moneyFact(record)] }}
                </span>
              </td>
              <td style="text-align: right"><PriceText :amount="record.requested_amount" size="sm" muted :grouping="false" /></td>
              <td style="text-align: right"><PriceText :amount="record.approved_amount" size="sm" muted :grouping="false" /></td>
              <td style="text-align: right"><PriceText :amount="record.refunded_amount" size="sm" muted :grouping="false" /></td>
              <td style="text-align: right">
                <PriceText :amount="afterSaleRefundable(record)" size="sm" :grouping="false" />
              </td>
              <td>
                <div class="as-list__actions">
                  <button
                    v-if="flagsFor(record).approve"
                    type="button"
                    class="nx-btn nx-btn--text"
                    @click="open(record, 'approve')"
                  >
                    核准
                  </button>
                  <button
                    v-if="flagsFor(record).reject"
                    type="button"
                    class="nx-btn nx-btn--text"
                    @click="open(record, 'reject')"
                  >
                    驳回
                  </button>
                  <button
                    v-if="flagsFor(record).refund"
                    type="button"
                    class="nx-btn nx-btn--text"
                    @click="open(record, 'refund')"
                  >
                    退款
                  </button>
                  <span
                    v-if="!flagsFor(record).approve && !flagsFor(record).refund"
                    class="as-list__hint"
                    :title="blockedReasonFor(record.status)"
                  >
                    不可操作
                  </span>
                </div>
              </td>
            </tr>
          </tbody>
        </table>

        <!-- pager --------------------------------------------------------- -->
        <div v-if="meta" class="as-list__pager">
          <button type="button" class="nx-btn nx-btn--sm" :disabled="filters.page <= 1" @click="changePage(-1)">
            上一页
          </button>
          <span class="nx-muted">第 {{ meta.page }} / {{ meta.total_pages }} 页</span>
          <button
            type="button"
            class="nx-btn nx-btn--sm"
            :disabled="meta.total_pages > 0 && filters.page >= meta.total_pages"
            @click="changePage(1)"
          >
            下一页
          </button>
        </div>
      </StateView>
    </div>

    <!-- decide dialog ------------------------------------------------------ -->
    <div v-if="active && form.mode" class="as-list__modal" role="dialog" aria-modal="true" :aria-label="MODE_TITLE[form.mode]">
      <div class="nx-block as-list__modal-card">
        <div class="nx-block__head">
          <h2 class="nx-block__title">{{ MODE_TITLE[form.mode] }} · {{ active.after_sale_no }}</h2>
        </div>
        <div class="nx-block__body">
          <p class="nx-muted as-list__modal-hint">
            申请 <PriceText :amount="active.requested_amount" size="sm" /> ·
            已核准 <PriceText :amount="active.approved_amount" size="sm" /> ·
            已退款 <PriceText :amount="active.refunded_amount" size="sm" />
          </p>

          <label v-if="form.mode === 'reject'" class="as-list__field">
            <span>驳回原因</span>
            <input v-model="rejectReason" class="nx-input" maxlength="100" placeholder="例如：不符合退款条件" />
          </label>

          <template v-else>
            <label class="as-list__field">
              <span>{{ form.mode === 'approve' ? '核准金额（元）' : '退款金额（元）' }}</span>
              <input v-model="form.amountYuan" class="nx-input" inputmode="decimal" />
            </label>
            <p v-if="amountProblem" class="as-list__problem">{{ amountProblem }}</p>
            <p class="nx-muted as-list__modal-hint">
              可退上限
              <PriceText :amount="capMinor" size="sm" />
              ——服务端会在事务内独立复核，界面只负责提示。
            </p>
          </template>

          <div class="as-list__modal-actions">
            <button type="button" class="nx-btn nx-btn--primary" :disabled="busy" @click="submit()">确认</button>
            <button type="button" class="nx-btn" :disabled="busy" @click="close()">取消</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.as-list {
  &__count {
    padding: 8px 10px;
    font-size: 12px;
  }

  &__money {
    font-size: 12px;

    &[data-fact='SUCCEEDED'] {
      color: var(--nx-text);
    }

    &[data-fact='FAILED'] {
      color: var(--nx-price);
      font-weight: 600;
    }
  }

  &__actions {
    display: flex;
    align-items: center;
    gap: 6px;
    white-space: nowrap;
  }

  &__hint {
    color: var(--nx-text-muted);
    font-size: 11.5px;
  }

  &__pager {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px;
    font-size: 12px;
  }

  &__modal {
    position: fixed;
    inset: 0;
    z-index: 40;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgb(0 0 0 / 35%);
  }

  &__modal-card {
    width: min(440px, 92vw);
  }

  &__field {
    display: block;
    margin-bottom: 10px;
    font-size: 12px;

    span {
      display: block;
      margin-bottom: 4px;
      color: var(--nx-text-muted);
    }

    input {
      width: 100%;
    }
  }

  &__problem {
    margin: 0 0 6px;
    color: var(--nx-price);
    font-size: 12px;
  }

  &__modal-hint {
    margin: 0 0 10px;
    font-size: 11.5px;
  }

  &__modal-actions {
    display: flex;
    gap: 8px;
    margin-top: 12px;
  }
}
</style>
