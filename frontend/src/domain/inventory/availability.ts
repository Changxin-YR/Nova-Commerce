/**
 * Inventory action availability (§26, §40–§44, §99).
 *
 * InventoryMovement is APPEND-ONLY and carries before/after values, so an adjustment is
 * legal regardless of status — the real guard is the OPTIMISTIC LOCK `version`, not a state.
 * A stale version returns `INVENTORY_CONFLICT_STALE_VERSION` (40 002) and the UI MUST NOT
 * auto-retry with a bumped version: that would silently overwrite another operator's change.
 */

import type { Inventory } from '@/types/frozen-contract'

/**
 * The inventory fields these predicates read.
 *
 * THE MIGRATION, and why it is a correctness fix rather than a rename:
 *  - the columns are `on_hand_qty` / `locked_qty` (not `on_hand` / `reserved`);
 *  - `sellable_qty` comes from the SERVER and is NOT recomputed here.
 *
 * The previous version derived sellable stock as `on_hand - reserved` in the client. That is a
 * second implementation of a server rule, and the contract puts `sellable_qty` on the wire
 * explicitly — which means it may already account for `safety_stock` or reservations the client
 * knows nothing about. Recomputing would let the UI confidently display a number the server
 * disagrees with, and an operator would act on it.
 */
export interface InventoryRowLike {
  on_hand_qty: number
  locked_qty: number
  /** Server-computed sellable units. Never derived client-side. */
  sellable_qty: number
  /** Optimistic-lock token. */
  version?: number | null
}

/**
 * The version must be present and a non-negative integer for the request to be well-formed.
 * Offering the button without one would produce a call the server cannot verify (§27).
 */
export function canAdjustInventory(row: InventoryRowLike): boolean {
  return typeof row.version === 'number' && Number.isInteger(row.version) && row.version >= 0
}

/** Sellable units, exactly as the server reported them. */
export function sellableQuantity(row: Pick<InventoryRowLike, 'sellable_qty'>): number {
  return row.sellable_qty
}

/**
 * Reservations exceeding stock on hand is an integrity problem worth surfacing, not hiding —
 * it means locks were taken without the stock to back them.
 */
export function isOverReserved(row: Pick<InventoryRowLike, 'on_hand_qty' | 'locked_qty'>): boolean {
  return row.locked_qty > row.on_hand_qty
}

/** A row at or below the low-stock threshold needs operator attention. */
export function isLowStock(row: InventoryRowLike, threshold: number): boolean {
  return sellableQuantity(row) <= threshold
}

/**
 * Validate the adjustment the operator typed, before it becomes a request.
 * Returns a Chinese reason when invalid, or `null` when acceptable.
 *
 * Business rules encoded: a delta of 0 is a no-op (the server would still append a movement, which
 * would be noise in the ledger), and the result may not go negative.
 */
export function validateAdjustment(row: InventoryRowLike, delta: number): string | null {
  if (!Number.isInteger(delta)) return '调整数量必须是整数'
  if (delta === 0) return '调整数量不能为 0'
  if (sellableQuantity(row) + delta < 0) return '调整后库存不能为负'
  return null
}

/** Narrow a frozen `Inventory` row to the shape the predicates above accept. */
export function asInventoryRow(row: Inventory): InventoryRowLike {
  return {
    on_hand_qty: row.on_hand_qty,
    locked_qty: row.locked_qty,
    sellable_qty: row.sellable_qty,
    version: row.version,
  }
}
