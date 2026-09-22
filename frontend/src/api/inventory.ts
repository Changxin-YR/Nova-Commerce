/**
 * Inventory module (§98).
 *
 * Optimistic locking: `adjust` carries the `version` the merchant last saw. A stale
 * version returns `INVENTORY_CONFLICT_STALE_VERSION` (40002) and the UI must ask the
 * merchant to refresh — it must never silently retry with a bumped version, because
 * that would overwrite a concurrent change.
 */

import { httpClient } from '@/api/client'
import { API } from '@/api/endpoints'
import type { PageQuery } from '@/types/api'
import type { Paged } from '@/types/domain'

export interface StockRow {
  sku_id: string
  sku_code: string
  product_id: string
  product_title: string
  specs: Record<string, string>
  /** Total units on hand. */
  on_hand: number
  /** Units held by unpaid orders. The UI shows `on_hand - reserved` as sellable. */
  reserved: number
  /** Optimistic-lock version. Must be echoed back on adjust. */
  version: number
  warehouse_id: string
}

export interface AdjustStockRequest {
  /** Positive adds stock, negative removes it. A movement record is written either way. */
  delta: number
  reason: string
  /** Echo of the version the merchant saw. */
  version: number
  idempotency_key: string
}

export interface InventoryMovement {
  id: string
  sku_id: string
  movement_type: 'INBOUND' | 'OUTBOUND' | 'RESERVE' | 'RELEASE' | 'ADJUST' | 'SALE' | 'REFUND'
  quantity: number
  before_quantity: number
  after_quantity: number
  reference_no?: string
  reason?: string
  operator: string
  created_at: string
}

export const inventoryApi = {
  /** Display-only availability; the server still decides at checkout. */
  async available(skuId: string): Promise<number> {
    return httpClient.get<number>(API.inventory.available(skuId))
  },
}

export const inventoryAdminApi = {
  async stock(query: PageQuery & { product_id?: string; low_stock_only?: boolean } = {}): Promise<Paged<StockRow>> {
    return httpClient.get<Paged<StockRow>>(API.inventory.adminStock, { params: query })
  },

  async adjust(skuId: string, payload: AdjustStockRequest): Promise<StockRow> {
    return httpClient.post<StockRow>(API.inventory.adjust(skuId), payload, {
      idempotencyKey: payload.idempotency_key,
    })
  },

  async movements(query: PageQuery & { sku_id?: string } = {}): Promise<Paged<InventoryMovement>> {
    return httpClient.get<Paged<InventoryMovement>>(API.inventory.movements, { params: query })
  },
}
