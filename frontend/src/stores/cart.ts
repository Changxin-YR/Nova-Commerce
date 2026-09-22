/**
 * Cart store (§105) — one of the only six allowed Pinia stores.
 *
 * The cart is SERVER state. This store is a cache of the last server response plus
 * a `revision` counter, so two tabs and the product page agree. It never computes
 * prices: `selected_amount` and every `subtotal_amount` come from the server, and
 * the store must not "fix up" a number the backend produced.
 */

import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { cartApi } from '@/api'
import type { Cart } from '@/types/domain'
import type { NormalizedApiError } from '@/types/api'

export const useCartStore = defineStore('cart', () => {
  const cart = ref<Cart | null>(null)
  const loading = ref(false)
  const mutating = ref(false)
  const error = ref<NormalizedApiError | null>(null)

  const items = computed(() => cart.value?.items ?? [])
  const itemCount = computed(() => cart.value?.item_count ?? 0)
  /** Integer minor units, selected items only, as reported by the server. */
  const selectedAmount = computed(() => cart.value?.selected_amount ?? 0)
  const selectedItems = computed(() => items.value.filter((item) => item.selected))
  const isEmpty = computed(() => items.value.length === 0)

  async function load(): Promise<void> {
    loading.value = true
    error.value = null
    try {
      cart.value = await cartApi.get()
    } catch (e) {
      error.value = e as NormalizedApiError
      throw e
    } finally {
      loading.value = false
    }
  }

  /** Wraps one mutation; every mutation returns the authoritative new cart. */
  async function mutate(action: () => Promise<Cart>): Promise<Cart> {
    mutating.value = true
    error.value = null
    try {
      cart.value = await action()
      return cart.value
    } catch (e) {
      error.value = e as NormalizedApiError
      throw e
    } finally {
      mutating.value = false
    }
  }

  const addItem = (productId: string, skuId: string, quantity = 1) =>
    mutate(() => cartApi.addItem({ product_id: productId, sku_id: skuId, quantity }))

  const updateQuantity = (itemId: string, quantity: number) =>
    mutate(() => cartApi.updateItem(itemId, { quantity }))

  const removeItem = (itemId: string) => mutate(() => cartApi.removeItem(itemId))

  const selectItems = (itemIds: string[], selected: boolean) =>
    mutate(() => cartApi.select({ item_ids: itemIds, selected }))

  const clear = () => mutate(() => cartApi.clear())

  function reset(): void {
    cart.value = null
    error.value = null
  }

  return {
    cart,
    loading,
    mutating,
    error,
    items,
    itemCount,
    selectedAmount,
    selectedItems,
    isEmpty,
    load,
    addItem,
    updateQuantity,
    removeItem,
    selectItems,
    clear,
    reset,
  }
})
