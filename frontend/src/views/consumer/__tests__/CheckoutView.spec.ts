import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import CheckoutView from '@/views/consumer/CheckoutView.vue'
import { useCartStore } from '@/stores/cart'

const listAddresses = vi.fn()
const myCoupons = vi.fn()
const previewOrder = vi.fn()
const createOrder = vi.fn()
const createPayment = vi.fn()
const navigate = vi.fn()

vi.mock('vue-router', () => ({ useRouter: () => ({ push: navigate }) }))
vi.mock('@/api', () => ({
  addressApi: { list: (...args: unknown[]) => listAddresses(...args) },
  marketingApi: { myCoupons: (...args: unknown[]) => myCoupons(...args) },
  orderApi: {
    preview: (...args: unknown[]) => previewOrder(...args),
    create: (...args: unknown[]) => createOrder(...args),
  },
  paymentApi: { create: (...args: unknown[]) => createPayment(...args) },
}))

async function mountView() {
  const pinia = createPinia()
  setActivePinia(pinia)
  await useCartStore().addItem('7', '42', 2)
  return mount(CheckoutView, { global: { plugins: [pinia], stubs: ['RouterLink'] } })
}

describe('checkout sends server-owned order inputs', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    listAddresses.mockResolvedValue([{ id: '3', is_default: true, receiver_name: 'Test', receiver_phone: '13800000000' }])
    myCoupons.mockResolvedValue([{
      id: 55, template_id: 8, merchant_id: 1, status: 'UNUSED',
      valid_from: '2020-01-01T00:00:00Z', valid_to: '2099-01-01T00:00:00Z',
      order_id: null, locked_at: null, used_at: null,
    }])
    previewOrder.mockResolvedValue({
      items: [{
        sku_id: 42, product_id: 7, product_name: 'Test', sku_name: 'Standard', image_url: null,
        unit_price: 500, quantity: 2, original_amount: 1000, promotion_discount_amount: 0,
        coupon_discount_amount: 100, allocated_discount_amount: 100, payable_amount: 900,
      }],
      original_amount: 1000, promotion_discount_amount: 0, coupon_discount_amount: 100,
      shipping_amount: 0, payable_amount: 900, warnings: [],
    })
    createOrder.mockResolvedValue({ order_no: 'N-1' })
    createPayment.mockResolvedValue({ id: '11' })
  })

  it('previews selected SKU quantities with numeric address and coupon IDs', async () => {
    const wrapper = await mountView()
    await flushPromises()
    expect(previewOrder).toHaveBeenCalledWith({
      items: [{ sku_id: 42, quantity: 2 }], address_id: 3, coupon_id: undefined,
    })

    await wrapper.find('select.checkout__coupon').setValue('55')
    await flushPromises()
    expect(previewOrder).toHaveBeenLastCalledWith({
      items: [{ sku_id: 42, quantity: 2 }], address_id: 3, coupon_id: 55,
    })
    expect(wrapper.text()).toContain('应付总额')
  })

  it('creates an order with the same business inputs and one idempotency value', async () => {
    const wrapper = await mountView()
    await flushPromises()
    await wrapper.find('select.checkout__coupon').setValue('55')
    await flushPromises()
    await wrapper.find('.checkout__submit').trigger('click')
    await flushPromises()

    expect(createOrder).toHaveBeenCalledTimes(1)
    expect(createOrder).toHaveBeenCalledWith(expect.objectContaining({
      items: [{ sku_id: 42, quantity: 2 }], address_id: 3, coupon_id: 55,
      client_request_id: expect.stringMatching(/^order-/),
    }))
    expect(createPayment).toHaveBeenCalledWith(expect.objectContaining({ order_no: 'N-1', channel: 'MOCK' }))
    expect(navigate).toHaveBeenCalledWith({ name: 'mock-pay', params: { paymentId: '11' } })
  })
})
