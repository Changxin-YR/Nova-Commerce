import { beforeEach, describe, expect, it } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'
import { useCartStore } from '@/stores/cart'
import { useAuthStore } from '@/stores/auth'
import { API } from '@/api/endpoints'

function stores() {
  setActivePinia(createPinia())
  return { cart: useCartStore(), auth: useAuthStore() }
}

describe('local cart selection (§14.1)', () => {
  beforeEach(() => localStorage.clear())

  it('persists SKU and quantity without a cached price', async () => {
    const { cart } = stores()
    await cart.addItem('7', '42', 2, { product_title: 'Phone', sku_name: 'Black' })
    await cart.addItem('7', '42', 1)
    expect(cart.selectedItems).toHaveLength(1)
    expect(cart.selectedItems[0]?.quantity).toBe(3)

    const stored = localStorage.getItem('nova:cart:v1:guest') ?? ''
    expect(stored).not.toContain('price')
    const restored = stores().cart
    await restored.load()
    expect(restored.selectedItems).toMatchObject([{ sku_id: '42', quantity: 3 }])
  })

  it('isolates signed-in users and discards malformed browser rows', async () => {
    const first = stores()
    first.auth.user = { id: 1, username: 'one', display_name: 'One', roles: [] }
    await first.cart.addItem('7', '42')

    localStorage.setItem('nova:cart:v1:2', JSON.stringify([
      { product_id: '7', sku_id: '42', quantity: -1, selected: true },
    ]))
    const second = stores()
    second.auth.user = { id: 2, username: 'two', display_name: 'Two', roles: [] }
    await second.cart.load()
    expect(second.cart.items).toEqual([])
    expect(first.cart.items).toHaveLength(1)
    first.auth.user = { id: 2, username: 'two', display_name: 'Two', roles: [] }
    await nextTick()
    expect(first.cart.items).toEqual([])
  })

  it('routes preview and create to the frozen order API', () => {
    expect(API.orders.preview).toBe('/orders/preview')
    expect(API.orders.create).toBe('/orders')
  })
})
