/**
 * Order action availability — the state-machine drift guard (§31–§34, §99).
 *
 * These tests encode the FROZEN state machine and the FROZEN wire shape
 * (`API_CONTRACT.md` §5–§6), not whatever the UI currently does, so if someone "simplifies" a
 * predicate or renames a field the test fails and names the rule that broke.
 *
 * Two rules carry most of the weight here:
 *  1. **Shipping never changes `order_status`** (§31). An order is PROCESSING while shipped, so a
 *     naive implementation would infer "shippable" from `order_status === 'PROCESSING'` alone and
 *     offer 发货 on an order that already went out.
 *  2. **"Not yet shipped" is `carrier === null`**, not a status field. The server already states
 *     it on the fulfillment.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest'
import {
  canCancelOrder,
  canConfirmReceipt,
  canRefundOrder,
  canShipOrder,
  findUnshippedFulfillment,
  isUnshippedFulfillment,
  orderActionBlockedReason,
  orderActionFlags,
  refundableAmount,
  refundableBridgeState,
} from '@/domain/orders/availability'
import type { AfterSaleStatus, FulfillmentStatus, OrderStatus, PaymentStatus } from '@/types/domain'
import type { Fulfillment } from '@/types/frozen-contract'

/**
 * Build the minimal order shape the predicates read, using the FROZEN field names
 * (`order_status`, and `paid_amount`/`refunded_amount` instead of a server-side
 * `refundable_amount`).
 */
function order(overrides: {
  order_status: OrderStatus
  payment_status?: PaymentStatus
  fulfillment_status?: FulfillmentStatus
  after_sale_status?: AfterSaleStatus
  paid_amount?: number
  refunded_amount?: number
}) {
  return {
    order_status: overrides.order_status,
    payment_status: overrides.payment_status ?? 'PAID',
    fulfillment_status: overrides.fulfillment_status ?? 'UNFULFILLED',
    after_sale_status: overrides.after_sale_status ?? ('NONE' as AfterSaleStatus),
    paid_amount: overrides.paid_amount ?? 0,
    refunded_amount: overrides.refunded_amount ?? 0,
  }
}

/**
 * A fulfillment, `carrier: null` unless explicitly shipped — which is exactly how the server
 * reports an outstanding package (§5).
 */
function fulfillment(overrides: Partial<Pick<Fulfillment, 'id' | 'carrier' | 'tracking_no'>> = {}) {
  return {
    id: overrides.id ?? 123,
    carrier: overrides.carrier === undefined ? null : overrides.carrier,
    tracking_no: overrides.tracking_no === undefined ? null : overrides.tracking_no,
  }
}

/** A shippable fulfillment: nothing stamped on it yet. */
const unshipped = fulfillment()

describe('refundableAmount — server-owned, with a documented pre-backend bridge', () => {
  it('READS the server field when it is present', () => {
    // `API_CONTRACT.md` §11 made this server-owned because it gates whether a refund control
    // renders at all (§15: the client must not be the authority on an accounting rule).
    expect(refundableAmount({ paid_amount: 279900, refunded_amount: 0, refundable_amount: 279900 })).toBe(
      279900,
    )
  })

  it('prefers the server figure over its own arithmetic when the two disagree', () => {
    // The server may bound the value below 0, account for a rule the client does not know about,
    // or hold a lock. Its number wins — that is the whole point of the addendum.
    expect(refundableAmount({ paid_amount: 279900, refunded_amount: 0, refundable_amount: 100000 })).toBe(
      100000,
    )
  })

  it('never returns a negative amount, even if the server sends one', () => {
    expect(refundableAmount({ paid_amount: 100, refunded_amount: 500, refundable_amount: -50 })).toBe(0)
  })

  it('still works BEFORE the backend ships the field, so refunds never silently vanish', () => {
    // THE FAILURE MODE THE ADDENDUM WAS WRITTEN FOR: `undefined > 0` is false, so a missing field
    // would hide every refund affordance without throwing anything. The bridge derives the value
    // until the order module lands.
    expect(refundableAmount({ paid_amount: 279900, refunded_amount: 0 })).toBe(279900)
    expect(refundableAmount({ paid_amount: 279900, refunded_amount: 79900 })).toBe(200000)
    expect(refundableAmount({ paid_amount: 279900, refunded_amount: 279900 })).toBe(0)
    // An explicit null is treated as "absent", not as zero.
    expect(refundableAmount({ paid_amount: 279900, refunded_amount: 0, refundable_amount: null })).toBe(
      279900,
    )
  })

  it('subtracts what was refunded from what was paid (bridge)', () => {
    expect(refundableAmount({ paid_amount: 279900, refunded_amount: 0 })).toBe(279900)
    expect(refundableAmount({ paid_amount: 279900, refunded_amount: 79900 })).toBe(200000)
  })

  it('never goes negative, even if the server reports an over-refund', () => {
    expect(refundableAmount({ paid_amount: 100, refunded_amount: 500 })).toBe(0)
  })

  it('is 0 for an unpaid order, so a refund is never offered on it', () => {
    expect(refundableAmount({ paid_amount: 0, refunded_amount: 0 })).toBe(0)
  })
})

describe('the refundable bridge ANNOUNCES itself (§15 transition hygiene)', () => {
  /**
   * The bridge is tolerated only while it is noisy. These tests exist because a silent fallback
   * becomes a permanent fallback: the whole point of the warning is that a bridge which speaks up
   * gets deleted, so the warning is a behaviour worth pinning rather than a nicety.
   */
  beforeEach(() => {
    refundableBridgeState.warned = false
  })

  it('warns when it falls back, naming the missing field and the removal trigger', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    refundableAmount({ paid_amount: 279900, refunded_amount: 0 })

    expect(warn).toHaveBeenCalledTimes(1)
    const message = String(warn.mock.calls[0]?.[0] ?? '')
    // Names the missing field...
    expect(message).toContain('OrderDetail.refundable_amount')
    // ...and the condition that retires the bridge, so nobody has to reverse-engineer it.
    expect(message).toContain('Phase 5')
    expect(message).toContain('Removal trigger')
    warn.mockRestore()
  })

  it('does NOT warn when the server supplied the field', () => {
    // The bridge must be silent on the happy path, otherwise the signal becomes noise and the
    // warning is ignored exactly when it matters.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    refundableAmount({ paid_amount: 279900, refunded_amount: 0, refundable_amount: 279900 })

    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })

  it('warns only ONCE per session, because it is called from computed values and templates', () => {
    // A per-call warning would flood the console into uselessness the moment an order list renders.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const legacyOrder = { paid_amount: 279900, refunded_amount: 0 }
    refundableAmount(legacyOrder)
    refundableAmount(legacyOrder)
    refundableAmount(legacyOrder)

    expect(warn).toHaveBeenCalledTimes(1)
    warn.mockRestore()
  })
})

describe('isUnshippedFulfillment — the carrier is the server’s own answer', () => {
  it('treats carrier === null as outstanding', () => {
    expect(isUnshippedFulfillment({ carrier: null })).toBe(true)
  })

  it('treats a stamped carrier CODE as already shipped', () => {
    expect(isUnshippedFulfillment({ carrier: 'SF' })).toBe(false)
  })

  it('is exactly a null check — only the server’s explicit null counts', () => {
    // `carrier` is typed `string | null`, and the contract says it is `null` until shipped. A
    // blanket falsy check would treat an empty-string code as unshipped; requiring `=== null`
    // keeps the rule identical to the wire contract.
    expect(isUnshippedFulfillment({ carrier: '' })).toBe(false)
  })
})

describe('findUnshippedFulfillment — picking the package a ship click acts on', () => {
  it('returns the first fulfillment with no carrier', () => {
    expect(
      findUnshippedFulfillment([
        fulfillment({ id: 1, carrier: 'SF', tracking_no: 'SF1' }),
        fulfillment({ id: 2 }),
      ]),
    ).toBe(2)
  })

  it('returns undefined when every package has shipped', () => {
    expect(findUnshippedFulfillment([fulfillment({ id: 1, carrier: 'SF' })])).toBeUndefined()
    expect(findUnshippedFulfillment([])).toBeUndefined()
    expect(findUnshippedFulfillment(undefined)).toBeUndefined()
  })

  it('never returns an id for an already-shipped package', () => {
    // The specific regression this guards: the old implementation keyed off
    // `fulfillment_status === 'UNFULFILLED'`, so a package that already carried a tracking
    // number could still be selected and the server would answer 70 003.
    expect(findUnshippedFulfillment([fulfillment({ id: 9, carrier: 'JD', tracking_no: 'JD9' })])).toBeUndefined()
  })
})

describe('canCancelOrder', () => {
  it('allows cancelling an unpaid order that has not shipped', () => {
    expect(canCancelOrder(order({ order_status: 'PENDING_PAYMENT', payment_status: 'UNPAID' }))).toBe(true)
  })

  it('allows cancelling a paid but unshipped order', () => {
    expect(canCancelOrder(order({ order_status: 'PROCESSING' }))).toBe(true)
  })

  it('REFUSES cancel once anything shipped, even though status is still PROCESSING (§31)', () => {
    for (const f of ['PARTIAL_SHIPPED', 'SHIPPED', 'DELIVERED'] as FulfillmentStatus[]) {
      expect(
        canCancelOrder(order({ order_status: 'PROCESSING', fulfillment_status: f })),
        `must not be cancellable when fulfillment=${f}`,
      ).toBe(false)
    }
  })

  it('REFUSES cancel on terminal orders', () => {
    for (const order_status of ['COMPLETED', 'CANCELLED', 'CLOSED'] as OrderStatus[]) {
      expect(canCancelOrder(order({ order_status }))).toBe(false)
    }
  })

  it('REFUSES cancel once money has been refunded', () => {
    expect(canCancelOrder(order({ order_status: 'PROCESSING', after_sale_status: 'REFUNDED' }))).toBe(false)
    expect(canCancelOrder(order({ order_status: 'PROCESSING', after_sale_status: 'PARTIAL_REFUNDED' }))).toBe(
      false,
    )
  })
})

describe('canShipOrder', () => {
  it('allows shipping a paid, processing, unshipped order with an unshipped fulfillment', () => {
    expect(canShipOrder(order({ order_status: 'PROCESSING' }), unshipped)).toBe(true)
  })

  it('allows shipping a partially shipped order (a second package)', () => {
    expect(
      canShipOrder(order({ order_status: 'PROCESSING', fulfillment_status: 'PARTIAL_SHIPPED' }), unshipped),
    ).toBe(true)
  })

  it('REFUSES shipping when no fulfillment exists — there would be no URL to call', () => {
    // The frozen endpoint is POST /fulfillments/{id}/ship, so an order without an outstanding
    // fulfillment has no callable action. Offering the button would guarantee a failure.
    expect(canShipOrder(order({ order_status: 'PROCESSING' }), undefined)).toBe(false)
    expect(canShipOrder(order({ order_status: 'PROCESSING' }), null)).toBe(false)
  })

  it('REFUSES shipping a fulfillment the server has already stamped with a carrier', () => {
    // THE CARRIER RULE. The order looks perfectly shippable by every status field; only the
    // fulfillment's own `carrier` says otherwise.
    expect(
      canShipOrder(order({ order_status: 'PROCESSING' }), fulfillment({ carrier: 'SF', tracking_no: 'SF1' })),
    ).toBe(false)
  })

  it('REFUSES shipping an unpaid order', () => {
    for (const payment of ['UNPAID', 'PAYING'] as PaymentStatus[]) {
      expect(
        canShipOrder(order({ order_status: 'PENDING_PAYMENT', payment_status: payment }), unshipped),
        `must not ship with payment=${payment}`,
      ).toBe(false)
    }
  })

  it('REFUSES shipping an already fully shipped or delivered order', () => {
    for (const f of ['SHIPPED', 'DELIVERED'] as FulfillmentStatus[]) {
      expect(
        canShipOrder(order({ order_status: 'PROCESSING', fulfillment_status: f }), unshipped),
      ).toBe(false)
    }
  })

  it('REFUSES shipping a cancelled or completed order', () => {
    expect(canShipOrder(order({ order_status: 'CANCELLED' }), unshipped)).toBe(false)
    expect(canShipOrder(order({ order_status: 'COMPLETED' }), unshipped)).toBe(false)
  })
})

describe('canConfirmReceipt', () => {
  it('allows confirming a shipped or partially shipped processing order', () => {
    expect(canConfirmReceipt(order({ order_status: 'PROCESSING', fulfillment_status: 'SHIPPED' }))).toBe(true)
    expect(
      canConfirmReceipt(order({ order_status: 'PROCESSING', fulfillment_status: 'PARTIAL_SHIPPED' })),
    ).toBe(true)
  })

  it('REFUSES confirming before anything shipped', () => {
    expect(canConfirmReceipt(order({ order_status: 'PROCESSING' }))).toBe(false)
  })

  it('REFUSES confirming an order that is already DELIVERED (receipt already given)', () => {
    expect(canConfirmReceipt(order({ order_status: 'PROCESSING', fulfillment_status: 'DELIVERED' }))).toBe(
      false,
    )
  })

  it('REFUSES confirming an unpaid order', () => {
    expect(
      canConfirmReceipt(order({ order_status: 'PENDING_PAYMENT', fulfillment_status: 'SHIPPED' })),
    ).toBe(false)
  })
})

describe('canRefundOrder', () => {
  it('allows a refund while money remains on a paid order', () => {
    expect(canRefundOrder(order({ order_status: 'PROCESSING', paid_amount: 1 }))).toBe(true)
    expect(canRefundOrder(order({ order_status: 'PROCESSING', paid_amount: 299900 }))).toBe(true)
  })

  it('allows a further refund after a partial one', () => {
    expect(
      canRefundOrder(order({ order_status: 'PROCESSING', payment_status: 'PARTIAL_REFUNDED', paid_amount: 279900, refunded_amount: 79900 })),
    ).toBe(true)
  })

  it('REFUSES when there is no refundable balance', () => {
    expect(canRefundOrder(order({ order_status: 'PROCESSING', paid_amount: 0 }))).toBe(false)
    expect(
      canRefundOrder(order({ order_status: 'PROCESSING', payment_status: 'REFUNDED', paid_amount: 279900, refunded_amount: 279900 })),
    ).toBe(false)
  })

  it('REFUSES on an unpaid order even if a balance were reported', () => {
    expect(
      canRefundOrder(order({ order_status: 'PENDING_PAYMENT', payment_status: 'UNPAID', paid_amount: 100 })),
    ).toBe(false)
  })
})

describe('orderActionFlags — the row-action matrix', () => {
  it('an unpaid, unshipped order exposes cancel but NOT ship', () => {
    const flags = orderActionFlags(
      order({ order_status: 'PENDING_PAYMENT', payment_status: 'UNPAID' }),
      unshipped,
    )
    expect(flags.cancel).toBe(true)
    expect(flags.ship).toBe(false)
    expect(flags.confirmReceipt).toBe(false)
    expect(flags.refund).toBe(false)
    expect(flags.viewDetail).toBe(true)
  })

  it('a paid, unshipped order exposes cancel + ship, but not confirm-receipt', () => {
    const flags = orderActionFlags(order({ order_status: 'PROCESSING', paid_amount: 279900 }), unshipped)
    expect(flags).toEqual({
      cancel: true,
      ship: true,
      confirmReceipt: false,
      refund: true,
      viewDetail: true,
    })
  })

  it('a shipped order exposes confirm-receipt and NO ship', () => {
    const flags = orderActionFlags(
      order({ order_status: 'PROCESSING', fulfillment_status: 'SHIPPED', paid_amount: 279900 }),
      unshipped,
    )
    expect(flags.ship).toBe(false)
    expect(flags.confirmReceipt).toBe(true)
    // Cancel is refused after shipping (§31), even though the order is still PROCESSING.
    expect(flags.cancel).toBe(false)
  })

  it('a delivered order exposes neither ship nor confirm-receipt', () => {
    const flags = orderActionFlags(
      order({ order_status: 'PROCESSING', fulfillment_status: 'DELIVERED', paid_amount: 279900 }),
      unshipped,
    )
    expect(flags.ship).toBe(false)
    expect(flags.confirmReceipt).toBe(false)
  })

  it('a completed order exposes no row actions except detail', () => {
    const flags = orderActionFlags(
      order({ order_status: 'COMPLETED', fulfillment_status: 'DELIVERED' }),
      unshipped,
    )
    expect(flags).toEqual({
      cancel: false,
      ship: false,
      confirmReceipt: false,
      refund: false,
      viewDetail: true,
    })
  })

  it('a cancelled order exposes no row actions at all', () => {
    const flags = orderActionFlags(order({ order_status: 'CANCELLED' }))
    expect(flags.cancel).toBe(false)
    expect(flags.ship).toBe(false)
    expect(flags.viewDetail).toBe(true)
  })
})

describe('orderActionBlockedReason — the UI explains WHY, it does not silently hide', () => {
  it('explains a shipped order cannot be cancelled', () => {
    expect(
      orderActionBlockedReason('cancel', order({ order_status: 'PROCESSING', fulfillment_status: 'SHIPPED' })),
    ).toContain('已发货')
  })

  it('explains an unpaid order cannot ship', () => {
    expect(
      orderActionBlockedReason('ship', order({ order_status: 'PENDING_PAYMENT', payment_status: 'UNPAID' }), unshipped),
    ).toContain('尚未支付')
  })

  it('explains a missing fulfillment instead of pretending the order cannot ship', () => {
    expect(orderActionBlockedReason('ship', order({ order_status: 'PROCESSING' }), undefined)).toContain(
      '履约单',
    )
  })

  it('explains that a package already carrying a carrier cannot be shipped again', () => {
    expect(
      orderActionBlockedReason(
        'ship',
        order({ order_status: 'PROCESSING' }),
        fulfillment({ carrier: 'SF', tracking_no: 'SF1' }),
      ),
    ).toContain('已发货')
  })

  it('explains an empty refundable balance', () => {
    expect(orderActionBlockedReason('refund', order({ order_status: 'PROCESSING' }))).toContain('可退余额')
  })
})
