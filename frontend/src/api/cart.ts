/**
 * Cart module. The cart is server-side state (§105 lists `cart` as genuinely
 * cross-page), so every mutation is a request and the response is the new cart.
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import type {
  AddCartItemRequest,
  SelectCartItemsRequest,
  UpdateCartItemRequest,
} from '@/types/api-contract'
import type { Cart } from '@/types/domain'

export const cartApi = {
  async get(): Promise<Cart> {
    return httpClient.get<Cart>(API.cart.current)
  },

  async addItem(payload: AddCartItemRequest): Promise<Cart> {
    return httpClient.post<Cart>(API.cart.items, payload)
  },

  async updateItem(itemId: string, payload: UpdateCartItemRequest): Promise<Cart> {
    return httpClient.put<Cart>(API.cart.item(itemId), payload)
  },

  async removeItem(itemId: string): Promise<Cart> {
    return httpClient.delete<Cart>(API.cart.item(itemId))
  },

  /** Selection is persisted server-side so checkout always agrees with the UI. */
  async select(payload: SelectCartItemsRequest): Promise<Cart> {
    return httpClient.post<Cart>(API.cart.select, payload)
  },

  async clear(): Promise<Cart> {
    return httpClient.delete<Cart>(API.cart.clear)
  },
}
