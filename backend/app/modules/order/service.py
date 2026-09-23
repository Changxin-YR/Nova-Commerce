"""``OrderService`` - order use cases, and the only place their transactions end.

Spec references:
    §17  controllers are thin: parse, authenticate, call, map
    §31  the four status axes are independent; shipping never moves ``order_status``
    §36  every top-level transition appends exactly one ``order_status_logs`` row
    §37  the price comes from :class:`PricingService`, never from here
    §109 IDOR: a stranger's order is ``ORDER_NOT_FOUND (50003)``, never 403
    §112 INV-006, INV-014, INV-015
    PHASE4_DESIGN §8 (the frozen service surface)

## Where the transactions are

Two paths own a commit, and for the same reason: they *change* something.

* :meth:`create_order` delegates to :class:`CreateOrderWorkflow`, which owns its
  transaction (one commit, or nothing at all - §49).
* :meth:`cancel` and :meth:`confirm_receipt` are small enough to own theirs directly:
  lock the order ``FOR UPDATE``, guard the transition, write, commit.

Everything else - preview and the four read paths - performs **no** write and has no
transaction to manage. That is what lets them run concurrently with a create without
touching it.

## Ownership is applied in the query, not after the load

Every customer-facing method filters by ``user_id`` **in the SQL**. §109 is explicit
that a consumer asking for someone else's order must get ``ORDER_NOT_FOUND (50003)``
and never 403: distinguishing "not yours" from "does not exist" turns the endpoint into
an existence oracle. A post-load ownership check is one early ``return`` away from
leaking that distinction, so the foreign row is never fetched at all (see
:meth:`OrderRepository.get_by_order_no`).

## Why cancel and confirm-receipt are not one "update status" method

They differ in everything that matters: what they do to stock, which error code they
answer, and which fields they stamp. A shared ``set_status(order_no, status)`` would be
the natural place to forget the stock release, and it would let a caller reach
``CANCELLED`` without the compensating ledger entry. Two methods, two guards, one
transition table.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.errors import OrderNotFoundError, PermissionDeniedError, ValidationError
from app.core.logging import get_logger
from app.modules.identity.enums import DataScope, PermissionCode
from app.modules.identity.service import AddressService, Principal
from app.modules.inventory.enums import ReferenceType
from app.modules.inventory.service import InventoryService
from app.modules.order.enums import (
    FULFILLMENT_STATUSES,
    ORDER_STATUSES,
    PAYMENT_STATUSES,
    OrderStatus,
)
from app.modules.order.models import Order
from app.modules.order.repository import OrderRepository, OrderStatusLogRepository
from app.modules.order.state_machine import OrderStateMachine
from app.modules.order.workflow import (
    CreateOrderResult,
    CreateOrderWorkflow,
    OrderLineInput,
    PricingRules,
    load_priced_lines,
    log_operator_type,
    merge_order_lines,
    movement_operator,
    order_release_movement_key,
    validate_coupon_input,
)
from app.modules.pricing import CartPrice, PricingService
from app.shared.db.base import utc_now

logger = get_logger(__name__)

__all__ = ["OrderPage", "OrderService"]

#: Server-owned wording for ``cancel_reason`` when the caller supplies none (§6: the
#: client cannot infer the reason, so the server must author it).
DEFAULT_CANCEL_REASON = "cancelled by customer"

#: The matching default for confirm-receipt's status-log reason.
CONFIRM_RECEIPT_REASON = "receipt confirmed by customer"

#: Reason text recorded in the inventory ledger on release.
CANCEL_RELEASE_REASON = "order cancelled"


@dataclass(frozen=True, slots=True)
class OrderPage:
    """One page of orders plus the unpaginated total.

    A small type rather than a bare tuple so the API layer cannot accidentally unpack
    it in the wrong order - ``(rows, total)`` reversed is a valid-looking pair, and the
    resulting bug is a list that renders with the wrong page count.
    """

    rows: tuple[Order, ...]
    total: int


class OrderService:
    """Preview, create, cancel, confirm-receipt and the four read paths."""

    def __init__(self, session: Session, pricing: PricingService | None = None) -> None:
        self._session = session
        self._pricing = pricing or PricingService()
        self._orders = OrderRepository(session)
        self._logs = OrderStatusLogRepository(session)
        self._inventory = InventoryService(session)
        self._addresses = AddressService(session)

    # ------------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------------
    def preview(
        self,
        *,
        principal: Principal,
        items: Sequence[OrderLineInput],
        address_id: int | None = None,
        coupon_id: int | None = None,
        pricing_rules: PricingRules | None = None,
    ) -> CartPrice:
        """Price a selection **without** creating anything (§29, §37, §14.2).

        Returns the authority's :class:`CartPrice` rather than a response model, so the
        API layer maps it exactly once
        (:func:`app.modules.order.serializers.to_preview`) and there cannot be a second,
        subtly different mapping for the create path to diverge from.

        ``pricing_rules`` is the internal seam (PHASE4_DESIGN §7): always ``None`` from
        HTTP, and used by tests to exercise the allocation arithmetic through the same
        code path the real endpoint uses.

        The address, when supplied, is validated as **the caller's own**. It does not
        affect the V1 price at all (shipping is free, §14.4), but §14.2 requires it to
        belong to the caller and a preview is the cheapest possible moment to say so -
        the alternative is telling the customer only after they have committed.
        """
        lines = merge_order_lines(items)
        if address_id is not None:
            # `AddressService.get` filters by user_id in the query, so a stranger's
            # address id is ADDRESS_NOT_FOUND - never a 403 that would confirm the id
            # exists (§109 IDOR).
            self._addresses.get(user_id=principal.user_id, address_id=address_id)

        rules = pricing_rules or PricingRules()
        validate_coupon_input(coupon_id=coupon_id, rules=rules)

        priced_lines, _merchant_id = load_priced_lines(self._session, lines)
        return self._pricing.calculate_cart_price(
            priced_lines,
            promotion=rules.promotion,
            coupon=rules.coupon,
            shipping_policy=None,
        )

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------
    def create_order(
        self,
        *,
        principal: Principal,
        items: Sequence[OrderLineInput],
        address_id: int,
        client_request_id: str,
        idempotency_key: str,
        coupon_id: int | None = None,
        remark: str | None = None,
        pricing_rules: PricingRules | None = None,
    ) -> CreateOrderResult:
        """Create the order, or replay the one this key already created.

        Delegated in full to :class:`CreateOrderWorkflow`, which owns the transaction.
        The service deliberately decides nothing here: a decision made outside that
        transaction is a decision made before the locks (§27), which is the one thing
        the ordering rule forbids.
        """
        return CreateOrderWorkflow(self._session, self._pricing).execute(
            principal=principal,
            items=items,
            address_id=address_id,
            client_request_id=client_request_id,
            idempotency_key=idempotency_key,
            coupon_id=coupon_id,
            remark=remark,
            pricing_rules=pricing_rules,
        )

    # ------------------------------------------------------------------
    # Customer transitions
    # ------------------------------------------------------------------
    def cancel(self, *, principal: Principal, order_no: str, reason: str | None = None) -> Order:
        """``PENDING_PAYMENT -> CANCELLED``, releasing the reserved stock (§14.5).

        Locks the order ``FOR UPDATE`` first, then guards, then releases - the §27 rule
        again, one level up. Two concurrent cancels of the same order would otherwise
        both pass the guard and both release stock, double-crediting availability. The
        row lock is what makes the second one see ``CANCELLED`` and answer
        ``ORDER_NOT_CANCELLABLE (50010)``.

        Stock release uses ``order-release:{order_no}:{sku_id}``, which is both the
        ledger's duplicate guard (INV-003) and the reason a retried release after a
        partial failure cannot return the same unit to availability twice.

        **Expiry is deliberately not checked.** An order whose ``expires_at`` has passed
        while its reconciliation (Phase 6) has not yet run is still cancellable, and
        allowing it is the better outcome: refusing would leave stock locked with no path
        for the customer to release it. ``ORDER_ALREADY_EXPIRED (50005)`` belongs to the
        payment path, where paying an expired order is the actual problem.
        """
        order = self._orders.get_by_order_no_for_update(order_no, user_id=principal.user_id)
        if order is None:
            # The same answer whether the order does not exist or belongs to somebody
            # else (§109 IDOR).
            raise OrderNotFoundError("order not found", context={"order_no": order_no})

        # 50010 for every refused transition, whatever state the order was in.
        OrderStateMachine.to_cancelled(order.order_status)

        self._release_reserved_stock(principal=principal, order=order)

        order.order_status = OrderStatus.CANCELLED.value
        order.cancel_reason = reason or DEFAULT_CANCEL_REASON
        order.cancelled_at = utc_now()
        order.version = (order.version or 0) + 1

        self._logs.append(
            order=order,
            from_status=OrderStatus.PENDING_PAYMENT,
            to_status=OrderStatus.CANCELLED,
            operator_type=log_operator_type(principal),
            operator_id=principal.user_id,
            reason=order.cancel_reason,
            trace_id=None,
        )
        self._session.commit()

        logger.info("order cancelled", order_no=order.order_no, user_id=principal.user_id)
        return order

    def confirm_receipt(self, *, principal: Principal, order_no: str) -> Order:
        """``PROCESSING -> COMPLETED`` - a receipt, not a delivery (§14.5).

        Explicitly does **not** touch ``fulfillment_status``. §31 keeps the four axes
        independent, and a "confirm receipt" that also marked the order DELIVERED would
        be exactly the state-machine collapse the spec forbids: the carrier delivered the
        goods, and the customer's confirmation is a different fact.
        """
        order = self._orders.get_by_order_no_for_update(order_no, user_id=principal.user_id)
        if order is None:
            raise OrderNotFoundError("order not found", context={"order_no": order_no})

        # 50011 for every refused transition.
        OrderStateMachine.to_completed(order.order_status)

        order.order_status = OrderStatus.COMPLETED.value
        order.completed_at = utc_now()
        order.version = (order.version or 0) + 1

        self._logs.append(
            order=order,
            from_status=OrderStatus.PROCESSING,
            to_status=OrderStatus.COMPLETED,
            operator_type=log_operator_type(principal),
            operator_id=principal.user_id,
            reason=CONFIRM_RECEIPT_REASON,
            trace_id=None,
        )
        self._session.commit()

        logger.info("order receipt confirmed", order_no=order.order_no, user_id=principal.user_id)
        return order

    # ------------------------------------------------------------------
    # Customer reads
    # ------------------------------------------------------------------
    def get_customer_order(self, *, principal: Principal, order_no: str) -> Order:
        """One of the caller's own orders, or 50003. Never 403."""
        order = self._orders.get_by_order_no(order_no, user_id=principal.user_id)
        if order is None:
            raise OrderNotFoundError("order not found", context={"order_no": order_no})
        return order

    def list_customer_orders(
        self,
        *,
        principal: Principal,
        order_status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> OrderPage:
        """The caller's own orders, newest first (§14.6).

        The status filter is validated against the frozen vocabulary rather than passed
        through. An unknown value is a ``ValidationError``, not an empty page: silently
        returning nothing for ``?order_status=SHIPPED`` would tell a customer they have
        no orders in the state they meant to filter by - and ``SHIPPED`` is exactly the
        plausible mistake here, because it is a *fulfillment* status and not an order
        status (§31).
        """
        _validate_status_filter(order_status, ORDER_STATUSES, "order_status")
        rows, total = self._orders.list_customer_orders(
            user_id=principal.user_id,
            order_status=order_status,
            page=page,
            page_size=page_size,
        )
        return OrderPage(rows=tuple(rows), total=total)

    # ------------------------------------------------------------------
    # Console reads
    # ------------------------------------------------------------------
    def get_admin_order(self, *, principal: Principal, order_no: str) -> Order:
        """One order for the console, scoped to the caller's merchant.

        The permission is checked **here** rather than only in the controller. §8 puts it
        on the service, and it matters beyond FastAPI: Phase 9's tool gateway will call
        services directly, and a check that lives only in a route decorator is a check a
        non-HTTP caller skips.
        """
        principal.require_permission(PermissionCode.ORDER_READ.value)
        order = self._orders.get_by_order_no(order_no, merchant_id=_merchant_filter(principal))
        if order is None:
            # Not-found rather than 403, for the same reason as the customer path: the
            # merchant filter is applied in the query, so a foreign row is never fetched
            # and therefore cannot be confirmed to exist.
            raise OrderNotFoundError("order not found", context={"order_no": order_no})
        return order

    def list_admin_orders(
        self,
        *,
        principal: Principal,
        order_status: str | None = None,
        payment_status: str | None = None,
        fulfillment_status: str | None = None,
        order_no: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> OrderPage:
        """The console list, with the four frozen filters (§14.6).

        Every filter is validated against **its own** vocabulary, so a filter that can
        never match is refused instead of being answered with an empty page. That is the
        same mistake as above and the console is where it costs the most: an operator who
        filters ``payment_status=PAID_AND_REFUNDED`` and sees nothing concludes there are
        no such orders, and acts on that conclusion.
        """
        principal.require_permission(PermissionCode.ORDER_READ.value)
        _validate_status_filter(order_status, ORDER_STATUSES, "order_status")
        _validate_status_filter(payment_status, PAYMENT_STATUSES, "payment_status")
        _validate_status_filter(fulfillment_status, FULFILLMENT_STATUSES, "fulfillment_status")
        if order_no is not None and not order_no.strip():
            raise ValidationError("order_no filter must not be blank")

        rows, total = self._orders.list_admin_orders(
            merchant_id=_merchant_filter(principal),
            order_status=order_status,
            payment_status=payment_status,
            fulfillment_status=fulfillment_status,
            order_no=order_no,
            page=page,
            page_size=page_size,
        )
        return OrderPage(rows=tuple(rows), total=total)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _release_reserved_stock(self, *, principal: Principal, order: Order) -> None:
        """Return every line's reservation to available stock.

        Each release targets the ``warehouse_id`` **stored on the line** - "the row that
        was locked" (§4.2) - rather than re-resolving the default warehouse. Re-resolving
        would be right only while there is exactly one warehouse, and would silently
        release the wrong row the day there is not.

        Sorted by ``sku_id`` for the same lock-ordering reason the reserve path is: a
        cancel running against a concurrent multi-line create must acquire rows in one
        agreed order.
        """
        operator_type, operator_id = movement_operator(principal)
        for item in sorted(order.items, key=lambda row: row.sku_id):
            self._inventory.release(
                sku_id=item.sku_id,
                quantity=item.quantity,
                idempotency_key=order_release_movement_key(
                    order_no=order.order_no, sku_id=item.sku_id
                ),
                warehouse_id=item.warehouse_id,
                reference_type=ReferenceType.ORDER,
                reference_id=order.id,
                operator_type=operator_type,
                operator_id=operator_id,
                reason=CANCEL_RELEASE_REASON,
            )


def _merchant_filter(principal: Principal) -> int | None:
    """The merchant id a console read is scoped to.

    ``DataScope.ALL`` means "every merchant", so it passes ``None`` (no filter).
    Anything else must narrow to the caller's own merchant, and a staff principal
    without one cannot be scoped at all - returning everything would turn a misconfigured
    account into a cross-tenant read, so it is refused instead.
    """
    if principal.data_scope is DataScope.ALL:
        return None
    if principal.merchant_id is None:
        raise PermissionDeniedError("this staff account is not attached to a merchant")
    return principal.merchant_id


def _validate_status_filter(value: str | None, allowed: tuple[str, ...], field: str) -> None:
    """Refuse a filter value outside the frozen vocabulary for that field.

    ``None`` is "not requested" and passes through; everything else must be an exact
    member of the vocabulary the column's CHECK constraint enforces, so a filter cannot
    express a state the database could never hold.
    """
    if value is None:
        return
    if value not in allowed:
        raise ValidationError(
            f"unknown {field} filter",
            context={field: value, "allowed": list(allowed)},
        )
