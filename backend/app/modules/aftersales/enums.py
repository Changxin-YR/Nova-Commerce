"""After-sales domain vocabularies - PHASE5_DESIGN 搂4.3.

    AfterSaleType, AfterSaleClaimStatus, RefundStatus, Carrier

## Three vocabularies that are deliberately *not* the order's

``orders.after_sale_status`` (``NONE/PROCESSING/PARTIAL_REFUNDED/REFUNDED``) is the
**order's rollup**. It lives in :mod:`app.modules.order.enums` and is written by
exactly one code path - the refund workflow. The three enums below are different
objects:

* :class:`AfterSaleClaimStatus` is the **claim's** own lifecycle. A claim that is
  ``APPROVED`` has changed nothing about the order; a claim that reaches
  ``COMPLETED`` is a claim whose own approved amount has been fully refunded.
* :class:`RefundStatus` is the **money movement's** status, and it is what the
  frontend's frozen ``RefundRecord.status`` union declares.
* :class:`AfterSaleType` decides whether goods come back, which is the only thing
  that may move stock (PHASE5_DESIGN 搂6.2 step 6).

Writing any one of them as a side effect of another is the defect 搂4.4 exists to
prevent: **a claim never writes ``orders.after_sale_status``**, and approving a
claim is not a refund.

## Why ``StrEnum``

The values are stored verbatim in ``VARCHAR`` columns that carry hand-written
``CHECK (col IN (...))`` constraints derived from these same tuples (see
``_sql_vocabulary`` in :mod:`app.modules.order.models`). ``StrEnum`` keeps
``AfterSaleType.REFUND_ONLY == "REFUND_ONLY"`` true, so a row read back from the
database as a plain string compares equal without a conversion step - and the
conversion step is exactly the kind of small oversight that turns a guard into a
no-op.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AFTER_SALE_CLAIM_STATUSES",
    "AFTER_SALE_TYPES",
    "CARRIER_CODES",
    "CARRIER_NAMES",
    "REFUND_STATUSES",
    "AfterSaleClaimStatus",
    "AfterSaleType",
    "RefundStatus",
    "is_carrier_code",
]


class AfterSaleType(StrEnum):
    """What the customer is asking for.

    The distinction is not cosmetic and not a UI label: ``RETURN_REFUND`` means
    goods physically come back, so the refund path appends a ``RETURN_IN`` stock
    movement. ``REFUND_ONLY`` means the customer keeps the goods, and crediting
    stock for them would inflate availability until the next stock count found it
    (PHASE5_DESIGN 搂6.2 step 6).
    """

    REFUND_ONLY = "REFUND_ONLY"
    RETURN_REFUND = "RETURN_REFUND"


class AfterSaleClaimStatus(StrEnum):
    """The claim's own lifecycle (PHASE5_DESIGN 搂4.3).

    ``PENDING -> APPROVED -> COMPLETED`` is the happy path; ``REJECTED`` and
    ``CANCELLED`` are terminal and reachable only from ``PENDING``. ``COMPLETED``
    is reached by the refund workflow when the claim's own
    ``refunded_amount`` equals its ``approved_amount`` - not when the *order*
    reaches ``REFUNDED``, because a claim worth 30 yuan on a 100 yuan order
    completes while the order is only ``PARTIAL_REFUNDED``.

    ``APPROVED -> COMPLETED`` therefore requires an intervening refund, and that is
    why the refund workflow accepts both ``APPROVED`` and the
    ``PARTIAL_REFUNDED`` claim status - a claim refunded in two instalments is
    still ``APPROVED`` after the first one and has to be refundable again.
    """

    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"

    @property
    def is_terminal(self) -> bool:
        """``REJECTED``/``CANCELLED`` are final; ``COMPLETED`` means "fully refunded"."""
        return self in {
            AfterSaleClaimStatus.REJECTED,
            AfterSaleClaimStatus.CANCELLED,
            AfterSaleClaimStatus.COMPLETED,
        }


class RefundStatus(StrEnum):
    """The money movement's status. Matches the frontend's frozen union exactly.

    V1 has **no asynchronous refund**: the disbursement is executed inside the same
    transaction that writes the row, so the row is written straight to
    ``SUCCEEDED``. A ``PENDING`` row left behind for a worker to finish would be a
    lie the customer can see - the console would show "refund pending" for money
    that was never sent, and nothing would ever complete it. ``FAILED`` exists for
    the same reason it does on the payment record: a recorded failure is
    distinguishable from a movement that never happened.
    """

    PENDING = "PENDING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


#: Vocabularies in the exact order the migration's ``CHECK`` constraints list them.
AFTER_SALE_TYPES: tuple[str, ...] = tuple(member.value for member in AfterSaleType)
AFTER_SALE_CLAIM_STATUSES: tuple[str, ...] = tuple(member.value for member in AfterSaleClaimStatus)
REFUND_STATUSES: tuple[str, ...] = tuple(member.value for member in RefundStatus)


#: The carrier allowlist is **owned by the fulfillment module** and re-exported here, not
#: re-declared. PHASE5_DESIGN section 4.2 puts the vocabulary there ("``carrier`` ... is
#: validated against a small allowlist in the fulfillment enums"), and section 3's comment on
#: ``fulfillment/enums.py`` says "FulfillmentStatus re-export + Carrier vocabulary" - the same
#: shape as ``FulfillmentStatus`` being imported from ``order/enums.py`` rather than declared
#: twice.
#:
#: The reason to insist on one declaration rather than two identical ones: ``fulfillments``
#: carries a hand-written ``CHECK (carrier IS NULL OR carrier IN (...))`` derived from the
#: fulfillment vocabulary, while ``after_sales`` carries the **return** parcel's carrier. A
#: second tuple that must stay equal forever, with nothing enforcing it, is precisely how a
#: validator accepts a code that MySQL then rejects with errno 3819 at write time - drift that
#: no test catches until somebody returns a parcel with a newly-added carrier.
#:
#: A return parcel travels with the same carriers as an outbound one, so the two really are the
#: same vocabulary; there was never a reason for a local copy.
from app.modules.fulfillment.enums import (  # noqa: E402 - grouped with the re-export, not the module imports
    CARRIER_CODES,
    CARRIER_NAMES,
    Carrier,
    is_valid_carrier as is_carrier_code,
)

__all__ += ["Carrier"]
