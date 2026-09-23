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
  OrderStatus,
  PaymentStatus,
} from '@/types/domain'
import type { Fulfillment } from '@/types/frozen-contract'

/**
 * The order fields every predicate below reads.
 *
 * NOTE the field is `order_status`, not `status` (API_CONTRACT.md §6). The rename is not
 * cosmetic: `order.status` on a frozen payload is `undefined`, and an `undefined` status makes
 * every `includes()` check below return `false` — the console would hide EVERY action and nothing
 * would throw.
 */
export interface OrderStateLike {
  order_status: OrderStatus
  payment_status: PaymentStatus
  fulfillment_status: FulfillmentStatus
  after_sale_status: AfterSaleStatus
  /** Integer minor units actually paid. */
  paid_amount: number
  /** Integer minor units already refunded. */
  refunded_amount: number
  /**
   * SERVER-OWNED remaining refundable amount (`OrderDetail`, §11 addendum).
   *
   * Optional in the TYPE only because the bridge below still has to cope with a payload produced
   * before the backend order module lands; every real `OrderDetail` carries it.
   */
  refundable_amount?: number | null
}

export type OrderAction = 'cancel' | 'confirmReceipt' | 'ship' | 'refund' | 'viewDetail'

export interface OrderActionFlags {
  cancel: boolean
  confirmReceipt: boolean
  ship: boolean
  refund: boolean
  viewDetail: boolean
}

/**
 * Remaining refundable amount, in integer minor units — **read from the server**.
 *
 * `API_CONTRACT.md` §11 made `refundable_amount` server-owned, and the reason is worth stating where
 * the value is consumed: this figure decides whether a refund CONTROL IS RENDERED AT ALL, so a
 * client-side derivation would put an accounting rule (INV-005) in the UI. §15 forbids the client
 * from being the authority on it.
 *
 * WHY THERE IS STILL A DERIVATION HERE: the order module does not exist yet (`app/modules/*`), so
 * until it lands a real payload has no `refundable_amount`. Without the fallback, `undefined > 0` is
 * `false` and EVERY refund affordance would silently disappear — a dead UI is worse than a clearly
 * marked transition, because a dead UI gets debugged while a silent fallback gets forgotten.
 *
 * @deprecated TRANSITIONAL BRIDGE. This arithmetic must be removed once the backend ships
 *   `OrderDetail.refundable_amount`. It lives in the DOMAIN layer, and a second source of truth for a
 *   money figure is exactly the shape §15 forbids — so it is tolerated only as a labelled, noisy,
 *   temporary bridge. See `TODO(phase-5)` at the fallback branch below.
 */
export function refundableAmount(
  order: Pick<OrderStateLike, 'paid_amount' | 'refunded_amount'> & {
    refundable_amount?: number | null
  },
): number {
  const fromServer = order.refundable_amount
  if (typeof fromServer === 'number' && Number.isFinite(fromServer)) {
    return Math.max(0, fromServer)
  }

  // TODO(phase-5): DELETE this branch AND its spec cases once Phase 5 lands
  //   `OrderDetail.refundable_amount`.
  // Removal trigger: a real `GET /orders/{admin/{order_no}}` response carries the field, so this
  //   branch becomes unreachable in production and the domain layer goes back to having ONE source
  //   of truth for a money figure. Owner: the backend implementer completing Phase 5 (the
  //   definition of done for Phase 5 in HANDOFF.md names this deletion explicitly).
  warnBridgeHit()
  return Math.max(0, order.paid_amount - order.refunded_amount)
}

/**
 * Test seam. The warn-once latch is module state, so the tests reset it to assert the warning fires
 * exactly once per session rather than on every render pass.
 */
export const refundableBridgeState = { warned: false }

/**
 * A silent fallback becomes a PERMANENT fallback. The bridge therefore announces itself in
 * development, naming the missing field and the condition that retires it, so it cannot be forgotten
 * — the point being that a bridge which speaks up is a bridge that gets deleted.
 *
 * `import.meta.env.DEV` keeps this out of production builds entirely: the warning is a developer
 * signal, not user-facing noise. Warns ONCE per session because this function is called from
 * computed values and templates, and a per-call warning would flood the console into uselessness.
 */
function warnBridgeHit(): void {
  if (!import.meta.env.DEV || refundableBridgeState.warned) return
  refundableBridgeState.warned = true
  console.warn(
    '[nexora] refundableAmount() fell back to paid_amount - refunded_amount.\n' +
      '  Missing field: OrderDetail.refundable_amount (server-owned, API_CONTRACT.md §11).\n' +
      '  This is a TRANSITIONAL BRIDGE in the domain layer and a second source of truth for a money\n' +
      '  figure that gates whether refund controls render (INV-005 / §15).\n' +
      '  Removal trigger: delete the fallback + its spec cases once Phase 5 ships the field.',
  )
}

/** Cancellable while nothing has left the warehouse and refunds have not started. */
export function canCancelOrder(
  order: Pick<OrderStateLike, 'order_status' | 'fulfillment_status' | 'after_sale_status'>,
): boolean {
  const notShipped = order.fulfillment_status === 'UNFULFILLED'
  const activeStatus: OrderStatus[] = ['PENDING_PAYMENT', 'PROCESSING']
  const noRefundYet: AfterSaleStatus[] = ['NONE', 'PROCESSING']
  return (
    activeStatus.includes(order.order_status) &&
    notShipped &&
    noRefundYet.includes(order.after_sale_status)
  )
}

/**
 * Confirm-receipt requires something to actually be in transit. DELIVERED is excluded because the
 * receipt is already confirmed; COMPLETED orders are closed.
 *
 * NOTE: the order must also be PROCESSING (§31 puts paid orders there), so a PENDING_PAYMENT order
 * can never be received.
 */
export function canConfirmReceipt(
  order: Pick<OrderStateLike, 'order_status' | 'fulfillment_status'>,
): boolean {
  const shippedStates: FulfillmentStatus[] = ['SHIPPED', 'PARTIAL_SHIPPED']
  return order.order_status === 'PROCESSING' && shippedStates.includes(order.fulfillment_status)
}

/**
 * A fulfillment is still shippable when the SERVER has not stamped a carrier on it.
 *
 * THE RULE THE CAPTAIN CALLED OUT, and why it matters: `carrier` / `tracking_no` are `null` until
 * shipped (API_CONTRACT.md §5), so `carrier === null` is the server's own statement that the
 * package is outstanding. Re-deriving "outstanding" from `fulfillment_status` would be a second,
 * weaker copy of state the server already told us, and the two can disagree — a package that
 * already carries a tracking number could still read UNFULFILLED, and the UI would offer an action
 * the server rejects with `FULFILLMENT_ALREADY_SHIPPED` (70 003).
 */
export function isUnshippedFulfillment(fulfillment: Pick<Fulfillment, 'carrier'>): boolean {
  return fulfillment.carrier === null
}

/**
 * Pick the fulfillment a "ship" click should act on: the first one with no carrier yet.
 *
 * Returns `undefined` when nothing is outstanding, which is a REAL state (an order may be fully
 * shipped while a stale row is still on screen) and must read as "nothing to ship" rather than as
 * a broken button.
 */
export function findUnshippedFulfillment(
  fulfillments: readonly Pick<Fulfillment, 'id' | 'carrier'>[] | undefined,
): Fulfillment['id'] | undefined {
  return fulfillments?.find((f) => isUnshippedFulfillment(f))?.id
}

/**
 * Ship requires a paid, in-progress order that is not fully fulfilled, AND a fulfillment the
 * server has not shipped yet.
 *
 * WHY a `Fulfillment` and not an id: the task endpoint is `POST /fulfillments/{id}/ship` — keyed by
 * fulfillment, and the id is a NUMBER (API_CONTRACT.md §5). Taking the whole object lets this
 * function read `carrier` for itself instead of trusting a caller's guess about which package is
 * outstanding.
 */
export function canShipOrder(
  order: Pick<OrderStateLike, 'order_status' | 'payment_status' | 'fulfillment_status'>,
  fulfillment?: Pick<Fulfillment, 'carrier'> | null,
): boolean {
  const paidStates: PaymentStatus[] = ['PAID', 'PARTIAL_REFUNDED']
  const notFullyShipped: FulfillmentStatus[] = ['UNFULFILLED', 'PARTIAL_SHIPPED']
  return (
    order.order_status === 'PROCESSING' &&
    paidStates.includes(order.payment_status) &&
    notFullyShipped.includes(order.fulfillment_status) &&
    !!fulfillment &&
    isUnshippedFulfillment(fulfillment)
  )
}

/** A refund can still be issued while money remains un-refunded on a paid order. */
export function canRefundOrder(
  order: Pick<OrderStateLike, 'paid_amount' | 'refunded_amount' | 'payment_status'> & {
    refundable_amount?: number | null
  },
): boolean {
  const refundableStates: PaymentStatus[] = ['PAID', 'PARTIAL_REFUNDED']
  return refundableStates.includes(order.payment_status) && refundableAmount(order) > 0
}

/** All row actions for one order, as booleans the table cell can bind to. */
export function orderActionFlags(
  order: Pick<
    OrderStateLike,
    | 'order_status'
    | 'payment_status'
    | 'fulfillment_status'
    | 'after_sale_status'
    | 'paid_amount'
    | 'refunded_amount'
  >,
  fulfillment?: Pick<Fulfillment, 'carrier'> | null,
): OrderActionFlags {
  return {
    cancel: canCancelOrder(order),
    confirmReceipt: canConfirmReceipt(order),
    ship: canShipOrder(order, fulfillment),
    refund: canRefundOrder(order),
    // Detail is always available: inspecting an order is never a state transition.
    viewDetail: true,
  }
}

/** Human reason an action is unavailable — surfaced as a tooltip, never as a hidden rule. */
export function orderActionBlockedReason(
  action: Exclude<OrderAction, 'viewDetail'>,
  order: Pick<
    OrderStateLike,
    | 'order_status'
    | 'payment_status'
    | 'fulfillment_status'
    | 'after_sale_status'
    | 'paid_amount'
    | 'refunded_amount'
  >,
  fulfillment?: Pick<Fulfillment, 'carrier'> | null,
): string {
  switch (action) {
    case 'cancel':
      if (order.fulfillment_status !== 'UNFULFILLED') return '商品已发货，无法取消'
      if (order.after_sale_status === 'REFUNDED' || order.after_sale_status === 'PARTIAL_REFUNDED') {
        return '订单已发生退款，无法取消'
      }
      return '当前订单状态不允许取消'
    case 'confirmReceipt':
      if (
        order.order_status === 'PENDING_PAYMENT' ||
        order.order_status === 'CLOSED' ||
        order.order_status === 'CANCELLED'
      ) {
        return '订单未进入待收货阶段'
      }
      return '商品尚未发货，无法确认收货'
    case 'ship':
      if (order.payment_status === 'UNPAID' || order.payment_status === 'PAYING') {
        return '订单尚未支付，不能发货'
      }
      if (order.fulfillment_status === 'SHIPPED' || order.fulfillment_status === 'DELIVERED') {
        return '订单已发货'
      }
      if (!fulfillment) return '缺少待发货的履约单'
      // The server already stamped a carrier on this package: it is out the door.
      if (!isUnshippedFulfillment(fulfillment)) return '该履约单已发货'
      return '当前订单状态不允许发货'
    case 'refund':
      if (refundableAmount(order) <= 0) return '没有可退余额'
      return '订单未支付或已全额退款'
    default:
      return '当前状态不允许该操作'
  }
}
