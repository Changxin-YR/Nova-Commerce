"""Order domain vocabularies - spec section 19 (``VARCHAR`` + Python Enum).

    OrderStatus, PaymentStatus, FulfillmentStatus, AfterSaleStatus

## Four independent status axes, and why that is the whole point

The tempting simplification is one ``status`` column covering "where is this
order". That column cannot answer the two questions a support agent actually
asks - *has it been paid?* and *has it shipped?* - because they move
independently: an order can be ``PROCESSING`` (paid, being picked) while
``UNFULFILLED`` (nothing shipped yet), and a ``CANCELLED`` order can sit next to
a ``REFUNDED`` payment status.

Spec section 31 makes the separation a rule rather than a preference:
**shipping never changes ``order_status``**, and the four fields are never
inferred from one another. This module keeps that honest by refusing to express
the derivation anywhere - there is no ``order_status -> payment_status`` mapping
in the codebase, and :data:`ORDER_STATUS_TRANSITIONS` is the *only* place
``order_status`` moves.

## Why ``StrEnum``

The values are stored verbatim in ``VARCHAR`` columns that also carry hand-written
``CHECK (col IN (...))`` constraints, so the Python vocabulary and the migration
vocabulary must be byte-identical. ``StrEnum`` (not ``str, Enum``) makes
``OrderStatus.PENDING_PAYMENT == "PENDING_PAYMENT"`` true, so a value can be
compared against either without a ``.value`` dance and without a silent
mismatch.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AFTER_SALE_STATUSES",
    "FULFILLMENT_STATUSES",
    "OPERATOR_TYPES",
    "ORDER_STATUSES",
    "ORDER_STATUS_TRANSITIONS",
    "PAYMENT_STATUSES",
    "AfterSaleStatus",
    "FulfillmentStatus",
    "OperatorType",
    "OrderStatus",
    "PaymentStatus",
    "is_valid_transition",
]


class OrderStatus(StrEnum):
    """The order's position in the commerce lifecycle (spec section 30).

    Exactly five states in V1. Note what is *absent*: there is no ``SHIPPED`` and
    no ``PAID``. Shipping is a fulfillment fact and payment is a payment fact, so
    neither belongs here - see the module docstring.
    """

    PENDING_PAYMENT = "PENDING_PAYMENT"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    #: Terminal, reached in Phase 6 by the expiry reconciliation: the payment
    #: window closed without a payment. Distinct from ``CANCELLED`` because the
    #: cause is time rather than a person, which matters for reporting.
    CLOSED = "CLOSED"


class PaymentStatus(StrEnum):
    """Money-in state. Written by Phase 5; Phase 4 only ever writes ``UNPAID``."""

    UNPAID = "UNPAID"
    PAYING = "PAYING"
    PAID = "PAID"
    PARTIAL_REFUNDED = "PARTIAL_REFUNDED"
    REFUNDED = "REFUNDED"


class FulfillmentStatus(StrEnum):
    """Goods-out state. Owned by shipping, and never by ``order_status``."""

    UNFULFILLED = "UNFULFILLED"
    PARTIAL_SHIPPED = "PARTIAL_SHIPPED"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"


class AfterSaleStatus(StrEnum):
    """Post-sale state (returns/refunds). Phase 5 writes everything past ``NONE``."""

    NONE = "NONE"
    PROCESSING = "PROCESSING"
    PARTIAL_REFUNDED = "PARTIAL_REFUNDED"
    REFUNDED = "REFUNDED"


class OperatorType(StrEnum):
    """Who caused a row to change (spec section 28).

    Stored on ``order_status_logs`` so the audit trail can distinguish a customer
    cancelling their own order from an agent cancelling it on their behalf -
    which is the difference between a normal event and something worth
    reviewing. ``AGENT``/``MCP``/``WORKER`` are unused in Phase 4 and present
    because the vocabulary is frozen by the spec and widening a ``CHECK``
    constraint later is a migration.
    """

    SYSTEM = "SYSTEM"
    CUSTOMER = "CUSTOMER"
    STAFF = "STAFF"
    AGENT = "AGENT"
    MCP = "MCP"
    WORKER = "WORKER"


#: Vocabulary tuples, in the exact order the migration's ``CHECK`` constraints
#: list them. Derived from the enums rather than re-typed so a new member cannot
#: be added in Python while the database still rejects it.
ORDER_STATUSES: tuple[str, ...] = tuple(member.value for member in OrderStatus)
PAYMENT_STATUSES: tuple[str, ...] = tuple(member.value for member in PaymentStatus)
FULFILLMENT_STATUSES: tuple[str, ...] = tuple(member.value for member in FulfillmentStatus)
AFTER_SALE_STATUSES: tuple[str, ...] = tuple(member.value for member in AfterSaleStatus)
OPERATOR_TYPES: tuple[str, ...] = tuple(member.value for member in OperatorType)


#: The order state machine (spec section 30). Frozen.
#:
#: * create lands here: ``None -> PENDING_PAYMENT``
#: * ``PENDING_PAYMENT -> PROCESSING`` is Phase 5's ``PaymentSuccessWorkflow``
#: * ``PENDING_PAYMENT -> CLOSED`` is Phase 6's expiry reconciliation
#: * ``PENDING_PAYMENT -> CANCELLED`` is the customer cancel endpoint
#: * ``PROCESSING -> COMPLETED`` is ``POST /orders/{order_no}/confirm-receipt``
#: * ``COMPLETED -> CLOSED`` is the post-sale close, Phase 5+
#:
#: ``CANCELLED`` and ``CLOSED`` are terminal: there is no resurrection path, so a
#: "cancelled" order can never quietly become payable again.
ORDER_STATUS_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_PAYMENT: frozenset(
        {OrderStatus.PROCESSING, OrderStatus.CANCELLED, OrderStatus.CLOSED}
    ),
    OrderStatus.PROCESSING: frozenset({OrderStatus.COMPLETED, OrderStatus.CLOSED}),
    OrderStatus.COMPLETED: frozenset({OrderStatus.CLOSED}),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.CLOSED: frozenset(),
}


def is_valid_transition(current: OrderStatus | str, target: OrderStatus | str) -> bool:
    """Whether ``current -> target`` is permitted by the frozen table.

    Accepts raw strings as well as enum members so a row read straight from the
    database (where ``order_status`` is a ``VARCHAR``) can be tested without a
    conversion step at the call site - the conversion is the kind of small
    oversight that turns a guard into a no-op.

    A self-transition is **not** valid: ``PENDING_PAYMENT -> PENDING_PAYMENT``
    would append a status log row that says nothing happened, which makes the
    audit trail harder to read and hides double-submits.
    """
    try:
        source = OrderStatus(current)
        destination = OrderStatus(target)
    except ValueError:
        return False
    return destination in ORDER_STATUS_TRANSITIONS[source]
