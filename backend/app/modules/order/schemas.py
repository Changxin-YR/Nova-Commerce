"""Order request/response schemas - the wire shapes frozen by API_CONTRACT §14.

Spec references:
    §38  ``CreateOrder`` accepts **business inputs only**. A client may not name a
         price. This module is where that rule is enforced at the edge.
    §110 mass assignment: every request model is ``extra="forbid"``, so a body
         carrying ``unit_price``/``payable_amount``/``merchant_id``/``user_id`` is
         **rejected with 422** rather than having the extra key ignored. Ignoring
         it would be worse than rejecting it: the client would believe it had set
         the price and only discover otherwise from the response.
    §2   scalar encodings: money is an integer in minor units, identifiers are
         numbers, enums are SCREAMING_SNAKE strings, timestamps are ISO-8601 UTC
         **with milliseconds**, and ``null`` is explicit rather than absent.
    §14.2 ``items`` carries SKU and quantity only.
    §14.3 ``client_request_id`` is required on create and absent from preview.
    §3   every list endpoint returns ``{items, meta}``, never a bare array.

## ``extra="forbid"`` is the point of this file

The single most important rule in Phase 4 is that the server recomputes every
figure (§38): "a client that can name its own price has bought the shop". The
enforcement is not a comment - it is ``extra="forbid"`` on the request models
plus the absence of any price field from them. A test posts a body containing
``unit_price`` and asserts 422, which is what makes the rule executable rather
than aspirational.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, field_validator

from app.modules.order.enums import (
    AfterSaleStatus,
    FulfillmentStatus,
    OrderStatus,
    PaymentStatus,
)

__all__ = [
    "MAX_ORDER_LINES",
    "CancelOrderRequest",
    "CreateOrderRequest",
    "FulfillmentItemOut",
    "FulfillmentOut",
    "MetaOut",
    "OrderDetailOut",
    "OrderItemOut",
    "OrderLineIn",
    "OrderPreviewItemOut",
    "OrderPreviewOut",
    "OrderPreviewRequest",
    "OrderSummaryOut",
    "UtcTimestamp",
    "iso_millis",
    "page_meta",
]

#: A ceiling on distinct lines, not business policy: it stops a fat-fingered or
#: hostile body from building a thousand-line cart inside one transaction. Each
#: line becomes a row lock, so an unbounded list is an unbounded transaction.
MAX_ORDER_LINES = 100


# ---------------------------------------------------------------------------
# Timestamps: exactly the frozen encoding
# ---------------------------------------------------------------------------
def iso_millis(value: datetime) -> str:
    """``2026-09-22T23:31:07.507Z`` - UTC, three fractional digits, literal ``Z``.

    Pydantic's default JSON encoding for a ``datetime`` is
    ``2026-09-22T23:31:07.507000Z`` (six digits). Both are valid ISO-8601 and both
    parse in JavaScript, so this is not a correctness bug - but §2 freezes the
    encoding as "ISO-8601 UTC **with milliseconds**" and shows a three-digit
    example, while the frontend's own ``Date.prototype.toISOString()`` emits three
    digits in the other direction. Emitting what the contract shows keeps a
    round-tripped value byte-identical to what the client sent.

    A naive value is treated as UTC rather than rejected: ``DateTimeMS`` already
    guarantees aware-UTC on the way out, so the only way to reach this branch is a
    hand-built object, and a silently local time is exactly the failure §2
    forbids. Normalising is the safe reading; raising here would turn a
    serialisation detail into a 500.

    Exported (not private) because the workflow stores a timestamp in the
    idempotency record's ``response_snapshot`` JSON, where Pydantic is not doing the
    encoding for us and a raw ``datetime`` would not serialise at all.
    """
    if value.tzinfo is None:
        aware = value.replace(tzinfo=UTC)
    else:
        aware = value.astimezone(UTC)
    return f"{aware:%Y-%m-%dT%H:%M:%S}.{aware.microsecond // 1000:03d}Z"


#: Use for every wire-facing timestamp (both the required and the nullable form -
#: ``Annotated`` composes with ``| None`` at the field, so one alias suffices).
UtcTimestamp = Annotated[
    datetime,
    PlainSerializer(iso_millis, return_type=str, when_used="json"),
]


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
class OrderLineIn(BaseModel):
    """One requested line: **SKU and quantity only** (§14.2).

    ``quantity`` is bounded above. The bound is a rail against arithmetic inside a
    transaction, not a merchandising rule; the real purchase limit is a per-SKU
    attribute the catalog owns and Phase 4 does not apply.
    """

    model_config = ConfigDict(extra="forbid")

    sku_id: int = Field(gt=0, description="The SKU to buy.")
    quantity: int = Field(gt=0, le=1_000_000, description="Units, positive.")


class OrderPreviewRequest(BaseModel):
    """``POST /api/v1/orders/preview`` (§14.2).

    Same business inputs as create, minus ``client_request_id``: a preview creates
    nothing, so there is nothing for a client-request id to de-duplicate.
    ``address_id`` is optional because the V1 shipping policy is free (§14.4) and
    therefore has no address input; it is accepted now so that enabling a paid
    policy is not a wire change.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[OrderLineIn] = Field(
        min_length=1,
        max_length=MAX_ORDER_LINES,
        description="Requested lines; duplicate sku_ids are merged before pricing.",
    )
    address_id: int | None = Field(default=None, gt=0)
    coupon_id: int | None = Field(
        default=None,
        gt=0,
        description="Accepted as a business input; refused with 90004 until Phase 6 resolves coupons.",
    )
    remark: str | None = Field(default=None, max_length=500)

    @field_validator("items")
    @classmethod
    def _no_line_may_be_zero(cls, value: list[OrderLineIn]) -> list[OrderLineIn]:
        if not value:
            raise ValueError("an order must contain at least one line")
        return value


class CreateOrderRequest(OrderPreviewRequest):
    """``POST /api/v1/orders`` (§14.2, §14.3).

    Adds the body half of the two idempotency guards. The header half
    (``Idempotency-Key``) cannot live here - it is a header, and putting an
    equivalent field in the body would invite clients to omit the header.

    ## Why ``address_id`` is **re-required** here

    Preview accepts an optional address (the V1 shipping policy is free, so nothing
    prices off it), but create cannot: the order snapshots the receiver's name, phone
    and address onto itself (§35), and the design's ``create_order`` signature takes
    ``address_id`` with no default.

    The field is therefore overridden as required rather than inherited as optional.
    Inheriting it would let a create with no address pass validation and fail later as
    ``ADDRESS_NOT_FOUND (50008)`` / 404 - an error that says the *address* could not be
    found when in fact the request never contained one. The client would go looking for
    a missing address instead of a missing field. Re-requiring it turns that into the
    422 that names the field, at the edge, before any transaction opens.
    """

    model_config = ConfigDict(extra="forbid")

    address_id: int = Field(
        gt=0,
        description="The caller's own address; snapshotted onto the order at creation.",
    )
    client_request_id: str = Field(
        min_length=1,
        max_length=64,
        description="Client-generated idempotency token; unique per customer (§14.3, second guard).",
    )

    @field_validator("client_request_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("client_request_id must not be blank")
        return cleaned


class CancelOrderRequest(BaseModel):
    """``POST /api/v1/orders/{order_no}/cancel`` (§14.5). Both fields optional."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def _blank_becomes_absent(cls, value: str | None) -> str | None:
        # "" and "   " are the same intent as omitting the field, and §2 says an
        # omitted field is not a silent null. Normalising here keeps the stored
        # ``cancel_reason`` from holding an empty string that reads as "supplied".
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


# ---------------------------------------------------------------------------
# Shared response pieces
# ---------------------------------------------------------------------------
class MetaOut(BaseModel):
    """The frozen paging meta (§3). Always present, even for one page."""

    page: int
    page_size: int
    total: int
    total_pages: int


def page_meta(*, page: int, page_size: int, total: int) -> MetaOut:
    """Build the §3 meta block.

    One implementation, because the two easy mistakes are both silent and both
    client-visible: forgetting ``total_pages == 0`` for an empty result (the UI
    renders "1 of 0 pages") and using floor division (the last partial page
    disappears).
    """
    return MetaOut(
        page=page,
        page_size=page_size,
        total=total,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    )


class OrderItemOut(BaseModel):
    """A persisted order line, from the **snapshot** columns (§6, INV-014).

    Every field is read from ``order_items``. Nothing here joins the live
    catalogue: a product rename must not rewrite last month's invoice, and the
    only way to guarantee that is for the read path to have no catalogue join to
    forget to remove.
    """

    id: int
    product_id: int
    sku_id: int
    product_name: str
    sku_name: str
    image_url: str | None = None
    unit_price: int
    quantity: int
    original_amount: int
    promotion_discount_amount: int
    coupon_discount_amount: int
    allocated_discount_amount: int
    payable_amount: int
    refunded_amount: int
    after_sale_status: str

    @field_validator("after_sale_status")
    @classmethod
    def _known_after_sale_status(cls, value: str) -> str:
        # Defensive rather than decorative: the stored value comes from a CHECK
        # constraint, so an unknown one means the vocabulary drifted from the
        # migration. Failing loudly here is far better than emitting a status the
        # frontend's exhaustive switch has no branch for.
        if value not in {member.value for member in AfterSaleStatus}:
            raise ValueError(f"unknown after_sale_status {value!r}")
        return value


class FulfillmentItemOut(BaseModel):
    """One shipped line inside a fulfillment (API_CONTRACT §5, frozen shape)."""

    id: int
    order_item_id: int
    sku_id: int
    product_name: str
    sku_name: str
    quantity: int


class FulfillmentOut(BaseModel):
    """A fulfillment as it appears inside an order (§5.1, frozen shape).

    Phase 4 has no fulfillment table, so ``shipments`` is always ``[]`` - which
    §6 explicitly allows ("``shipments``: []"). The model exists now so the field
    has a frozen type from the first release and Phase 5 fills it without a wire
    change. ``carrier``/``tracking_no`` are null until shipped.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    order_id: int
    order_no: str
    fulfillment_no: str
    fulfillment_status: str
    carrier: str | None = None
    tracking_no: str | None = None
    shipped_at: UtcTimestamp | None = None
    delivered_at: UtcTimestamp | None = None
    created_at: UtcTimestamp
    items: list[FulfillmentItemOut] = Field(default_factory=list)


class OrderSummaryOut(BaseModel):
    """A list row (§6). Deliberately carries **no** line items.

    ``item_count`` and ``first_item_name`` are the two backend-owned summary
    fields §6 added so an operator list can name what was bought without an N+1
    detail fetch. Both are stored on the order as snapshots, so computing them
    needs no catalogue read either.

    All four status axes are present: the UI must never infer one from another,
    and most importantly must read ``fulfillment_status`` - not ``order_status`` -
    to decide whether an order can be shipped.
    """

    id: int
    order_no: str
    order_status: str
    payment_status: str
    fulfillment_status: str
    after_sale_status: str
    original_amount: int
    promotion_discount_amount: int
    coupon_discount_amount: int
    shipping_amount: int
    payable_amount: int
    paid_amount: int
    refunded_amount: int
    receiver_name: str
    receiver_phone: str
    created_at: UtcTimestamp
    paid_at: UtcTimestamp | None = None
    expires_at: UtcTimestamp | None = None
    item_count: int
    first_item_name: str
    #: Added in Phase 5. ``OrderDetail`` has carried ``refundable_amount`` since
    #: Phase 4 because a missing field silently disables every refund affordance in
    #: the UI (``undefined > 0`` is false). The list row had the same defect and it
    #: surfaced the moment the client-side fallback was deleted: the consumer order
    #: list derives its refund control from this number. Same owner, same rule -
    #: ``Order.refundable_amount`` is the single implementation of INV-005.
    refundable_amount: int

    @field_validator("order_status")
    @classmethod
    def _known_order_status(cls, value: str) -> str:
        if value not in {member.value for member in OrderStatus}:
            raise ValueError(f"unknown order_status {value!r}")
        return value

    @field_validator("payment_status")
    @classmethod
    def _known_payment_status(cls, value: str) -> str:
        if value not in {member.value for member in PaymentStatus}:
            raise ValueError(f"unknown payment_status {value!r}")
        return value

    @field_validator("fulfillment_status")
    @classmethod
    def _known_fulfillment_status(cls, value: str) -> str:
        if value not in {member.value for member in FulfillmentStatus}:
            raise ValueError(f"unknown fulfillment_status {value!r}")
        return value


class OrderDetailOut(OrderSummaryOut):
    """One order, with lines, shipments and the address snapshot (§6).

    The three server-owned additions, and why each is server-owned:

    * ``refundable_amount`` - ``paid_amount - refunded_amount``, floored at 0.
      The frontend found that deriving this client-side silently disables every
      refund affordance (``undefined > 0`` is ``false``), so a money figure that
      decides whether a control is *rendered* belongs next to the rule it
      enforces - INV-005.
    * ``cancel_reason`` - recorded by the cancel workflow. Null unless the status
      is ``CANCELLED``/``CLOSED``; the client cannot infer it.
    * ``full_address`` - the **masked** snapshot (§14.6). The raw
      ``address_snapshot`` column is deliberately **not** returned: §94 masks the
      receiver, and shipping the unmasked dwelling next to the masked name would
      make the mask decorative.
    """

    items: list[OrderItemOut] = Field(default_factory=list)
    shipments: list[FulfillmentOut] = Field(default_factory=list)
    full_address: str | None = None
    remark: str | None = None
    cancel_reason: str | None = None
    completed_at: UtcTimestamp | None = None
    cancelled_at: UtcTimestamp | None = None
    #: ``paid_amount - refunded_amount``, never negative (INV-005).
    refundable_amount: int


# ---------------------------------------------------------------------------
# Preview response (§14.2)
# ---------------------------------------------------------------------------
class OrderPreviewItemOut(BaseModel):
    """A priced line in a preview.

    Note this is **not** :class:`OrderItemOut`: a preview line has no ``id``,
    ``refunded_amount`` or ``after_sale_status``, because nothing has been
    persisted. Reusing the persisted shape and filling the missing fields with
    zeros would make an unplaced cart indistinguishable from an order - which is
    how a client ends up posting a preview line's ``payable_amount`` back.
    """

    sku_id: int
    product_id: int
    product_name: str
    sku_name: str
    image_url: str | None = None
    unit_price: int
    quantity: int
    original_amount: int
    promotion_discount_amount: int
    coupon_discount_amount: int
    allocated_discount_amount: int
    payable_amount: int


class OrderPreviewOut(BaseModel):
    """The preview payload (§14.2).

    ``warnings`` are pricing **codes**, not sentences (see
    :class:`~app.modules.pricing.enums.PricingWarning`): the console renders the
    localised text. A rule that was supplied and did nothing produces a warning;
    a rule that is simply absent does not, because absence is not an event.
    """

    items: list[OrderPreviewItemOut] = Field(default_factory=list)
    original_amount: int
    promotion_discount_amount: int
    coupon_discount_amount: int
    shipping_amount: int
    payable_amount: int
    warnings: list[str] = Field(default_factory=list)
