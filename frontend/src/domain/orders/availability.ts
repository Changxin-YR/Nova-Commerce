/**
 * Order action availability — which row actions a given order state permits (§31–§34, §99).
 *
 * WHY THIS IS A SEPARATE, PURE MODULE
 *  This logic is the single most likely place for the UI to silently drift away from the
 *  backend state machine: if the console offers "发货" on an order that cannot ship, the
 *  operator sees a button that always fails, and nobody notices until a demo. Keeping it
 *  pure and unit-tested means the drift shows up as a red test instead of a broken button.
 *
 * STATE FACTS THIS ENCODES (all from the frozen model, not invented):
 *  - `OrderStatus`:    PENDING_PAYMENT | PROCESSING | COMPLETED | CANCELLED | CLOSED
 *  - `PaymentStatus`:  UNPAID | PAYING | PAID | PARTIAL_REFUNDED | REFUNDED
 *  - `FulfillmentStatus`: UNFULFILLED | PARTIAL_SHIPPED | SHIPPED | DELIVERED
 *  - `AfterSaleStatus`:  NONE | PROCESSING | PARTIAL_REFUNDED | REFUNDED
 *  - §31: payment success moves PENDING_PAYMENT -> PROCESSING, and **shipping NEVER changes
 *    order_status**. So "has it shipped?" must be read from `fulfillment_status`, never
 *    inferred from `order_status`.
 *  - §32–§34: cancel is a buyer/merchant intent with its own guard; confirm-receipt closes
 *    the order.
 *
 * THE BACKEND STILL DECIDES. Every flag below only controls what the UI OFFERS. Each
 * corresponding endpoint re-validates and may answer `ORDER_STATE_INVALID` (50 004),
 * `ORDER_NOT_CANCELLABLE` (50 010), `ORDER_NOT_CONFIRMABLE` (50 011),
 * `FULFILLMENT_STATE_INVALID` (70 002) or `FULFILLMENT_ALREADY_SHIPPED` (70 003). A `403`
 * is also possible and must be handled (`onConsoleForbidden` in the views).
 */

import type {
  AfterSaleStatus,
  FulfillmentStatus,
  Order,
  OrderStatus,
  PaymentStatus,
} from '@/types/domain'

export type OrderAction = 'cancel' | 'confirmReceipt' | 'ship' | 'refund' | 'viewDetail'

export interface OrderActionFlags {
  cancel: boolean
  confirmReceipt: boolean
  ship: boolean
  refund: boolean
  viewDetail: boolean
}

/** Cancellable while nothing has left the warehouse and refunds have not started. */
export function canCancelOrder(order: Pick<Order, 'status' | 'fulfillment_status' | 'after_sale_status'>): boolean {
  const notShipped = order.fulfillment_status === 'UNFULFILLED'
  const activeStatus: OrderStatus[] = ['PENDING_PAYMENT', 'PROCESSING']
  const noRefundYet: AfterSaleStatus[] = ['NONE', 'PROCESSING']
  return activeStatus.includes(order.status) && notShipped && noRefundYet.includes(order.after_sale_status)
}

/**
 * Confirm-receipt requires something to actually be in transit. DELIVERED is excluded
 * because the receipt is already confirmed; COMPLETED orders are closed.
 *
 * NOTE: the order must also be PROCESSING (§31 puts paid orders there), so a
 * PENDING_PAYMENT order can never be received.
 */
export function canConfirmReceipt(
  order: Pick<Order, 'status' | 'fulfillment_status'>,
): boolean {
  const shippedStates: FulfillmentStatus[] = ['SHIPPED', 'PARTIAL_SHIPPED']
  return order.status === 'PROCESSING' && shippedStates.includes(order.fulfillment_status)
}

/**
 * Ship requires a paid, in-progress order that is not fully fulfilled, AND the presence of
 * an unshipped fulfillment id.
 *
 * WHY the id is required: the frozen task endpoint is `POST /fulfillments/{id}/ship`, i.e.
 * it is FULFILLMENT-centric, not order-centric (see PROJECT_BASELINE.yaml
 * `task_endpoints`). Without a fulfillment id there is no URL to call, so offering the
 * button would guarantee a dead action. The views obtain the id from the order's
 * `shipments`/fulfillments payload and pass it here.
 */
export function canShipOrder(
  order: Pick<Order, 'status' | 'payment_status' | 'fulfillment_status'>,
  unshippedFulfillmentId?: string,
): boolean {
  const paidStates: PaymentStatus[] = ['PAID', 'PARTIAL_REFUNDED']
  const notFullyShipped: FulfillmentStatus[] = ['UNFULFILLED', 'PARTIAL_SHIPPED']
  return (
    order.status === 'PROCESSING' &&
    paidStates.includes(order.payment_status) &&
    notFullyShipped.includes(order.fulfillment_status) &&
    Boolean(unshippedFulfillmentId)
  )
}

/** A refund can still be issued while money remains un-refunded on a paid order. */
export function canRefundOrder(order: Pick<Order, 'refundable_amount' | 'payment_status'>): boolean {
  const refundableStates: PaymentStatus[] = ['PAID', 'PARTIAL_REFUNDED']
  return refundableStates.includes(order.payment_status) && order.refundable_amount > 0
}

/** All row actions for one order, as booleans the table cell can bind to. */
export function orderActionFlags(
  order: Pick<
    Order,
    'status' | 'payment_status' | 'fulfillment_status' | 'after_sale_status' | 'refundable_amount'
  >,
  unshippedFulfillmentId?: string,
): OrderActionFlags {
  return {
    cancel: canCancelOrder(order),
    confirmReceipt: canConfirmReceipt(order),
    ship: canShipOrder(order, unshippedFulfillmentId),
    refund: canRefundOrder(order),
    // Detail is always available: inspecting an order is never a state transition.
    viewDetail: true,
  }
}

/** Human reason an action is unavailable — surfaced as a tooltip, never as a hidden rule. */
export function orderActionBlockedReason(
  action: Exclude<OrderAction, 'viewDetail'>,
  order: Pick<
    Order,
    'status' | 'payment_status' | 'fulfillment_status' | 'after_sale_status' | 'refundable_amount'
  >,
  unshippedFulfillmentId?: string,
): string {
  switch (action) {
    case 'cancel':
      if (order.fulfillment_status !== 'UNFULFILLED') return '商品已发货，无法取消'
      if (order.after_sale_status === 'REFUNDED' || order.after_sale_status === 'PARTIAL_REFUNDED') {
        return '订单已发生退款，无法取消'
      }
      return '当前订单状态不允许取消'
    case 'confirmReceipt':
      if (order.status === 'PENDING_PAYMENT' || order.status === 'CLOSED' || order.status === 'CANCELLED') {
        return '订单未进入待收货阶段'
      }
      return '商品尚未发货，无法确认收货'
    case 'ship':
      if (order.payment_status === 'UNPAID' || order.payment_status === 'PAYING') return '订单尚未支付，不能发货'
      if (order.fulfillment_status === 'SHIPPED' || order.fulfillment_status === 'DELIVERED') {
        return '订单已发货'
      }
      if (!unshippedFulfillmentId) return '缺少待发货的履约单'
      return '当前订单状态不允许发货'
    case 'refund':
      if (order.refundable_amount <= 0) return '没有可退余额'
      return '订单未支付或已全额退款'
    default:
      return '当前状态不允许该操作'
  }
}
