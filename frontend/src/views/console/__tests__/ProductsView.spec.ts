import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ProductsView from '@/views/console/ProductsView.vue'
import { useNotificationStore } from '@/stores/notification'

const products = vi.fn()
const uploadImage = vi.fn()
vi.mock('@/api', () => ({
  catalogAdminApi: {
    products: (...args: unknown[]) => products(...args),
    uploadImage: (...args: unknown[]) => uploadImage(...args),
  },
}))

describe('merchant product image upload', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setActivePinia(createPinia())
    products.mockResolvedValue({
      items: [{
        id: 42, title: 'Nova Phone', cover_url: null, min_price_amount: 1999,
        original_price_amount: null, sales_count: 0, brand_name: null, status: 'DRAFT',
      }],
      meta: { page: 1, page_size: 20, total: 1, total_pages: 1 },
    })
    uploadImage.mockResolvedValue({ id: 8, url: 'https://example.test/image', role: 'PRIMARY' })
  })

  it('uploads the chosen file as primary and refreshes the product cover', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const wrapper = mount(ProductsView, { global: { plugins: [pinia] } })
    await flushPromises()
    const input = wrapper.get('input[type="file"]')
    const file = new File(['image'], 'front.png', { type: 'image/png' })
    Object.defineProperty(input.element, 'files', { configurable: true, value: [file] })
    await input.trigger('change')
    await flushPromises()
    expect(uploadImage).toHaveBeenCalledWith(42, file, 'PRIMARY')
    expect(products).toHaveBeenCalledTimes(2)
    expect(useNotificationStore().latest?.title).toBe('商品主图已上传')
  })
})
