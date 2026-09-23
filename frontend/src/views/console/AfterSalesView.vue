<script setup lang="ts">
/**
 * Console after-sales: approve / reject / refund.
 *
 * REFUND CAPS are shown, not enforced: the approve form caps the amount at the
 * request's own value, and the refund form caps at `requested - refunded`. The server
 * independently rejects anything above the paid amount or the per-item amount
 * (`REFUND_EXCEEDS_PAID_AMOUNT` / `REFUND_EXCEEDS_ITEM_AMOUNT`), and an executed refund
 * cannot be repeated (`REFUND_ALREADY_COMPLETED`).
 */
import { computed, reactive, ref } from 'vue'
import { afterSaleAdminApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { fromMajorString, toMajorString } from '@/utils/money'
import { normalizeError } from '@/api/error'
import { newTraceId } from '@/utils/trace'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'

const notifications = useNotificationStore()

const page = ref(1)
const statusFilter = ref('PROCESSING')

const {
  data: dataPage,
  status,
  error,
  execute,
} = useAsyncState(
  () => afterSaleAdminApi.list({ page: page.value, page_size: 20, status: statusFilter.value || undefined }),
  { immediate: true },
)

const records = computed(() => dataPage.value?.items ?? [])
const meta = computed(() => dataPage.value?.meta ?? null)

const actingNo = ref('')
const busy = ref(false)
const rejectReason = ref('')
const form = reactive({ mode: '' as '' | 'approve' | 'reject' | 'refund', amountYuan: '' })

const active = computed(() => records.value.find((item) => item.after_sale_no === actingNo.value) ?? null)

/** Remaining amount the server will still allow for this after-sale. */
const refundable = computed(() => {
  const record = active.value
  if (!record) return 0
  const base = record.approved_amount > 0 ? record.approved_amount : record.requested_amount
  return Math.max(0, base - record.refunded_amount)
})

function open(record: { after_sale_no: string; requested_amount: number; approved_amount: number }, mode: 'approve' | 'reject' | 'refund'): void {
  actingNo.value = record.after_sale_no
  form.mode = mode
  rejectReason.value = ''
  const base = record.approved_amount > 0 ? record.approved_amount : record.requested_amount
  form.amountYuan = toMajorString(base)
}

function close(): void {
  actingNo.value = ''
  form.mode = ''
}

async function submit(): Promise<void> {
  const record = active.value
  if (!record || !form.mode) return
  busy.value = true
  try {
    if (form.mode === 'reject') {
      if (!rejectReason.value.trim()) {
        notifications.warning('请填写驳回原因')
        return
      }
      await afterSaleAdminApi.reject(record.after_sale_no, rejectReason.value)
      notifications.success('已驳回')
    } else {
      const amount = fromMajorString(form.amountYuan)
      if (amount === null || amount <= 0) {
        notifications.warning('请输入有效金额')
        return
      }
      if (form.mode === 'approve') {
        if (amount > record.requested_amount) {
          notifications.warning('核准金额不能超过申请金额')
          return
        }
        await afterSaleAdminApi.approve(record.after_sale_no, { approved_amount: amount })
        notifications.success('已核准', `核准金额 ${amount} 分`)
      } else {
        if (amount > refundable.value) {
          notifications.warning('退款金额超过可退上限', `最多 ${refundable.value} 分`)
          return
        }
        await afterSaleAdminApi.refund(record.after_sale_no, {
          amount,
          reason: '商家退款',
          idempotency_key: `refund-${record.after_sale_no}-${newTraceId()}`,
        })
        notifications.success('退款已提交')
      }
    }
    close()
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    if (normalized.code === 80_007) {
      notifications.warning('该退款已完成', '重复退款已被服务端拒绝')
      await execute()
    } else if (normalized.code === 80_004 || normalized.code === 80_005) {
      notifications.error('退款金额超出上限', normalized.message, normalized.code, normalized.traceId)
      await execute()
    } else {
      notifications.error('操作失败', normalized.message, normalized.code, normalized.traceId)
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="c-as">
    <div class="c-as__head">
      <h2 class="nx-section-title">售后管理</h2>
      <select v-model="statusFilter" class="c-as__input" @change="page = 1; execute()">
        <option value="">全部</option>
        <option value="PROCESSING">处理中</option>
        <option value="PARTIAL_REFUNDED">部分退款</option>
        <option value="REFUNDED">已退款</option>
      </select>
    </div>

    <StateView
      :state="status"
      :error="error"
      :title="status === 'empty' ? '没有待处理的售后单' : undefined"
      @retry="execute()"
    >
      <table class="nx-table">
        <thead>
          <tr>
            <th>售后单</th>
            <th>订单号</th>
            <th>类型</th>
            <th>状态</th>
            <th>申请</th>
            <th>核准</th>
            <th>已退</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="record in records" :key="record.id">
            <td><code>{{ record.after_sale_no }}</code></td>
            <td><code>{{ record.order_no }}</code></td>
            <td>{{ record.type === 'REFUND_ONLY' ? '仅退款' : '退货退款' }}</td>
            <td><StatusChip :status="record.status" kind="aftersale" /></td>
            <td><PriceText :amount="record.requested_amount" size="sm" :grouping="false" /></td>
            <td><PriceText :amount="record.approved_amount" size="sm" :grouping="false" /></td>
            <td><PriceText :amount="record.refunded_amount" size="sm" :grouping="false" /></td>
            <td class="c-as__actions">
              <template v-if="record.status === 'PROCESSING'">
                <button type="button" class="nx-btn nx-btn--ghost" @click="open(record, 'approve')">核准</button>
                <button type="button" class="nx-btn nx-btn--ghost" @click="open(record, 'reject')">驳回</button>
              </template>
              <button
                v-else-if="record.status === 'PARTIAL_REFUNDED'"
                type="button"
                class="nx-btn nx-btn--ghost"
                @click="open(record, 'refund')"
              >
                继续退款
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="meta" class="c-as__pager">
        <button type="button" class="nx-btn" :disabled="page <= 1" @click="page -= 1; execute()">上一页</button>
        <span class="nx-muted">第 {{ meta.page }} / {{ meta.total_pages }} 页</span>
        <button
          type="button"
          class="nx-btn"
          :disabled="meta.total_pages > 0 && page >= meta.total_pages"
          @click="page += 1; execute()"
        >
          下一页
        </button>
      </div>
    </StateView>

    <div v-if="active && form.mode" class="c-as__modal" role="dialog" aria-modal="true" aria-label="售后处理">
      <div class="c-as__modal-card nx-card">
        <div class="nx-card__body">
          <h3 class="nx-section-title">
            {{ form.mode === 'approve' ? '核准售后' : form.mode === 'reject' ? '驳回售后' : '执行退款' }}
            · {{ active.after_sale_no }}
          </h3>

          <p class="nx-muted">
            申请 <PriceText :amount="active.requested_amount" size="sm" /> · 核准
            <PriceText :amount="active.approved_amount" size="sm" /> · 已退 <PriceText :amount="active.refunded_amount" size="sm" />
          </p>

          <label v-if="form.mode !== 'reject'" class="c-as__field">
            <span>
              金额（元）
              <template v-if="form.mode === 'refund'"> · 可退上限 <PriceText :amount="refundable" size="sm" muted /></template>
            </span>
            <input v-model="form.amountYuan" inputmode="decimal" />
          </label>

          <label v-if="form.mode === 'reject'" class="c-as__field">
            <span>驳回原因</span>
            <textarea v-model="rejectReason" rows="3" maxlength="200" />
          </label>

          <div class="c-as__modal-actions">
            <button type="button" class="nx-btn nx-btn--primary" :disabled="busy" @click="submit()">确认</button>
            <button type="button" class="nx-btn" :disabled="busy" @click="close()">取消</button>
          </div>

          <p class="nx-muted c-as__note">
            退款上限（实付 − 已退）由服务端在事务内二次校验；前端上限只是录入提示。
            退款请求带幂等键，重复提交不会重复扣款。
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.c-as {
  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }

  &__input {
    height: 34px;
    padding: 0 10px;
    border: 1px solid var(--nx-border-strong);
    border-radius: var(--nx-radius-control);
    background: var(--nx-surface);
    color: var(--nx-text);
    font-family: inherit;
    font-size: 13px;
  }


  &__actions {
    display: flex;
    gap: 4px;
  }

  &__pager {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 14px;
  }

  &__modal {
    position: fixed;
    inset: 0;
    z-index: 100;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(0, 0, 0, 0.45);
  }

  &__modal-card {
    width: min(440px, 92vw);
  }

  &__field {
    display: flex;
    flex-direction: column;
    gap: 5px;
    margin: 12px 0;
    font-size: 13px;

    input,
    textarea {
      padding: 8px 10px;
      border: 1px solid var(--nx-border-strong);
      border-radius: var(--nx-radius-control);
      background: var(--nx-surface);
      color: var(--nx-text);
      font-family: inherit;
      font-size: 13px;
    }

    textarea {
      resize: vertical;
    }
  }

  &__modal-actions {
    display: flex;
    gap: 8px;
  }

  &__note {
    margin: 12px 0 0;
    font-size: 11.5px;
    line-height: 1.6;
  }
}
</style>
