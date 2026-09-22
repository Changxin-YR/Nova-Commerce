<script setup lang="ts">
/**
 * Console order management: list, detail drawer, task-based shipping (§99).
 *
 * Shipping requires a carrier, a tracking number and an idempotency key, so a retried
 * request cannot create a second shipment (§70 003 FULFILLMENT_ALREADY_SHIPPED is the
 * server's own guard, not the UI's).
 */
import { computed, reactive, ref } from 'vue'
import { orderAdminApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { useNotificationStore } from '@/stores/notification'
import { formatMoney } from '@/utils/money'
import { normalizeError } from '@/api/error'
import { newTraceId } from '@/utils/trace'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'

const notifications = useNotificationStore()

const page = ref(1)
const statusFilter = ref('')
const keyword = ref('')

const {
  data: orderData,
  status,
  error,
  execute,
} = useAsyncState(
  () =>
    orderAdminApi.list({
      page: page.value,
      page_size: 20,
      status: statusFilter.value || undefined,
      keyword: keyword.value || undefined,
    }),
  { immediate: true },
)

const orders = computed(() => orderData.value?.items ?? [])
const meta = computed(() => orderData.value?.meta ?? null)

const shippingOrderNo = ref('')
const busy = ref(false)
const shipForm = reactive({ carrier: '', tracking_no: '' })

function openShip(orderNo: string): void {
  shippingOrderNo.value = orderNo
  shipForm.carrier = ''
  shipForm.tracking_no = ''
}

async function submitShip(): Promise<void> {
  if (!shippingOrderNo.value) return
  if (!shipForm.carrier.trim() || !shipForm.tracking_no.trim()) {
    notifications.warning('请填写承运商与运单号')
    return
  }
  busy.value = true
  try {
    await orderAdminApi.ship(shippingOrderNo.value, {
      carrier: shipForm.carrier,
      tracking_no: shipForm.tracking_no,
      idempotency_key: `ship-${shippingOrderNo.value}-${newTraceId()}`,
    })
    notifications.success('发货成功')
    shippingOrderNo.value = ''
    await execute()
  } catch (e) {
    const normalized = normalizeError(e)
    if (normalized.code === 70_003) {
      notifications.warning('该订单已发货', '重复发货已被服务端拒绝')
      await execute()
    } else {
      notifications.error('发货失败', normalized.message, normalized.code, normalized.traceId)
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="c-orders">
    <div class="c-orders__head">
      <h2 class="nx-section-title">订单管理</h2>
      <div class="c-orders__filters">
        <input v-model="keyword" class="c-orders__input" placeholder="订单号 / 收货人" @keydown.enter="page = 1; execute()" />
        <select v-model="statusFilter" class="c-orders__input" @change="page = 1; execute()">
          <option value="">全部状态</option>
          <option value="PENDING_PAYMENT">待付款</option>
          <option value="PROCESSING">处理中</option>
          <option value="COMPLETED">已完成</option>
          <option value="CANCELLED">已取消</option>
          <option value="CLOSED">已关闭</option>
        </select>
        <button type="button" class="nx-btn" @click="page = 1; execute()">查询</button>
      </div>
    </div>

    <StateView :state="status" :error="error" @retry="execute()">
      <table class="c-orders__table">
        <thead>
          <tr>
            <th>订单号</th>
            <th>下单时间</th>
            <th>状态</th>
            <th>支付</th>
            <th>履约</th>
            <th>应付</th>
            <th>已退</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="order in orders" :key="order.id">
            <td><code>{{ order.order_no }}</code></td>
            <td>{{ new Date(order.created_at).toLocaleString() }}</td>
            <td><StatusChip :status="order.status" kind="order" /></td>
            <td><StatusChip :status="order.payment_status" kind="payment" /></td>
            <td><StatusChip :status="order.fulfillment_status" kind="fulfillment" /></td>
            <td class="nx-money">{{ formatMoney(order.snapshot.payable_amount) }}</td>
            <td class="nx-money">{{ formatMoney(order.refunded_amount) }}</td>
            <td>
              <button
                v-if="order.status === 'PROCESSING' && order.fulfillment_status !== 'SHIPPED'"
                type="button"
                class="nx-btn nx-btn--ghost"
                @click="openShip(order.order_no)"
              >
                发货
              </button>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="meta" class="c-orders__pager">
        <button type="button" class="nx-btn" :disabled="page <= 1" @click="page -= 1; execute()">上一页</button>
        <span class="nx-muted">第 {{ meta.page }} / {{ meta.total_pages }} 页 · 共 {{ meta.total }} 条</span>
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

    <div v-if="shippingOrderNo" class="c-orders__modal" role="dialog" aria-modal="true" aria-label="订单发货">
      <div class="c-orders__modal-card nx-card">
        <div class="nx-card__body">
          <h3 class="nx-section-title">发货 · {{ shippingOrderNo }}</h3>
          <label class="c-orders__field">
            <span>承运商</span>
            <input v-model="shipForm.carrier" maxlength="60" placeholder="例如 顺丰速运" />
          </label>
          <label class="c-orders__field">
            <span>运单号</span>
            <input v-model="shipForm.tracking_no" maxlength="80" />
          </label>
          <div class="c-orders__modal-actions">
            <button type="button" class="nx-btn nx-btn--primary" :disabled="busy" @click="submitShip()">
              确认发货
            </button>
            <button type="button" class="nx-btn" :disabled="busy" @click="shippingOrderNo = ''">取消</button>
          </div>
          <p class="nx-muted c-orders__note">
            发货请求带幂等键；重复提交同一运单不会产生第二条履约单。数量校验由服务端在事务内完成。
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.c-orders {
  &__head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }

  &__filters {
    display: flex;
    gap: 8px;
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
      white-space: nowrap;
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
    width: min(420px, 92vw);
  }

  &__field {
    display: flex;
    flex-direction: column;
    gap: 5px;
    margin: 12px 0;
    font-size: 13px;

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
