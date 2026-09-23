<script setup lang="ts">
/**
 * Console · Orders — the merchant order desk.
 *
 * DENSE OPERATIONS LAYOUT: filter bar → hairline table → pager. 12px type, tabular figures,
 * every number right-aligned, statuses as bordered labels.
 *
 * ACTION POLICY (§99)
 *  Row actions come from `src/domain/orders/availability.ts`, never from inline conditions, so
 *  the UI cannot offer an operation the frozen state machine forbids. Every action is a TASK
 *  endpoint (`/cancel`, `/confirm-receipt`, `/fulfillments/{id}/ship`) — there is no generic
 *  `PATCH { status }` anywhere in this file.
 *
 * WHY SHIPPING RESOLVES A FULFILLMENT FIRST
 *  The frozen ship endpoint is `POST /fulfillments/{id}/ship` (PROJECT_BASELINE.yaml
 *  `task_endpoints`): it is keyed by FULFILLMENT id, not order number. The list response has no
 *  fulfillment ids, so the ship button fetches the order detail, picks the first unshipped
 *  fulfillment, and only then opens the form. If none exists the action stays unavailable
 *  rather than rendering a button that could only fail.
 *
 * §104 / §110 CONDUCT
 *  Permission checks here decide only what is OFFERED. Every mutation can still come back
 *  FORBIDDEN (20008) / INSUFFICIENT_PERMISSION (20009) or a state conflict (50 004 / 50 010 /
 *  50 011 / 70 002 / 70 003), and each of those paths is handled explicitly below rather than
 *  being allowed to crash or silently swallow.
 */
import { computed, reactive, ref } from 'vue'
import { fulfillmentAdminApi, orderAdminApi, orderApi } from '@/api'
import { useAsyncState } from '@/composables/useAsyncState'
import { buildOrderListParams, type OrderListFilters } from '@/domain/listParams'
import { orderActionBlockedReason, orderActionFlags } from '@/domain/orders/availability'
import { useNotificationStore } from '@/stores/notification'
import { normalizeError } from '@/api/error'
import StateView from '@/components/ui/StateView.vue'
import StatusChip from '@/components/ui/StatusChip.vue'
import PriceText from '@/components/ui/PriceText.vue'
import type { OrderSummary } from '@/types/domain'
import type { Fulfillment } from '@/types/frozen-contract'

const notifications = useNotificationStore()

/** Filter draft lives in this page — NOT in a store (§105). */
const filters = reactive<OrderListFilters>({
  orderNo: '',
  status: '',
  paymentStatus: '',
  fulfillmentStatus: '',
  startDate: '',
  endDate: '',
  page: 1,
  page_size: 20,
})

/**
 * Fulfillments still outstanding, keyed by order number.
 *
 * WHY THIS REPLACED THE PER-ROW DETAIL LOOKUP:
 *  - `OrderSummary` (the list payload) carries NO `items[]`/`shipments[]`. The contract splits list
 *    rows from detail precisely so a 20-row page does not drag every order's line items across the
 *    wire (§6), so the list cannot tell us a fulfillment id.
 *  - The old code answered that by calling `orderAdminApi.detail()` per row — N extra round trips
 *    before the operator could click anything — and it picked the package by
 *    `fulfillment_status === 'UNFULFILLED'` rather than by the carrier.
 *  - `GET /fulfillments/admin` (§5.2) is the frozen answer: one call returns every outstanding
 *    package with its `order_no`, so it is also exactly the data the server uses to decide
 *    shippability — including the `carrier` that says whether it has shipped.
 *
 * An order ABSENT from this map has nothing outstanding. That is a real state (fully shipped) and
 * must read as "nothing to ship", not as a broken button.
 */
const unshippedByOrderNo = ref<Record<string, Fulfillment>>({})

async function loadUnshippedFulfillments(): Promise<void> {
  const page = await fulfillmentAdminApi.list({ fulfillment_status: 'UNFULFILLED', page_size: 100 })
  const map: Record<string, Fulfillment> = {}
  for (const item of page.items) {
    // First outstanding package per order wins; shipping is per-package, not per-order.
    if (!map[item.order_no]) map[item.order_no] = item
  }
  unshippedByOrderNo.value = map
}

const { data: pageData, status, error, execute } = useAsyncState(
  async () => {
    const [page] = await Promise.all([
      orderAdminApi.list(buildOrderListParams(filters)),
      loadUnshippedFulfillments(),
    ])
    return page
  },
  { immediate: true },
)

const orders = computed(() => pageData.value?.items ?? [])
const meta = computed(() => pageData.value?.meta ?? null)

/** Order number currently mid-mutation (disables just that row). */
const busyOrderNo = ref('')

/* -- filter bar actions ---------------------------------------------------- */

/** Any filter change RESETS to page 1, otherwise the operator lands on an empty page 3. */
function applyFilters(): void {
  filters.page = 1
  void execute()
}

function resetFilters(): void {
  filters.orderNo = ''
  filters.status = ''
  filters.paymentStatus = ''
  filters.fulfillmentStatus = ''
  filters.startDate = ''
  filters.endDate = ''
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

/* -- per-row action availability ------------------------------------------ */

/**
 * Row actions, computed from the order's four status fields AND the fulfillment's own `carrier`.
 *
 * The carrier is what decides "already shipped": it is `null` until the server ships the package
 * (§5), so one that already carries a tracking number can never be offered a second time — the
 * server would reject it with `FULFILLMENT_ALREADY_SHIPPED` (70 003).
 */
function flagsFor(order: OrderSummary) {
  return orderActionFlags(order, shipFulfillmentFor(order.order_no) ?? null)
}

/** The one outstanding fulfillment for an order, when the server reports any. */
function shipFulfillmentFor(orderNo: string): Fulfillment | undefined {
  return unshippedByOrderNo.value[orderNo]
}

function blockedReason(
  action: 'cancel' | 'confirmReceipt' | 'ship' | 'refund',
  order: OrderSummary,
): string {
  return orderActionBlockedReason(action, order, shipFulfillmentFor(order.order_no) ?? null)
}

/**
 * Central error handler for every task action.
 * The 403 branch is the interesting one: it means the UI and the server disagreed about
 * permission, which is a real case (a role revoked mid-session) and must not crash the page.
 */
function reportActionFailure(title: string, e: unknown): void {
  const normalized = normalizeError(e)
  if (normalized.forbidden) {
    notifications.error(
      '权限不足',
      '服务端拒绝了该操作：界面权限与服务端不一致，请刷新后重试或联系管理员。',
      normalized.code,
      normalized.traceId,
    )
    // Re-read: the server is the authority on what this session may see.
    void execute()
    return
  }
  notifications.error(title, normalized.message, normalized.code, normalized.traceId)
}

/* -- task actions ---------------------------------------------------------- */

async function cancelOrder(orderNo: string): Promise<void> {
  busyOrderNo.value = orderNo
  try {
    await orderApi.cancel(orderNo, { reason: '商家取消' })
    notifications.success('订单已取消')
    await execute()
  } catch (e) {
    reportActionFailure('取消失败', e)
  } finally {
    busyOrderNo.value = ''
  }
}

async function confirmReceipt(orderNo: string): Promise<void> {
  busyOrderNo.value = orderNo
  try {
    await orderApi.confirmReceipt(orderNo)
    notifications.success('已确认收货')
    await execute()
  } catch (e) {
    reportActionFailure('确认收货失败', e)
  } finally {
    busyOrderNo.value = ''
  }
}

/* -- ship dialog ----------------------------------------------------------- */
const shipTarget = ref<{ orderNo: string; fulfillmentId: number } | null>(null)
const shipForm = reactive({ carrier: '', tracking_no: '' })

/**
 * Carrier CODES, not free text (§5). The contract is explicit that `carrier` is a code such as
 * `"SF"`, so a free-text box would let an operator type anything and the server would store a
 * value no downstream system recognises.
 */
const CARRIERS = [
  { code: 'SF', label: '顺丰速运' },
  { code: 'JD', label: '京东物流' },
  { code: 'YTO', label: '圆通速递' },
  { code: 'ZTO', label: '中通快递' },
  { code: 'STO', label: '申通快递' },
  { code: 'YD', label: '韵达速递' },
  { code: 'EMS', label: '中国邮政' },
] as const

function openShip(order: OrderSummary): void {
  const fulfillment = shipFulfillmentFor(order.order_no)
  if (!fulfillment) {
    // The server reports nothing outstanding for this order.
    notifications.warning('没有待发货的履约单', '该订单可能已全部发货，请刷新后确认。')
    return
  }
  // The id comes straight from the queue: a NUMBER, exactly what the task endpoint takes.
  shipTarget.value = { orderNo: order.order_no, fulfillmentId: fulfillment.id }
  shipForm.carrier = ''
  shipForm.tracking_no = ''
}

async function submitShip(): Promise<void> {
  const target = shipTarget.value
  if (!target) return
  if (!shipForm.carrier) {
    notifications.warning('请选择承运商')
    return
  }
  if (!shipForm.tracking_no.trim()) {
    notifications.warning('请填写运单号')
    return
  }
  busyOrderNo.value = target.orderNo
  try {
    /**
     * EXACTLY three fields (§5, §110 mass-assignment guard). There is deliberately no
     * `idempotency_key`: the server does not accept one, and the guard against shipping the same
     * package twice is the fulfillment's own state — a retry answers
     * `FULFILLMENT_ALREADY_SHIPPED` (70 003) rather than creating a second shipment.
     */
    await fulfillmentAdminApi.ship(target.fulfillmentId, {
      carrier: shipForm.carrier,
      tracking_no: shipForm.tracking_no.trim(),
      item_quantities: [],
    })
    notifications.success('发货成功')
    shipTarget.value = null
    await execute()
  } catch (e) {
    reportActionFailure('发货失败', e)
  } finally {
    busyOrderNo.value = ''
  }
}

/** Compact timestamp for a dense table cell. */
function stamp(iso: string): string {
  const date = new Date(iso)
  const pad = (n: number): string => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
</script>

<template>
  <div class="o-list">
    <div class="nx-block">
      <!-- filter bar ------------------------------------------------------ -->
      <div class="nx-filterbar">
        <label>
          订单号
          <input
            v-model="filters.orderNo"
            class="nx-input"
            placeholder="支持前缀匹配"
            @keydown.enter="applyFilters()"
          />
        </label>
        <label>
          订单状态
          <select v-model="filters.status" class="nx-input">
            <option value="">全部</option>
            <option value="PENDING_PAYMENT">待付款</option>
            <option value="PROCESSING">处理中</option>
            <option value="COMPLETED">已完成</option>
            <option value="CANCELLED">已取消</option>
            <option value="CLOSED">已关闭</option>
          </select>
        </label>
        <label>
          支付状态
          <select v-model="filters.paymentStatus" class="nx-input">
            <option value="">全部</option>
            <option value="UNPAID">未支付</option>
            <option value="PAYING">支付中</option>
            <option value="PAID">已支付</option>
            <option value="PARTIAL_REFUNDED">部分退款</option>
            <option value="REFUNDED">已退款</option>
          </select>
        </label>
        <label>
          履约状态
          <select v-model="filters.fulfillmentStatus" class="nx-input">
            <option value="">全部</option>
            <option value="UNFULFILLED">未发货</option>
            <option value="PARTIAL_SHIPPED">部分发货</option>
            <option value="SHIPPED">已发货</option>
            <option value="DELIVERED">已签收</option>
          </select>
        </label>
        <label>
          下单起
          <input v-model="filters.startDate" type="date" class="nx-input" />
        </label>
        <label>
          止
          <input v-model="filters.endDate" type="date" class="nx-input" />
        </label>
        <button type="button" class="nx-btn nx-btn--primary nx-btn--sm" @click="applyFilters()">
          查询
        </button>
        <button type="button" class="nx-btn nx-btn--sm" @click="resetFilters()">重置</button>
        <span class="nx-filterbar__spacer nx-muted">
          {{ meta ? `共 ${meta.total} 条` : '' }}
        </span>
      </div>

      <StateView :state="status" :error="error" @retry="execute()">
        <table class="nx-table">
          <thead>
            <tr>
              <th style="width: 170px">订单号</th>
              <th style="width: 130px">下单时间</th>
              <th style="width: 150px">商品</th>
              <th style="width: 90px">订单状态</th>
              <th style="width: 100px">支付</th>
              <th style="width: 100px">履约</th>
              <th style="width: 90px; text-align: right">应付</th>
              <th style="width: 90px; text-align: right">已退</th>
              <th style="width: 210px">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="order in orders" :key="order.id">
              <td><code class="o-list__no">{{ order.order_no }}</code></td>
              <td>{{ stamp(order.created_at) }}</td>
              <td>
                <!--
                  `OrderSummary` carries NO line items (§6) — that is the whole point of the
                  list/detail split — so the goods cell cannot name a product here without an N+1
                  detail fetch per row. It shows the order number instead of pretending.
                -->
                <span class="o-list__goods" :title="`订单 ${order.order_no}`">
                  <em class="nx-muted">{{ order.order_no }}</em>
                </span>
              </td>
              <td><StatusChip :status="order.order_status" kind="order" dot /></td>
              <td><StatusChip :status="order.payment_status" kind="payment" /></td>
              <td><StatusChip :status="order.fulfillment_status" kind="fulfillment" /></td>
              <td style="text-align: right">
                <PriceText :amount="order.payable_amount" size="sm" :grouping="false" />
              </td>
              <td style="text-align: right">
                <PriceText :amount="order.refunded_amount" size="sm" muted :grouping="false" />
              </td>
              <td>
                <div class="o-list__actions">
                  <!--
                    Gated by the tested availability logic. The disabled/hint branch explains WHY
                    rather than silently hiding the action (§99: no entry point for a state that
                    forbids it, and no dead button either).
                  -->
                  <button
                    v-if="flagsFor(order).ship"
                    type="button"
                    class="nx-btn nx-btn--text"
                    :disabled="busyOrderNo === order.order_no"
                    @click="openShip(order)"
                  >
                    发货
                  </button>
                  <button
                    v-if="flagsFor(order).confirmReceipt"
                    type="button"
                    class="nx-btn nx-btn--text"
                    :disabled="busyOrderNo === order.order_no"
                    @click="confirmReceipt(order.order_no)"
                  >
                    确认收货
                  </button>
                  <button
                    v-if="flagsFor(order).cancel"
                    type="button"
                    class="nx-btn nx-btn--text"
                    :disabled="busyOrderNo === order.order_no"
                    @click="cancelOrder(order.order_no)"
                  >
                    取消
                  </button>

                  <span
                    v-if="!flagsFor(order).ship && !flagsFor(order).confirmReceipt"
                    class="o-list__hint"
                    :title="blockedReason('ship', order)"
                  >
                    不可发货
                  </span>
                  <span
                    v-if="!flagsFor(order).cancel"
                    class="o-list__hint"
                    :title="blockedReason('cancel', order)"
                  >
                    不可取消
                  </span>
                </div>
              </td>
            </tr>
          </tbody>
        </table>

        <!-- pager --------------------------------------------------------- -->
        <div v-if="meta" class="o-list__pager">
          <button type="button" class="nx-btn nx-btn--sm" :disabled="filters.page <= 1" @click="changePage(-1)">
            上一页
          </button>
          <span class="nx-muted">
            第 {{ meta.page }} / {{ meta.total_pages }} 页 · 共 {{ meta.total }} 条
          </span>
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

    <!-- ship dialog: only the fields the endpoint accepts (§110 mass-assignment guard) -->
    <div v-if="shipTarget" class="o-list__modal" role="dialog" aria-modal="true" aria-label="订单发货">
      <div class="nx-block o-list__modal-card">
        <div class="nx-block__head">
          <h2 class="nx-block__title">发货 · {{ shipTarget.orderNo }}</h2>
        </div>
        <div class="nx-block__body">
          <p class="nx-muted o-list__modal-hint">
            提交到履约单 <code>{{ shipTarget.fulfillmentId }}</code>
          </p>
          <label class="o-list__field">
            <span>承运商</span>
            <select v-model="shipForm.carrier" class="nx-input">
              <option value="">请选择</option>
              <option v-for="c in CARRIERS" :key="c.code" :value="c.code">
                {{ c.label }}（{{ c.code }}）
              </option>
            </select>
          </label>
          <label class="o-list__field">
            <span>运单号</span>
            <input v-model="shipForm.tracking_no" class="nx-input" maxlength="80" />
          </label>
          <div class="o-list__modal-actions">
            <button
              type="button"
              class="nx-btn nx-btn--primary"
              :disabled="busyOrderNo === shipTarget.orderNo"
              @click="submitShip()"
            >
              确认发货
            </button>
            <button type="button" class="nx-btn" @click="shipTarget = null">取消</button>
          </div>
          <p class="nx-muted o-list__modal-note">
            请求仅含承运商、运单号与明细数量（§110）。重复提交不会产生第二张履约单：服务端以履约单自身状态为准，已发货会返回 70003。
          </p>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.o-list {
  &__no {
    font-size: 12px;
    font-variant-numeric: tabular-nums;
  }

  &__goods {
    display: inline-block;
    max-width: 140px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    vertical-align: middle;

    em {
      font-style: normal;
    }
  }

  &__actions {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  /* A disabled action keeps a visible, explained slot instead of vanishing. */
  &__hint {
    color: var(--nx-text-muted);
    font-size: 12px;
    cursor: help;
    border-bottom: 1px dotted var(--nx-border-strong);
  }

  &__pager {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 12px;
    border-top: 1px solid var(--nx-border);
    font-size: 12px;
  }

  &__modal {
    position: fixed;
    inset: 0;
    z-index: 2000;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(0, 0, 0, 0.45);
  }

  &__modal-card {
    width: min(420px, 92vw);
  }

  &__modal-hint code,
  &__modal-note code {
    font-size: 12px;
  }

  &__field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin: 10px 0;
    font-size: 12px;
  }

  &__modal-actions {
    display: flex;
    gap: 8px;
    margin-top: 4px;
  }

  &__modal-note {
    margin: 12px 0 0;
    font-size: 12px;
    line-height: 1.6;
  }
}
</style>
