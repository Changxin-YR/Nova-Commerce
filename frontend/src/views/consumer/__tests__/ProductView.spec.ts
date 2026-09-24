import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ProductView from '@/views/consumer/ProductView.vue'
import { useCartStore } from '@/stores/cart'

const product = vi.fn()
const navigate = vi.fn()
vi.mock('vue-router', () => ({
  useRoute: () => ({ params: { id: '7' } }),
  useRouter: () => ({ push: navigate }),
}))
vi.mock('@/api', () => ({ catalogApi: { product: (...args: unknown[]) => product(...args) } }))

describe('product detail uses numeric catalog SKU IDs', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
    product.mockResolvedValue({
      id: 7, title: 'Phone', min_price_amount: 1000, max_price_amount: 1000,
      images: [], created_at: '2026-09-24T00:00:00Z', status: 'PUBLISHED',
      skus: [{ id: 42, product_id: 7, sku_code: 'P-42', specs: { color: 'Black' },
        price_amount: 1000, available_stock: 5, status: 'ACTIVE' }],
    })
  })

  it('selects and stores a published SKU from the API response', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(ProductView, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(product).toHaveBeenCalledWith('7')
    expect(wrapper.find('.pdetail__sku--active').exists()).toBe(true)
    const add = wrapper.findAll('button').find((button) => button.text().includes('加入购物车'))
    await add?.trigger('click')
    await flushPromises()
    expect(useCartStore().selectedItems).toMatchObject([{
      product_id: '7', sku_id: '42', quantity: 1, product_title: 'Phone',
    }])
  })
})
