/**
 * Console · Orders — action wiring, mocked at the API MODULE BOUNDARY.
 *
 * `vi.mock('@/api')` is deliberate: the mock sits exactly where the real network call would
 * happen, so these tests keep working unchanged once the backend modules land. Nothing here
 * reaches into component internals or stubs axios.
 *
 * What this locks down, beyond the pure availability unit tests:
 *   1. The action that renders is the one the frozen state machine allows — an unpaid order shows
 *      取消 and NOT 发货.
 *   2. Clicking an action calls the FROZEN TASK ENDPOINT function (orderApi.cancel,
 *      orderApi.confirmReceipt, fulfillmentAdminApi.ship) — never a status patch.
 *   3. Shipping takes its id from the FULFILLMENT QUEUE (`GET /fulfillments/admin`, §5.2), which
 *      is what the frozen contract provides for discovering an id — the list payload has no
 *      `shipments[]`.
 *   4. A package the server already stamped a carrier on is NOT offered for shipping.
 *   5. A 403 from an action is handled gracefully (an error notice, no crash) — the case where the
 *      UI and the server disagree about permission.
 *
 * FROZEN SHAPES ONLY: `OrderSummary` for list rows and `Fulfillment` for packages. No `snapshot`,
 * no `status`, no string ids.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import OrdersView from '@/views/console/OrdersView.vue'
import { useNotificationStore } from '@/stores/notification'
import type { OrderSummary } from '@/types/domain'
import type { Fulfillment } from '@/types/frozen-contract'

/** The API-module boundary under test. */
const listMock = vi.fn()
const fulfillmentListMock = vi.fn()
const cancelMock = vi.fn()
const confirmReceiptMock = vi.fn()
const shipMock = vi.fn()

vi.mock('@/api', () => ({
  orderAdminApi: {
    list: (...args: unknown[]) => listMock(...args),
  },
  orderApi: {
    cancel: (...args: unknown[]) => cancelMock(...args),
    confirmReceipt: (...args: unknown[]) => confirmReceiptMock(...args),
  },
  fulfillmentAdminApi: {
    list: (...args: unknown[]) => fulfillmentListMock(...args),
    ship: (...args: unknown[]) => shipMock(...args),
  },
}))

/**
 * A list row built ONLY from frozen `OrderSummary` fields (API_CONTRACT.md §6).
 *
 * Note what is absent by design: no `items[]`, no `shipments[]`, no address. The list payload
 * genuinely does not carry them.
 */
function makeOrder(overrides: Partial<OrderSummary> = {}): OrderSummary {
  return {
    id: 456,
    order_no: 'NV20260922000001',
    order_status: 'PENDING_PAYMENT',
    payment_status: 'UNPAID',
    fulfillment_status: 'UNFULFILLED',
    after_sale_status: 'NONE',
    original_amount: 599900,
    promotion_discount_amount: 0,
    coupon_discount_amount: 0,
    shipping_amount: 0,
    payable_amount: 599900,
    paid_amount: 0,
    refunded_amount: 0,
    receiver_name: '张**',
    receiver_phone: '138****5678',
    created_at: '2026-09-22T23:31:07.507Z',
    paid_at: null,
    expires_at: '2026-09-22T23:46:07.507Z',
    // §11 addendum: backend-owned summary fields, so the list can name a product without
    // hydrating every order's basket.
    first_item_name: 'Nova Phone 15 Pro 原色钛金属 256GB',
    item_count: 1,
    ...overrides,
  }
}

/** A package the server has NOT shipped yet: `carrier` and `tracking_no` are null (§5). */
function makeFulfillment(overrides: Partial<Fulfillment> = {}): Fulfillment {
  return {
    id: 77,
    order_id: 456,
    order_no: 'NV20260922000001',
    fulfillment_no: 'NVF20260922000001',
    fulfillment_status: 'UNFULFILLED',
    carrier: null,
    tracking_no: null,
    shipped_at: null,
    delivered_at: null,
    created_at: '2026-09-22T23:31:07.507Z',
    items: [
      {
        id: 1,
        order_item_id: 9,
        sku_id: 3,
        product_name: 'Nova Phone 15 Pro',
        sku_name: '原色钛金属 256GB',
        quantity: 1,
      },
    ],
    ...overrides,
  }
}

function pageOf<T>(items: T[]) {
  return {
    items,
    meta: { page: 1, page_size: 20, total: items.length, total_pages: 1 },
  }
}

/** Mount with a fresh Pinia; the notices store is real so failures are observable. */
function mountView() {
  const pinia = createPinia()
  setActivePinia(pinia)
  return mount(OrdersView, { global: { plugins: [pinia] } })
}

/** All button labels currently rendered, normalized. */
function buttonLabels(wrapper: ReturnType<typeof mountView>): string[] {
  return wrapper.findAll('button').map((button) => button.text().trim())
}

async function findButton(wrapper: ReturnType<typeof mountView>, label: string) {
  return wrapper.findAll('button').find((candidate) => candidate.text().trim() === label)
}

describe('console Orders — row actions follow the frozen state machine', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // Default: nothing outstanding in the fulfillment queue.
    fulfillmentListMock.mockResolvedValue(pageOf<Fulfillment>([]))
  })

  it('an UNPAID order offers 取消 but NOT 发货 / 确认收货', async () => {
    listMock.mockResolvedValue(pageOf([makeOrder()]))
    const wrapper = mountView()
    await flushPromises()

    const labels = buttonLabels(wrapper)
    expect(labels).toContain('取消')
    expect(labels).not.toContain('发货')
    expect(labels).not.toContain('确认收货')
    // The refused actions are explained in a visible slot rather than silently absent.
    expect(wrapper.text()).toContain('不可发货')
  })

  it('a PAID, unshipped order with an outstanding fulfillment offers 发货 and 取消', async () => {
    listMock.mockResolvedValue(
      pageOf([
        makeOrder({
          order_status: 'PROCESSING',
          payment_status: 'PAID',
          paid_amount: 599900,
        }),
      ]),
    )
    fulfillmentListMock.mockResolvedValue(pageOf([makeFulfillment()]))

    const wrapper = mountView()
    await flushPromises()

    const labels = buttonLabels(wrapper)
    expect(labels).toContain('发货')
    expect(labels).toContain('取消')
    expect(labels).not.toContain('确认收货')
  })

  it('a PAID order whose package ALREADY has a carrier is NOT shippable', async () => {
    // THE CARRIER RULE: every status field says "shippable" (PROCESSING / PAID / UNFULFILLED),
    // and only the fulfillment's own `carrier` says otherwise.
    listMock.mockResolvedValue(
      pageOf([
        makeOrder({
          order_status: 'PROCESSING',
          payment_status: 'PAID',
          paid_amount: 599900,
        }),
      ]),
    )
    fulfillmentListMock.mockResolvedValue(
      pageOf([makeFulfillment({ carrier: 'SF', tracking_no: 'SF1' })]),
    )

    const wrapper = mountView()
    await flushPromises()

    expect(buttonLabels(wrapper)).not.toContain('发货')
  })

  it('a SHIPPED order offers 确认收货 and NOT 发货 (order_status is still PROCESSING, §31)', async () => {
    listMock.mockResolvedValue(
      pageOf([
        makeOrder({
          order_status: 'PROCESSING',
          payment_status: 'PAID',
          fulfillment_status: 'SHIPPED',
          paid_amount: 599900,
        }),
      ]),
    )
    const wrapper = mountView()
    await flushPromises()

    const labels = buttonLabels(wrapper)
    expect(labels).toContain('确认收货')
    expect(labels).not.toContain('发货')
    // Cancel is refused after shipping even though order_status is PROCESSING.
    expect(labels).not.toContain('取消')
    expect(wrapper.text()).toContain('不可取消')
  })

  it('names the goods from the §11 summary fields, without an N+1 detail fetch', async () => {
    listMock.mockResolvedValue(
      pageOf([
        makeOrder({
          order_status: 'PROCESSING',
          payment_status: 'PAID',
          first_item_name: 'Nova Phone 15 Pro 原色钛金属 256GB',
          item_count: 3,
        }),
      ]),
    )
    const wrapper = mountView()
    await flushPromises()

    // The addendum exists so the list can name a product; asserting it here means a regression to
    // a blank column fails the suite rather than shipping.
    expect(wrapper.text()).toContain('Nova Phone 15 Pro 原色钛金属 256GB')
    expect(wrapper.text()).toContain('等 3 件')
    // The row needed NO per-row detail fetch to render its goods cell: `orderAdminApi.detail` is
    // not even provided by the module mock above, so an N+1 lookup would throw rather than
    // quietly succeed. One list call for the page.
    expect(listMock).toHaveBeenCalledTimes(1)
  })

  it('omits the quantity suffix for a single-unit order', async () => {
    listMock.mockResolvedValue(pageOf([makeOrder({ first_item_name: '单件商品', item_count: 1 })]))
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('单件商品')
    expect(wrapper.text()).not.toContain('等 1 件')
  })

  it('queries the fulfillment queue for outstanding packages only', async () => {
    listMock.mockResolvedValue(pageOf([makeOrder({ order_status: 'PROCESSING', payment_status: 'PAID' })]))
    const wrapper = mountView()
    await flushPromises()

    // The server's own filter is used rather than fetching everything and filtering locally.
    expect(fulfillmentListMock).toHaveBeenCalledWith(
      expect.objectContaining({ fulfillment_status: 'UNFULFILLED' }),
    )
    expect(wrapper.text()).toContain('未发货')
  })
})

describe('console Orders — actions call the FROZEN task endpoints', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    fulfillmentListMock.mockResolvedValue(pageOf<Fulfillment>([]))
  })

  it('取消 calls orderApi.cancel (POST /orders/{no}/cancel) and refreshes the list', async () => {
    listMock.mockResolvedValue(pageOf([makeOrder()]))
    cancelMock.mockResolvedValue(makeOrder({ order_status: 'CANCELLED' }))

    const wrapper = mountView()
    await flushPromises()
    const before = listMock.mock.calls.length

    const cancelButton = await findButton(wrapper, '取消')
    expect(cancelButton, 'cancel button must exist for an unpaid order').toBeDefined()
    await cancelButton?.trigger('click')
    await flushPromises()

    expect(cancelMock).toHaveBeenCalledTimes(1)
    expect(cancelMock).toHaveBeenCalledWith('NV20260922000001', { reason: '商家取消' })
    // The list is re-read from the server rather than patched locally.
    expect(listMock.mock.calls.length).toBeGreaterThan(before)
  })

  it('确认收货 calls orderApi.confirmReceipt (POST /orders/{no}/confirm-receipt)', async () => {
    listMock.mockResolvedValue(
      pageOf([
        makeOrder({
          order_status: 'PROCESSING',
          payment_status: 'PAID',
          fulfillment_status: 'SHIPPED',
          paid_amount: 599900,
        }),
      ]),
    )
    confirmReceiptMock.mockResolvedValue(makeOrder({ order_status: 'COMPLETED' }))

    const wrapper = mountView()
    await flushPromises()

    const confirmButton = await findButton(wrapper, '确认收货')
    await confirmButton?.trigger('click')
    await flushPromises()

    expect(confirmReceiptMock).toHaveBeenCalledTimes(1)
    expect(confirmReceiptMock).toHaveBeenCalledWith('NV20260922000001')
  })

  it('发货 uses the queue id, then calls fulfillmentAdminApi.ship with EXACTLY three fields', async () => {
    listMock.mockResolvedValue(
      pageOf([
        makeOrder({ order_status: 'PROCESSING', payment_status: 'PAID', paid_amount: 599900 }),
      ]),
    )
    fulfillmentListMock.mockResolvedValue(pageOf([makeFulfillment({ id: 77 })]))
    shipMock.mockResolvedValue(makeFulfillment({ carrier: 'SF', tracking_no: 'SF123456789' }))

    const wrapper = mountView()
    await flushPromises()

    const shipButton = await findButton(wrapper, '发货')
    await shipButton?.trigger('click')
    await flushPromises()

    // The dialog opens against the queue's numeric id.
    expect(wrapper.text()).toContain('77')

    // The carrier is a SELECT (§5: a carrier CODE, not free text), so only the tracking number
    // is typed.
    const carrierSelect = wrapper.find('.o-list__modal select.nx-input')
    expect(carrierSelect.exists()).toBe(true)
    await carrierSelect.setValue('SF')
    const trackingInput = wrapper.find('.o-list__modal input.nx-input')
    await trackingInput.setValue('SF123456789')

    const confirmShip = await findButton(wrapper, '确认发货')
    await confirmShip?.trigger('click')
    await flushPromises()

    expect(shipMock).toHaveBeenCalledTimes(1)
    const [fulfillmentId, payload] = shipMock.mock.calls[0] as [number, Record<string, unknown>]
    // A NUMBER, exactly what `POST /fulfillments/{id}/ship` takes.
    expect(fulfillmentId).toBe(77)
    expect(payload.carrier).toBe('SF')
    expect(payload.tracking_no).toBe('SF123456789')
    // §110: only the fields the endpoint accepts. NO `idempotency_key` — the server rejects it.
    expect(Object.keys(payload).sort()).toEqual(['carrier', 'item_quantities', 'tracking_no'])
  })

  it('does not open the ship dialog when the queue reports nothing outstanding', async () => {
    listMock.mockResolvedValue(
      pageOf([
        makeOrder({ order_status: 'PROCESSING', payment_status: 'PAID', paid_amount: 599900 }),
      ]),
    )
    // Every package already shipped, so the queue is empty for this order.
    fulfillmentListMock.mockResolvedValue(
      pageOf([makeFulfillment({ fulfillment_status: 'SHIPPED', carrier: 'SF', tracking_no: 'SF1' })]),
    )

    const wrapper = mountView()
    await flushPromises()

    // Nothing is offered for this row at all, and no shipment request was attempted.
    expect(buttonLabels(wrapper)).not.toContain('发货')
    expect(shipMock).not.toHaveBeenCalled()
  })
})

describe('console Orders — 403 from an action is handled, not crashed', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    fulfillmentListMock.mockResolvedValue(pageOf<Fulfillment>([]))
  })

  it('shows a permission notice when the server rejects the action with FORBIDDEN', async () => {
    listMock.mockResolvedValue(pageOf([makeOrder()]))
    // This is the shape `normalizeError` produces for a 403 envelope, so the view's
    // `forbidden` branch is what runs.
    cancelMock.mockRejectedValue({
      code: 20_008,
      message: '没有权限执行该操作',
      traceId: 'trace-403',
      httpStatus: 403,
      retryable: false,
      forbidden: true,
      unauthenticated: false,
    })

    const wrapper = mountView()
    await flushPromises()

    // Notices are produced by the one shared store and rendered by Element Plus in the app,
    // so the store is where the assertion belongs — not the component's own DOM.
    const notices = useNotificationStore()
    const errorSpy = vi.spyOn(notices, 'error')

    const cancelButton = await findButton(wrapper, '取消')
    await cancelButton?.trigger('click')
    await flushPromises()

    // The action was attempted, the page survived, and the notice is permission-specific rather
    // than the raw server message.
    expect(cancelMock).toHaveBeenCalledTimes(1)
    expect(errorSpy).toHaveBeenCalledTimes(1)
    const [title, message] = errorSpy.mock.calls[0] as [string, string]
    expect(title).toBe('权限不足')
    expect(message).toContain('服务端拒绝')
    expect(notices.notices[0]?.code).toBe(20_008)
    expect(notices.notices[0]?.traceId).toBe('trace-403')
  })
})

describe('console Orders — §108 states', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    fulfillmentListMock.mockResolvedValue(pageOf<Fulfillment>([]))
  })

  it('renders the Empty state when the server returns no rows', async () => {
    listMock.mockResolvedValue(pageOf([]))
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('暂无数据')
  })

  it('renders the Error state with a retry affordance when the list call fails', async () => {
    listMock.mockRejectedValue({
      code: 50_003,
      message: '订单不存在',
      traceId: 'trace-1',
      httpStatus: 404,
      retryable: false,
      forbidden: false,
      unauthenticated: false,
    })
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('加载失败')
    expect(buttonLabels(wrapper)).toContain('重试')
  })

  it('renders the PermissionDenied state (not a generic error) for a forbidden list call', async () => {
    listMock.mockRejectedValue({
      code: 20_009,
      message: '当前角色权限不足',
      traceId: 'trace-2',
      httpStatus: 403,
      retryable: false,
      forbidden: true,
      unauthenticated: false,
    })
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.text()).toContain('没有访问权限')
  })
})
