"""The outbox event vocabulary and the payload shape of each event (spec §49).

    OutboxEventType, OutboxAggregateType, the payload builders

## Why the vocabulary lives here and not at the call sites

Three different bounded contexts emit events (order, payment, after-sales). A
consumer that has to know which module wrote a row is a consumer coupled to our
internals, so the *event* vocabulary is one shared, greppable list - and a new
event type is a deliberate edit here rather than a string typed somewhere in a
workflow.

## Why the payloads are built by functions rather than assembled inline

The payload leaves the database: a broker, a log line, possibly a third party.
The discipline that keeps it safe is therefore enforced in one place rather than
trusted at three call sites:

* **non-sensitive only** - identifiers, amounts and statuses. Never a token, a
  signature, a phone number or an address. ``REQ-CON-002`` states the rule for
  idempotency snapshots; an event payload is *more* exposed than a snapshot, so
  the same rule applies with more force (``OutboxWriter.enqueue`` asserts it).
* **stable keys** - a consumer keys off these names, so they are part of the
  contract rather than an implementation detail.

There is deliberately **no timestamp in any payload**: the row's ``created_at``
already *is* the event time, and a second copy of it is one more thing to keep in
sync.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

__all__ = [
    "OUTBOX_AGGREGATE_TYPES",
    "OUTBOX_EVENT_TYPES",
    "OutboxAggregateType",
    "OutboxEventType",
    "order_created_payload",
    "payment_settled_payload",
    "refund_succeeded_payload",
]


class OutboxEventType(StrEnum):
    """What happened. The value is the wire name and is stored verbatim.

    ``refund.succeeded`` is the exact string the ``RefundWorkflow`` seam comment
    names; the other two follow the same ``<aggregate>.<past-tense verb>`` shape
    so a consumer can route on the prefix.
    """

    #: ``CreateOrderWorkflow`` step 9 - the order and its reservation committed.
    ORDER_CREATED = "order.created"
    #: ``PaymentSuccessWorkflow`` step 10 - a verified callback settled a payment.
    PAYMENT_SETTLED = "payment.settled"
    #: ``RefundWorkflow`` step 7 - the money-out row and both caps committed.
    REFUND_SUCCEEDED = "refund.succeeded"


class OutboxAggregateType(StrEnum):
    """What the event is *about* - the aggregate a consumer would look up."""

    ORDER = "order"
    PAYMENT = "payment"
    REFUND = "refund"
    AFTER_SALE = "after_sale"


#: Vocabulary tuples, derived from the enums so a new member cannot be added in
#: Python while a database ``CHECK`` still rejects it.
OUTBOX_EVENT_TYPES: tuple[str, ...] = tuple(member.value for member in OutboxEventType)
OUTBOX_AGGREGATE_TYPES: tuple[str, ...] = tuple(member.value for member in OutboxAggregateType)


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------
def order_created_payload(
    *,
    order_no: str,
    payable_amount: int,
    item_count: int,
) -> dict[str, Any]:
    """Shape of ``order.created``.

    Mirrors the non-sensitive summary ``CreateOrderWorkflow`` already stores as
    its idempotency ``response_snapshot`` (spec §48): the customer's own view of
    the order, with no receiver name, phone or address - those never leave the
    order row.
    """
    return {
        "order_no": order_no,
        "payable_amount": payable_amount,
        "item_count": item_count,
    }


def payment_settled_payload(
    *,
    payment_no: str,
    order_no: str,
    amount: int,
    provider: str,
) -> dict[str, Any]:
    """Shape of ``payment.settled``.

    ``provider`` is the channel name (``MOCK``/``ALIPAY``/``WECHAT``), not the
    provider's event id or signature - a consumer needs to know *who* confirmed
    the money, and the raw delivery stays in ``payment_callbacks``.
    """
    return {
        "payment_no": payment_no,
        "order_no": order_no,
        "amount": amount,
        "provider": provider,
    }


def refund_succeeded_payload(
    *,
    refund_no: str,
    refund_id: int,
    after_sale_no: str,
    order_no: str,
    amount: int,
    order_refunded_amount: int,
    order_paid_amount: int,
    payment_status: str,
    after_sale_status: str,
    claim_status: str,
) -> dict[str, Any]:
    """Shape of ``refund.succeeded`` - the keys ``RefundWorkflow`` step 7 names.

    The seam comment lists these ten fields as the minimum a consumer needs, and
    the field names are taken from it verbatim so the doc and the code cannot
    drift.
    """
    return {
        "refund_no": refund_no,
        "refund_id": refund_id,
        "after_sale_no": after_sale_no,
        "order_no": order_no,
        "amount": amount,
        "order_refunded_amount": order_refunded_amount,
        "order_paid_amount": order_paid_amount,
        "payment_status": payment_status,
        "after_sale_status": after_sale_status,
        "claim_status": claim_status,
    }
