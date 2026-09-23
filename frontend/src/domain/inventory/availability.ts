/**
 * Inventory action availability (§26, §40–§44, §99).
 *
 * InventoryMovement is APPEND-ONLY and carries before/after values, so an adjustment is
 * legal regardless of status — the real guard is the OPTIMISTIC LOCK `version`, not a state.
 * A stale version returns `INVENTORY_CONFLICT_STALE_VERSION` (40 002) and the UI MUST NOT
 * auto-retry with a bumped version: that would silently overwrite another operator's change.
 */

export interface InventoryRowLike {
  on_hand: number
  reserved: number
  version?: number | null
}

/**
 * The version must be present and a non-negative integer for the request to be well-formed.
 * Offering the button without one would produce a call the server cannot verify.
 */
export function canAdjustInventory(row: InventoryRowLike): boolean {
  return typeof row.version === 'number' && Number.isInteger(row.version) && row.version >= 0
}

/** Sellable units, computed the same way the display does: on hand minus reservations. */
export function sellableQuantity(row: Pick<InventoryRowLike, 'on_hand' | 'reserved'>): number {
  return row.on_hand - row.reserved
}

/**
 * Reservations exceeding stock on hand is an integrity problem worth surfacing, not hiding —
 * it means locks were taken without the stock to back them.
 */
export function isOverReserved(row: Pick<InventoryRowLike, 'on_hand' | 'reserved'>): boolean {
  return row.reserved > row.on_hand
}

/** A row at or below the low-stock threshold needs operator attention. */
export function isLowStock(row: InventoryRowLike, threshold: number): boolean {
  return sellableQuantity(row) <= threshold
}

/**
 * Validate the adjustment the operator typed, before it becomes a request.
 * Returns a Chinese reason when invalid, or `null` when acceptable.
 *
 * Business rules encoded: a delta of 0 is a no-op (the server would still append a movement,
 * which would be noise in the ledger), and the result may not go negative.
 */
export function validateAdjustment(row: InventoryRowLike, delta: number): string | null {
  if (!Number.isInteger(delta)) return '调整数量必须是整数'
  if (delta === 0) return '调整数量不能为 0'
  if (sellableQuantity(row) + delta < 0) return '调整后库存不能为负'
  return null
}
