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
import type { AdjustmentPreview, CreateAdjustmentRequest, Inventory } from '@/types/frozen-contract'

/**
 * Append-only stock ledger row.
 *
 * NOTE: `API_CONTRACT.md` does not freeze a movement shape (§10 lists what is still open), so
 * this stays local to the module rather than being promoted into `@/types/frozen-contract`. It is
 * display-only; nothing computes availability or money from it.
 */
export interface InventoryMovement {
  id: number
  sku_id: number
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
  async available(skuId: number | string): Promise<number> {
    return httpClient.get<number>(API.inventory.available(skuId))
  },
}

export const inventoryAdminApi = {
  async stock(
    query: PageQuery & { product_id?: number; low_stock_only?: boolean } = {},
  ): Promise<Paged<Inventory>> {
    return httpClient.get<Paged<Inventory>>(API.inventory.adminStock, { params: query })
  },

  /**
   * Create an inventory adjustment (`POST /api/v1/inventory/adjustments`).
   *
   * TWO THINGS THIS SHAPE GETS RIGHT THAT THE INVENTED ONE DID NOT:
   *  1. The endpoint is NOT sku-keyed. `sku_id` (and `warehouse_id`) travel in the BODY, so a
   *     path like `/inventory/admin/stock/{skuId}/adjust` does not exist.
   *  2. The delta field is `delta_available`, not `delta`. Renaming it locally would compile
   *     perfectly and then fail on the wire — exactly the class of bug this migration removes.
   *
   * `version` is REQUIRED (§27 optimistic lock). A stale version returns 409 with code
   * `INVENTORY_CONFLICT_STALE_VERSION` (40002) and `data` carrying the current `Inventory`, so the
   * UI can show the conflict and offer a refresh — it must never silently retry with a bumped
   * version, because that would overwrite a concurrent operator's change.
   *
   * There is no `idempotency_key`: the optimistic lock IS the concurrency guard, and inventing a
   * field the server does not accept would break the §110 mass-assignment contract.
   */
  async adjust(payload: CreateAdjustmentRequest): Promise<Inventory> {
    return httpClient.post<Inventory>(API.inventory.adjustments, payload)
  },

  /** Dry-run an adjustment so the operator sees before/after before committing. */
  async preview(payload: CreateAdjustmentRequest): Promise<AdjustmentPreview> {
    return httpClient.post<AdjustmentPreview>(API.inventory.adjustmentsPreview, payload)
  },

  async movements(query: PageQuery & { sku_id?: number } = {}): Promise<Paged<InventoryMovement>> {
    return httpClient.get<Paged<InventoryMovement>>(API.inventory.movements, { params: query })
  },
}
