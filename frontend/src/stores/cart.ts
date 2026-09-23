/** Cart selection lives in Pinia and localStorage; order preview owns every price (§14.1). */
import { computed, ref, watch } from 'vue'
import { defineStore } from 'pinia'
import { useAuthStore } from '@/stores/auth'
import type { CartItem } from '@/types/domain'

const MAX_QUANTITY = 1_000_000

function validLine(value: unknown): value is CartItem {
  if (!value || typeof value !== 'object') return false
  const line = value as Partial<CartItem>
  return typeof line.product_id === 'string' && /^\d+$/.test(line.product_id)
    && typeof line.sku_id === 'string' && /^\d+$/.test(line.sku_id)
    && Number.isSafeInteger(line.quantity) && Number(line.quantity) > 0
    && Number(line.quantity) <= MAX_QUANTITY && typeof line.selected === 'boolean'
}

export const useCartStore = defineStore('cart', () => {
  const auth = useAuthStore()
  const items = ref<CartItem[]>([])
  const loading = ref(false)
  const mutating = ref(false)
  const itemCount = computed(() => items.value.reduce((count, item) => count + item.quantity, 0))
  const selectedItems = computed(() => items.value.filter((item) => item.selected))
  const isEmpty = computed(() => items.value.length === 0)
  const storageKey = computed(() => `nova:cart:v1:${auth.user?.id ?? 'guest'}`)
  let loadedKey = ''

  function persist(): void {
    try {
      localStorage.setItem(storageKey.value, JSON.stringify(items.value))
    } catch {
      // The in-memory cart remains usable when browser storage is unavailable.
    }
  }

  async function load(): Promise<void> {
    if (loadedKey === storageKey.value) return
    loading.value = true
    try {
      const stored = localStorage.getItem(storageKey.value)
      const parsed: unknown = stored ? JSON.parse(stored) : []
      items.value = Array.isArray(parsed)
        ? parsed.filter(validLine).map((line) => ({
            id: line.sku_id,
            product_id: line.product_id,
            sku_id: line.sku_id,
            quantity: line.quantity,
            selected: line.selected,
            product_title: typeof line.product_title === 'string' ? line.product_title : undefined,
            sku_name: typeof line.sku_name === 'string' ? line.sku_name : undefined,
            cover_url: typeof line.cover_url === 'string' ? line.cover_url : undefined,
          }))
        : []
      loadedKey = storageKey.value
    } catch {
      items.value = []
      loadedKey = storageKey.value
    } finally {
      loading.value = false
    }
  }

  watch(storageKey, () => {
    items.value = []
    loadedKey = ''
    void load()
  })

  async function mutate(action: () => void): Promise<void> {
    await load()
    mutating.value = true
    try {
      action()
      persist()
    } finally {
      mutating.value = false
    }
  }

  async function addItem(
    productId: string,
    skuId: string,
    quantity = 1,
    display: Pick<CartItem, 'product_title' | 'sku_name' | 'cover_url'> = {},
  ): Promise<void> {
    if (!Number.isSafeInteger(quantity) || quantity < 1 || quantity > MAX_QUANTITY) {
      throw new Error('invalid cart quantity')
    }
    await mutate(() => {
      const existing = items.value.find((line) => line.sku_id === skuId)
      if (existing) {
        if (existing.quantity + quantity > MAX_QUANTITY) throw new Error('cart quantity limit exceeded')
        existing.quantity += quantity
        existing.selected = true
        Object.assign(existing, display)
      } else {
        items.value.push({ id: skuId, product_id: productId, sku_id: skuId, quantity, selected: true, ...display })
      }
    })
  }

  const updateQuantity = async (itemId: string, quantity: number) => mutate(() => {
    if (!Number.isSafeInteger(quantity) || quantity < 1 || quantity > MAX_QUANTITY) {
      throw new Error('invalid cart quantity')
    }
    const line = items.value.find((item) => item.id === itemId)
    if (line) line.quantity = quantity
  })
  const removeItem = async (itemId: string) => mutate(() => {
    items.value = items.value.filter((item) => item.id !== itemId)
  })
  const selectItems = async (itemIds: string[], selected: boolean) => mutate(() => {
    const ids = new Set(itemIds)
    for (const item of items.value) if (ids.has(item.id)) item.selected = selected
  })
  const clear = async () => mutate(() => { items.value = [] })

  function reset(): void {
    items.value = []
    loadedKey = ''
  }

  return {
    loading, mutating, items, itemCount, selectedItems, isEmpty,
    load, addItem, updateQuantity, removeItem, selectItems, clear, reset,
  }
})
