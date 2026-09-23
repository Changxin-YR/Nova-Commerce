/**
 * After-sale + inventory + pending-action availability (§47–§52, §26, §101).
 *
 * Kept in one file because they share a theme: each predicate must refuse to OFFER an action
 * the backend would reject, and the refund cap must be computed identically to the server's.
 */

import { describe, expect, it } from 'vitest'
import {
  afterSaleActionFlags,
  afterSaleBlockedReason,
  afterSaleRefundable,
  canDecideAfterSale,
  canRefundAfterSale,
  validateRefundAmount,
} from '@/domain/afterSales/availability'
import {
  asInventoryRow,
  canAdjustInventory,
  isLowStock,
  isOverReserved,
  sellableQuantity,
  validateAdjustment,
  type InventoryRowLike,
} from '@/domain/inventory/availability'
import {
  canDecidePendingAction,
  pendingActionBlockedReason,
  pendingActionFlags,
  riskRequiresApproval,
} from '@/domain/governance/availability'
import type {
  AfterSaleStatus,
  PendingActionStatus,
} from '@/types/domain'
import type { Inventory } from '@/types/frozen-contract'

function afterSale(overrides: {
  status: AfterSaleStatus
  requested_amount?: number
  approved_amount?: number
  refunded_amount?: number
}) {
  return {
    status: overrides.status,
    requested_amount: overrides.requested_amount ?? 10000,
    approved_amount: overrides.approved_amount ?? 0,
    refunded_amount: overrides.refunded_amount ?? 0,
  }
}

describe('afterSaleRefundable — the cap the server enforces independently', () => {
  it('uses the approved amount when the merchant approved something', () => {
    expect(afterSaleRefundable(afterSale({ status: 'PROCESSING', requested_amount: 10000, approved_amount: 6000 }))).toBe(
      6000,
    )
  })

  it('falls back to the requested amount while nothing is approved yet', () => {
    expect(afterSaleRefundable(afterSale({ status: 'PROCESSING', requested_amount: 10000 }))).toBe(10000)
  })

  it('subtracts what was already refunded (partial refunds)', () => {
    expect(
      afterSaleRefundable(afterSale({ status: 'PARTIAL_REFUNDED', approved_amount: 10000, refunded_amount: 4000 })),
    ).toBe(6000)
  })

  it('never returns a negative cap, even if the server reports an over-refund', () => {
    expect(
      afterSaleRefundable(afterSale({ status: 'PARTIAL_REFUNDED', approved_amount: 100, refunded_amount: 500 })),
    ).toBe(0)
  })
})

describe('after-sale decisions', () => {
  it('allows approve/reject only while PROCESSING', () => {
    expect(canDecideAfterSale('PROCESSING')).toBe(true)
    for (const status of ['NONE', 'PARTIAL_REFUNDED', 'REFUNDED'] as AfterSaleStatus[]) {
      expect(canDecideAfterSale(status), `must not decide ${status}`).toBe(false)
    }
  })

  it('allows a first refund on PROCESSING and a further one on PARTIAL_REFUNDED', () => {
    expect(canRefundAfterSale(afterSale({ status: 'PROCESSING' }))).toBe(true)
    expect(
      canRefundAfterSale(afterSale({ status: 'PARTIAL_REFUNDED', approved_amount: 10000, refunded_amount: 4000 })),
    ).toBe(true)
  })

  it('refuses a refund when nothing is left or the request is closed', () => {
    expect(canRefundAfterSale(afterSale({ status: 'REFUNDED', approved_amount: 10000, refunded_amount: 10000 }))).toBe(
      false,
    )
    expect(canRefundAfterSale(afterSale({ status: 'NONE' }))).toBe(false)
    expect(
      canRefundAfterSale(afterSale({ status: 'PARTIAL_REFUNDED', approved_amount: 5000, refunded_amount: 5000 })),
    ).toBe(false)
  })

  it('exposes the right row-action set for each status', () => {
    expect(afterSaleActionFlags(afterSale({ status: 'PROCESSING' }))).toEqual({
      approve: true,
      reject: true,
      refund: true,
      viewDetail: true,
    })
    expect(
      afterSaleActionFlags(
        afterSale({ status: 'PARTIAL_REFUNDED', approved_amount: 9000, refunded_amount: 3000 }),
      ),
    ).toEqual({ approve: false, reject: false, refund: true, viewDetail: true })
    expect(
      afterSaleActionFlags(afterSale({ status: 'REFUNDED', approved_amount: 9000, refunded_amount: 9000 })),
    ).toEqual({ approve: false, reject: false, refund: false, viewDetail: true })
  })

  it('explains a closed request instead of hiding the column', () => {
    expect(afterSaleBlockedReason('REFUNDED')).toContain('已完成')
    expect(afterSaleBlockedReason('PARTIAL_REFUNDED')).toContain('不能再次核准')
  })
})

describe('validateRefundAmount — rejects before the server has to', () => {
  const record = afterSale({ status: 'PROCESSING', requested_amount: 10000 })

  it('accepts an amount inside the cap', () => {
    expect(validateRefundAmount(record, 1)).toBeNull()
    expect(validateRefundAmount(record, 10000)).toBeNull()
  })

  it('rejects a non-integer, zero, negative, or over-cap amount', () => {
    expect(validateRefundAmount(record, 10.5)).toContain('整数')
    expect(validateRefundAmount(record, 0)).toContain('大于 0')
    expect(validateRefundAmount(record, -100)).toContain('大于 0')
    expect(validateRefundAmount(record, 10001)).toContain('超出可退上限')
  })
})

describe('inventory adjustment', () => {
  /**
   * Row builder for the FROZEN inventory shape (API_CONTRACT.md §7).
   *
   * `sellable_qty` is server-computed and deliberately INDEPENDENT of `on_hand_qty - locked_qty`
   * in these fixtures, so the tests fail if any predicate starts recomputing it.
   */
  function row(overrides: Partial<InventoryRowLike> = {}): InventoryRowLike {
    return { on_hand_qty: 10, locked_qty: 0, sellable_qty: 10, version: 1, ...overrides }
  }

  it('requires a usable optimistic-lock version', () => {
    expect(canAdjustInventory(row({ version: 0 }))).toBe(true)
    expect(canAdjustInventory(row({ version: 7 }))).toBe(true)
    expect(canAdjustInventory(row({ version: null }))).toBe(false)
    expect(canAdjustInventory(row({ version: undefined }))).toBe(false)
    expect(canAdjustInventory(row({ version: 1.5 }))).toBe(false)
    expect(canAdjustInventory(row({ version: -1 }))).toBe(false)
  })

  it('reads sellable stock from the SERVER field and never recomputes it', () => {
    // The whole point of the migration: the server's number is the number.
    expect(sellableQuantity(row({ on_hand_qty: 100, locked_qty: 30, sellable_qty: 70 }))).toBe(70)
    // Now the case that a client-side `on_hand - locked` would get WRONG: a safety stock of 60
    // is already deducted on the wire, so recomputing would claim 70 sellable units the server
    // would refuse to sell.
    expect(sellableQuantity(row({ on_hand_qty: 100, locked_qty: 30, sellable_qty: 10 }))).toBe(10)
    // A negative sellable (over-reserved and safety-stock-limited) is reported, not clamped away.
    expect(sellableQuantity(row({ on_hand_qty: 5, locked_qty: 9, sellable_qty: 0 }))).toBe(0)
  })

  it('flags over-reserved rows, which are an integrity problem not a display quirk', () => {
    expect(isOverReserved(row({ on_hand_qty: 10, locked_qty: 12 }))).toBe(true)
    expect(isOverReserved(row({ on_hand_qty: 10, locked_qty: 10 }))).toBe(false)
  })

  it('flags low stock by sellable quantity, not raw on-hand', () => {
    // 100 on hand but the server reports only 5 sellable, which is low.
    expect(isLowStock(row({ on_hand_qty: 100, locked_qty: 95, sellable_qty: 5 }), 10)).toBe(true)
    expect(isLowStock(row({ on_hand_qty: 100, locked_qty: 50, sellable_qty: 50 }), 10)).toBe(false)
    // Boundary: exactly at the threshold counts as low.
    expect(isLowStock(row({ sellable_qty: 10 }), 10)).toBe(true)
  })

  it('rejects a no-op or negative-resulting adjustment', () => {
    expect(validateAdjustment(row({ sellable_qty: 8 }), 5)).toBeNull()
    expect(validateAdjustment(row({ sellable_qty: 8 }), -8)).toBeNull()
    expect(validateAdjustment(row({ sellable_qty: 8 }), 0)).toContain('不能为 0')
    expect(validateAdjustment(row({ sellable_qty: 8 }), 1.5)).toContain('整数')
    // sellable is 8, so -9 would go negative.
    expect(validateAdjustment(row({ sellable_qty: 8 }), -9)).toContain('不能为负')
  })

  it('validates against the server number, not on_hand_qty - locked_qty', () => {
    // `on_hand_qty - locked_qty` here is 70, but only 10 units are actually sellable. A
    // recomputing implementation would allow -30 and let the operator drive stock negative.
    const constrained = row({ on_hand_qty: 100, locked_qty: 30, sellable_qty: 10 })
    expect(validateAdjustment(constrained, -30)).toContain('不能为负')
    expect(validateAdjustment(constrained, -10)).toBeNull()
  })

  it('narrows a frozen Inventory row without inventing fields', () => {
    const inventory: Inventory = {
      id: 11,
      warehouse_id: 1,
      sku_id: 3,
      sku_no: 'NV-SKU-0003',
      product_name: 'Nova Phone 15 Pro',
      sku_name: '原色钛金属 256GB',
      available_qty: 42,
      locked_qty: 3,
      safety_stock: 0,
      sellable_qty: 42,
      on_hand_qty: 45,
      version: 7,
      updated_at: '2026-09-22T23:31:07.507Z',
    }
    expect(asInventoryRow(inventory)).toEqual({
      on_hand_qty: 45,
      locked_qty: 3,
      sellable_qty: 42,
      version: 7,
    })
  })
})

describe('pending actions (HITL)', () => {
  it('allows a decision only while PENDING', () => {
    expect(canDecidePendingAction('PENDING')).toBe(true)
    for (const status of [
      'APPROVED',
      'REJECTED',
      'EXECUTING',
      'SUCCEEDED',
      'FAILED',
      'EXPIRED',
    ] as PendingActionStatus[]) {
      expect(canDecidePendingAction(status), `must not decide ${status}`).toBe(false)
    }
  })

  it('requires human approval for HIGH and CRITICAL, and NOT for READ/LOW/MEDIUM', () => {
    expect(riskRequiresApproval('HIGH')).toBe(true)
    expect(riskRequiresApproval('CRITICAL')).toBe(true)
    // READ is the LOWEST level (read-only) — not a typo for RED.
    expect(riskRequiresApproval('READ')).toBe(false)
    expect(riskRequiresApproval('LOW')).toBe(false)
    expect(riskRequiresApproval('MEDIUM')).toBe(false)
  })

  it('derives both decide flags from the status in one place', () => {
    expect(pendingActionFlags({ status: 'PENDING' })).toEqual({ approve: true, reject: true })
    expect(pendingActionFlags({ status: 'EXPIRED' })).toEqual({ approve: false, reject: false })
  })

  it('explains why each non-decidable state is blocked', () => {
    expect(pendingActionBlockedReason('EXECUTING')).toContain('执行中')
    expect(pendingActionBlockedReason('EXPIRED')).toContain('过期')
    expect(pendingActionBlockedReason('FAILED')).toContain('回执')
  })
})
