/**
 * Order action availability — the state-machine drift guard (§31–§34, §99).
 *
 * These tests encode the FROZEN state machine, not whatever the UI currently does, so if
 * someone "simplifies" a predicate the test fails and lists the rule that broke.
 *
 * The most important case is the §31 rule: **shipping never changes `order_status`**. An
 * order is PROCESSING while shipped, so a naive implementation would infer "shippable" from
 * `status === 'PROCESSING'` alone and offer 发货 on an order that already shipped.
 */

import { describe, expect, it } from 'vitest'
import {
  canCancelOrder,
  canConfirmReceipt,
  canRefundOrder,
  canShipOrder,
  orderActionBlockedReason,
  orderActionFlags,
} from '@/domain/orders/availability'
import type { AfterSaleStatus, FulfillmentStatus, OrderStatus, PaymentStatus } from '@/types/domain'

/** Build the minimal shape the predicates read. */
function order(overrides: {
  status: OrderStatus
  payment_status?: PaymentStatus
  fulfillment_status?: FulfillmentStatus
  after_sale_status?: AfterSaleStatus
  refundable_amount?: number
}) {
  return {
    status: overrides.status,
    payment_status: overrides.payment_status ?? 'PAID',
    fulfillment_status: overrides.fulfillment_status ?? 'UNFULFILLED',
    after_sale_status: overrides.after_sale_status ?? ('NONE' as AfterSaleStatus),
    refundable_amount: overrides.refundable_amount ?? 0,
  }
}

describe('canCancelOrder', () => {
  it('allows cancelling an unpaid order that has not shipped', () => {
    expect(canCancelOrder(order({ status: 'PENDING_PAYMENT', payment_status: 'UNPAID' }))).toBe(true)
  })

  it('allows cancelling a paid but unshipped order', () => {
    expect(canCancelOrder(order({ status: 'PROCESSING' }))).toBe(true)
  })

  it('REFUSES cancel once anything shipped, even though status is still PROCESSING (§31)', () => {
    for (const fulfillment of ['PARTIAL_SHIPPED', 'SHIPPED', 'DELIVERED'] as FulfillmentStatus[]) {
      expect(
        canCancelOrder(order({ status: 'PROCESSING', fulfillment_status: fulfillment })),
        `must not be cancellable when fulfillment=${fulfillment}`,
      ).toBe(false)
    }
  })

  it('REFUSES cancel on terminal orders', () => {
    for (const status of ['COMPLETED', 'CANCELLED', 'CLOSED'] as OrderStatus[]) {
      expect(canCancelOrder(order({ status }))).toBe(false)
    }
  })

  it('REFUSES cancel once money has been refunded', () => {
    expect(canCancelOrder(order({ status: 'PROCESSING', after_sale_status: 'REFUNDED' }))).toBe(false)
    expect(canCancelOrder(order({ status: 'PROCESSING', after_sale_status: 'PARTIAL_REFUNDED' }))).toBe(
      false,
    )
  })
})

describe('canShipOrder', () => {
  const fulfillmentId = 'ful-1'

  it('allows shipping a paid, processing, unshipped order WITH a fulfillment id', () => {
    expect(canShipOrder(order({ status: 'PROCESSING' }), fulfillmentId)).toBe(true)
  })

  it('allows shipping a partially shipped order (a second package)', () => {
    expect(
      canShipOrder(order({ status: 'PROCESSING', fulfillment_status: 'PARTIAL_SHIPPED' }), fulfillmentId),
    ).toBe(true)
  })

  it('REFUSES shipping when no fulfillment id exists — there would be no URL to call', () => {
    // The frozen endpoint is POST /fulfillments/{id}/ship, so an order without a
    // fulfillment has no callable action. Offering the button would guarantee a failure.
    expect(canShipOrder(order({ status: 'PROCESSING' }), undefined)).toBe(false)
  })

  it('REFUSES shipping an unpaid order', () => {
    for (const payment of ['UNPAID', 'PAYING'] as PaymentStatus[]) {
      expect(
        canShipOrder(order({ status: 'PENDING_PAYMENT', payment_status: payment }), fulfillmentId),
        `must not ship with payment=${payment}`,
      ).toBe(false)
    }
  })

  it('REFUSES shipping an already fully shipped or delivered order', () => {
    for (const fulfillment of ['SHIPPED', 'DELIVERED'] as FulfillmentStatus[]) {
      expect(
        canShipOrder(order({ status: 'PROCESSING', fulfillment_status: fulfillment }), fulfillmentId),
      ).toBe(false)
    }
  })

  it('REFUSES shipping a cancelled or completed order', () => {
    expect(canShipOrder(order({ status: 'CANCELLED' }), fulfillmentId)).toBe(false)
    expect(canShipOrder(order({ status: 'COMPLETED' }), fulfillmentId)).toBe(false)
  })
})

describe('canConfirmReceipt', () => {
  it('allows confirming a shipped or partially shipped processing order', () => {
    expect(canConfirmReceipt(order({ status: 'PROCESSING', fulfillment_status: 'SHIPPED' }))).toBe(true)
    expect(
      canConfirmReceipt(order({ status: 'PROCESSING', fulfillment_status: 'PARTIAL_SHIPPED' })),
    ).toBe(true)
  })

  it('REFUSES confirming before anything shipped', () => {
    expect(canConfirmReceipt(order({ status: 'PROCESSING' }))).toBe(false)
  })

  it('REFUSES confirming an order that is already DELIVERED (receipt already given)', () => {
    expect(canConfirmReceipt(order({ status: 'PROCESSING', fulfillment_status: 'DELIVERED' }))).toBe(
      false,
    )
  })

  it('REFUSES confirming an unpaid order', () => {
    expect(
      canConfirmReceipt(order({ status: 'PENDING_PAYMENT', fulfillment_status: 'SHIPPED' })),
    ).toBe(false)
  })
})

describe('canRefundOrder', () => {
  it('allows a refund while money remains on a paid order', () => {
    expect(canRefundOrder(order({ status: 'PROCESSING', refundable_amount: 1 }))).toBe(true)
    expect(canRefundOrder(order({ status: 'PROCESSING', refundable_amount: 299900 }))).toBe(true)
  })

  it('REFUSES when there is no refundable balance', () => {
    expect(canRefundOrder(order({ status: 'PROCESSING', refundable_amount: 0 }))).toBe(false)
  })

  it('REFUSES on an unpaid order even if a balance were reported', () => {
    expect(
      canRefundOrder(order({ status: 'PENDING_PAYMENT', payment_status: 'UNPAID', refundable_amount: 100 })),
    ).toBe(false)
  })
})

describe('orderActionFlags — the row-action matrix', () => {
  it('an unpaid, unshipped order exposes cancel but NOT ship', () => {
    const flags = orderActionFlags(
      order({ status: 'PENDING_PAYMENT', payment_status: 'UNPAID', refundable_amount: 0 }),
      'ful-1',
    )
    expect(flags.cancel).toBe(true)
    expect(flags.ship).toBe(false)
    expect(flags.confirmReceipt).toBe(false)
    expect(flags.refund).toBe(false)
    expect(flags.viewDetail).toBe(true)
  })

  it('a paid, unshipped order exposes cancel + ship, but not confirm-receipt', () => {
    const flags = orderActionFlags(order({ status: 'PROCESSING', refundable_amount: 100 }), 'ful-1')
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
      order({ status: 'PROCESSING', fulfillment_status: 'SHIPPED', refundable_amount: 100 }),
      'ful-1',
    )
    expect(flags.ship).toBe(false)
    expect(flags.confirmReceipt).toBe(true)
    // Cancel is refused after shipping (§31), even though the order is still PROCESSING.
    expect(flags.cancel).toBe(false)
  })

  it('a delivered order exposes neither ship nor confirm-receipt', () => {
    const flags = orderActionFlags(
      order({ status: 'PROCESSING', fulfillment_status: 'DELIVERED', refundable_amount: 100 }),
      'ful-1',
    )
    expect(flags.ship).toBe(false)
    expect(flags.confirmReceipt).toBe(false)
  })

  it('a completed order exposes no row actions except detail', () => {
    const flags = orderActionFlags(
      order({ status: 'COMPLETED', fulfillment_status: 'DELIVERED', refundable_amount: 0 }),
      'ful-1',
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
    const flags = orderActionFlags(order({ status: 'CANCELLED', refundable_amount: 0 }))
    expect(flags.cancel).toBe(false)
    expect(flags.ship).toBe(false)
    expect(flags.viewDetail).toBe(true)
  })
})

describe('orderActionBlockedReason — the UI explains WHY, it does not silently hide', () => {
  it('explains a shipped order cannot be cancelled', () => {
    expect(
      orderActionBlockedReason('cancel', order({ status: 'PROCESSING', fulfillment_status: 'SHIPPED' })),
    ).toContain('已发货')
  })

  it('explains an unpaid order cannot ship', () => {
    expect(
      orderActionBlockedReason('ship', order({ status: 'PENDING_PAYMENT', payment_status: 'UNPAID' }), 'ful-1'),
    ).toContain('尚未支付')
  })

  it('explains a missing fulfillment instead of pretending the order cannot ship', () => {
    expect(orderActionBlockedReason('ship', order({ status: 'PROCESSING' }), undefined)).toContain(
      '履约单',
    )
  })

  it('explains an empty refundable balance', () => {
    expect(orderActionBlockedReason('refund', order({ status: 'PROCESSING', refundable_amount: 0 }))).toContain(
      '可退余额',
    )
  })
})
