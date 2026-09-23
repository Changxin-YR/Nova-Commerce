/**
 * Console · Orders — action wiring, mocked at the API MODULE BOUNDARY.
 *
 * `vi.mock('@/api')` is deliberate: the mock sits exactly where the real network call would
 * happen, so these tests keep working unchanged once the backend modules land. Nothing here
 * reaches into component internals or stubs axios.
 *
 * What this locks down, beyond the pure availability unit tests:
 *   1. The action that renders is the one the frozen state machine allows — an unpaid order
 *      shows 取消 and NOT 发货.
 *   2. Clicking an action calls the FROZEN TASK ENDPOINT function (orderApi.cancel,
 *      orderApi.confirmReceipt, fulfillmentAdminApi.ship) — never a status patch.
 *   3. Shipping resolves a fulfillment id first, because the endpoint is fulfillment-keyed.
 *   4. A 403 from an action is handled gracefully (an error notice, no crash) — the case
 *      where the UI and the server disagree about permission.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import OrdersView from '@/views/console/OrdersView.vue'
import { useNotificationStore } from '@/stores/notification'
import type { Order } from '@/types/domain'

/** The API-module boundary under test. */
const listMock = vi.fn()
const detailMock = vi.fn()
const cancelMock = vi.fn()
const confirmReceiptMock = vi.fn()
const shipMock = vi.fn()

vi.mock('@/api', () => ({
  orderAdminApi: {
    list: (...args: unknown[]) => listMock(...args),
    detail: (...args: unknown[]) => detailMock(...args),
  },
  orderApi: {
    cancel: (...args: unknown[]) => cancelMock(...args),
    confirmReceipt: (...args: unknown[]) => confirmReceiptMock(...args),
  },
  fulfillmentAdminApi: {
    ship: (...args: unknown[]) => shipMock(...args),
  },
}))

/** A realistic order fixture built only from frozen fields. */
function makeOrder(overrides: Partial<Order> = {}): Order {
  return {
    id: 'ord-1',
    order_no: 'NOVA20250101001',
    user_id: 'user-1',
    status: 'PENDING_PAYMENT',
    payment_status: 'UNPAID',
    fulfillment_status: 'UNFULFILLED',
    after_sale_status: 'NONE',
    snapshot: {
      receiver_name: '张三',
      receiver_phone: '13800000000',
      full_address: '北京市朝阳区某路 1 号',
      items: [
        {
          id: 'item-1',
          product_id: 'prod-1',
          sku_id: 'sku-1',
          product_title: '轻薄本 14 英寸',
          sku_specs: { 颜色: '深空灰' },
          quantity: 1,
          unit_price_amount: 599900,
          subtotal_amount: 599900,
          refunded_amount: 0,
        },
      ],
      items_amount: 599900,
      discount_amount: 0,
      shipping_amount: 0,
      payable_amount: 599900,
    },
    shipments: [],
    paid_amount: 0,
    refunded_amount: 0,
    refundable_amount: 0,
    created_at: '2025-01-01T10:00:00Z',
    ...overrides,
  }
}

function pageOf(items: Order[]) {
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
  const button = wrapper.findAll('button').find((candidate) => candidate.text().trim() === label)
  return button
}

describe('console Orders — row actions follow the frozen state machine', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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

  it('a PAID, unshipped order offers 发货 and 取消', async () => {
    listMock.mockResolvedValue(
      pageOf([
        makeOrder({
          status: 'PROCESSING',
          payment_status: 'PAID',
          refundable_amount: 599900,
        }),
      ]),
    )
    const wrapper = mountView()
    await flushPromises()

    const labels = buttonLabels(wrapper)
    expect(labels).toContain('发货')
    expect(labels).toContain('取消')
    expect(labels).not.toContain('确认收货')
  })

  it('a SHIPPED order offers 确认收货 and NOT 发货 (status is still PROCESSING, §31)', async () => {
    listMock.mockResolvedValue(
      pageOf([
        makeOrder({
          status: 'PROCESSING',
          payment_status: 'PAID',
          fulfillment_status: 'SHIPPED',
          refundable_amount: 599900,
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
})

describe('console Orders — actions call the FROZEN task endpoints', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('取消 calls orderApi.cancel (POST /orders/{no}/cancel) and refreshes the list', async () => {
    listMock.mockResolvedValue(pageOf([makeOrder()]))
    cancelMock.mockResolvedValue(makeOrder({ status: 'CANCELLED' }))

    const wrapper = mountView()
    await flushPromises()
    const before = listMock.mock.calls.length

    const cancelButton = await findButton(wrapper, '取消')
    expect(cancelButton, 'cancel button must exist for an unpaid order').toBeDefined()
    await cancelButton?.trigger('click')
    await flushPromises()

    expect(cancelMock).toHaveBeenCalledTimes(1)
    expect(cancelMock).toHaveBeenCalledWith('NOVA20250101001', { reason: '商家取消' })
    // The list is re-read from the server rather than patched locally.
    expect(listMock.mock.calls.length).toBeGreaterThan(before)
  })

  it('确认收货 calls orderApi.confirmReceipt (POST /orders/{no}/confirm-receipt)', async () => {
    listMock.mockResolvedValue(
      pageOf([
        makeOrder({
          status: 'PROCESSING',
          payment_status: 'PAID',
          fulfillment_status: 'SHIPPED',
          refundable_amount: 599900,
        }),
      ]),
    )
    confirmReceiptMock.mockResolvedValue(makeOrder({ status: 'COMPLETED' }))

    const wrapper = mountView()
    await flushPromises()

    const confirmButton = await findButton(wrapper, '确认收货')
    await confirmButton?.trigger('click')
    await flushPromises()

    expect(confirmReceiptMock).toHaveBeenCalledTimes(1)
    expect(confirmReceiptMock).toHaveBeenCalledWith('NOVA20250101001')
  })

  it('发货 resolves a fulfillment id first, then calls fulfillmentAdminApi.ship and NOT the order endpoint', async () => {
    listMock.mockResolvedValue(
      pageOf([makeOrder({ status: 'PROCESSING', payment_status: 'PAID', refundable_amount: 599900 })]),
    )
    // The list payload has no fulfillment ids, so the view must fetch detail to get one.
    detailMock.mockResolvedValue(
      makeOrder({
        status: 'PROCESSING',
        payment_status: 'PAID',
        shipments: [
          {
            id: 'ful-77',
            order_no: 'NOVA20250101001',
            carrier: '',
            tracking_no: '',
            fulfillment_status: 'UNFULFILLED',
            items: [],
          },
        ],
      }),
    )
    shipMock.mockResolvedValue({ id: 'ful-77', fulfillment_status: 'SHIPPED' })

    const wrapper = mountView()
    await flushPromises()

    const shipButton = await findButton(wrapper, '发货')
    await shipButton?.trigger('click')
    await flushPromises()

    // The dialog opens against the resolved fulfillment.
    expect(detailMock).toHaveBeenCalledWith('NOVA20250101001')
    expect(wrapper.text()).toContain('ful-77')

    // Fill the dialog's own inputs. Scoped to the modal on purpose: the filter bar also
    // renders `input.nx-input`, so an unscoped selector would fill the wrong fields.
    const modalInputs = wrapper.findAll('.o-list__modal input.nx-input')
    expect(modalInputs).toHaveLength(2)
    await modalInputs[0]?.setValue('顺丰速运')
    await modalInputs[1]?.setValue('SF123456789')

    const confirmShip = await findButton(wrapper, '确认发货')
    await confirmShip?.trigger('click')
    await flushPromises()

    expect(shipMock).toHaveBeenCalledTimes(1)
    const [fulfillmentId, payload] = shipMock.mock.calls[0] as [string, Record<string, string>]
    expect(fulfillmentId).toBe('ful-77')
    expect(payload.carrier).toBe('顺丰速运')
    expect(payload.tracking_no).toBe('SF123456789')
    // Idempotency key present, and scoped to this fulfillment.
    expect(payload.idempotency_key).toContain('ful-77')
    // §110: only the fields the endpoint accepts are sent — no whole-object round trip.
    expect(Object.keys(payload).sort()).toEqual(['carrier', 'idempotency_key', 'tracking_no'])
  })

  it('does not open the ship dialog when the order has no unshipped fulfillment', async () => {
    listMock.mockResolvedValue(
      pageOf([makeOrder({ status: 'PROCESSING', payment_status: 'PAID', refundable_amount: 599900 })]),
    )
    detailMock.mockResolvedValue(
      makeOrder({
        status: 'PROCESSING',
        payment_status: 'PAID',
        // Already shipped: nothing left to fulfil.
        shipments: [
          {
            id: 'ful-1',
            order_no: 'NOVA20250101001',
            carrier: '顺丰',
            tracking_no: 'SF1',
            fulfillment_status: 'SHIPPED',
            items: [],
          },
        ],
      }),
    )

    const wrapper = mountView()
    await flushPromises()

    const shipButton = await findButton(wrapper, '发货')
    await shipButton?.trigger('click')
    await flushPromises()

    // No dialog, and no shipment request was attempted.
    expect(wrapper.text()).not.toContain('确认发货')
    expect(shipMock).not.toHaveBeenCalled()
  })
})

describe('console Orders — 403 from an action is handled, not crashed', () => {
  beforeEach(() => {
    vi.clearAllMocks()
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

    // The action was attempted, the page survived, and the notice is permission-specific
    // rather than the raw server message.
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
