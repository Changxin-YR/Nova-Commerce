"""Order read-path projections - ORM rows to the frozen wire shapes.

Spec references:
    §6    ``OrderSummary`` (no lines) vs ``OrderDetail`` (lines + shipments + address)
    §94   ``receiver_name``/``receiver_phone`` are **already masked** on read, on
          both surfaces
    §14.6 ``full_address`` is the masked address snapshot; the client must not
          attempt to un-mask it
    §112  INV-014: a historical order is immune to later product edits

## Why a serializer and not ``model_validate(order)``

Three things happen here that no ORM projection can express:

1. **Masking.** ``receiver_name`` and ``receiver_phone`` exist as raw columns and
   must never reach a client unmasked. Doing it at the edge means the raw values
   stay available to the cancel/fulfilment paths that legitimately need them - and
   it means there is exactly one function in the codebase that decides what a
   client sees.
2. **Derived, server-owned figures.** ``refundable_amount`` is read from the model
   property rather than recomputed here, so the rule lives in one place (INV-005).
3. **Explicit absence.** §2 says an omitted field is never a silent ``null``.
   Building the output field by field is what makes that checkable.

## The snapshot rule

Every field below is read from ``orders``/``order_items``. This module performs
**no catalogue lookup at all** - not even for the image, whose signed URL is
already stored on the line as ``image_url``. That is INV-014: a product rename or
a price edit must not be able to rewrite an order that was placed last month, and
the only way to be sure is for the read path to have no live source to read from.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.core.redaction import mask_address, mask_name, mask_phone
from app.modules.order.models import Order, OrderItem
from app.modules.order.schemas import (
    FulfillmentItemOut,
    FulfillmentOut,
    OrderDetailOut,
    OrderItemOut,
    OrderPreviewItemOut,
    OrderPreviewOut,
    OrderSummaryOut,
)

__all__ = [
    "mask_full_address",
    "to_detail",
    "to_preview",
    "to_summary",
]


def _masked_receiver(order: Order) -> tuple[str, str]:
    """The two PII columns, masked (§94).

    Called on both surfaces - list and detail - because an order *list* is the
    more attractive exfiltration target: one request returns many receivers.
    """
    return mask_name(order.receiver_name or ""), mask_phone(order.receiver_phone or "")


def mask_full_address(snapshot: dict[str, Any] | None) -> str | None:
    """Join the address snapshot's geography and mask the dwelling (§14.6).

    Accepts both shapes the snapshot may legitimately have: the structured
    ``province``/``city``/``district``/``detail`` keys (§4.1) and a pre-joined
    ``full_address`` produced by :meth:`UserAddress.to_snapshot`. Tolerating both
    is not indecision - the snapshot is stored data that has already been written
    to production in one shape, and a serializer that can only read the newest
    shape would turn a historical order into a 500.

    Returns ``None`` for a missing/empty snapshot rather than an empty string, so
    "we have no address on file" stays distinguishable from "the address is
    blank" (§2's null-versus-absent rule).
    """
    if not snapshot:
        return None

    joined = snapshot.get("full_address")
    if not joined:
        parts = [
            snapshot.get("province") or "",
            snapshot.get("city") or "",
            snapshot.get("district") or "",
            snapshot.get("detail") or "",
        ]
        joined = "".join(part for part in parts if part)

    if not joined:
        return None
    # Postcode is deliberately dropped: it is additional PII with no use in the
    # order UI, and `mask_address` cannot mask something it is not shown.
    return mask_address(joined)


def _item_out(item: OrderItem) -> OrderItemOut:
    return OrderItemOut(
        id=item.id,
        product_id=item.product_id,
        sku_id=item.sku_id,
        product_name=item.product_name,
        sku_name=item.sku_name,
        image_url=item.image_url,
        unit_price=item.unit_price,
        quantity=item.quantity,
        original_amount=item.original_amount,
        promotion_discount_amount=item.promotion_discount_amount,
        coupon_discount_amount=item.coupon_discount_amount,
        allocated_discount_amount=item.allocated_discount_amount,
        payable_amount=item.payable_amount,
        refunded_amount=item.refunded_amount,
        after_sale_status=item.after_sale_status,
    )


def _fulfillment_out(row: Any) -> FulfillmentOut:
    """Project a fulfillment ORM row into the §5 frozen shape.

    Phase 4 never calls this with a row (there is no fulfillment table yet), but
    the projection lives here rather than being deferred so that
    ``OrderDetailOut.shipments`` has a frozen element type from the first
    release. Written against attribute access only, so it does not import - and
    therefore cannot drift with - a Phase 5 model.
    """
    items = [
        FulfillmentItemOut(
            id=line.id,
            order_item_id=line.order_item_id,
            sku_id=line.sku_id,
            product_name=line.product_name,
            sku_name=line.sku_name,
            quantity=line.quantity,
        )
        for line in (getattr(row, "items", None) or ())
    ]
    return FulfillmentOut(
        id=row.id,
        order_id=row.order_id,
        order_no=row.order_no,
        fulfillment_no=row.fulfillment_no,
        fulfillment_status=row.fulfillment_status,
        carrier=row.carrier,
        tracking_no=row.tracking_no,
        shipped_at=row.shipped_at,
        delivered_at=row.delivered_at,
        created_at=row.created_at,
        items=items,
    )


def to_summary(order: Order) -> OrderSummaryOut:
    """A list row (§6). Carries no line items - that is the whole point of it."""
    receiver_name, receiver_phone = _masked_receiver(order)
    return OrderSummaryOut(
        id=order.id,
        order_no=order.order_no,
        order_status=order.order_status,
        payment_status=order.payment_status,
        fulfillment_status=order.fulfillment_status,
        after_sale_status=order.after_sale_status,
        original_amount=order.original_amount,
        promotion_discount_amount=order.promotion_discount_amount,
        coupon_discount_amount=order.coupon_discount_amount,
        shipping_amount=order.shipping_amount,
        payable_amount=order.payable_amount,
        paid_amount=order.paid_amount,
        refunded_amount=order.refunded_amount,
        receiver_name=receiver_name,
        receiver_phone=receiver_phone,
        created_at=order.created_at,
        paid_at=order.paid_at,
        expires_at=order.expires_at,
        item_count=order.item_count,
        first_item_name=order.first_item_name,
        # Read from the model property, not recomputed: INV-005 has one owner, and
        # the summary row is not allowed to grow a second derivation of it.
        refundable_amount=order.refundable_amount,
    )


def to_detail(
    order: Order,
    fulfillments: Sequence[Any] = (),
    *,
    items: Sequence[OrderItem] | None = None,
) -> OrderDetailOut:
    """One order, with lines, shipments and the masked address (§6).

    ``items`` defaults to ``order.items``: the relationship is ``lazy="selectin"``,
    so the lines are already loaded by the query that loaded the order and reading
    the attribute costs no round trip. The parameter exists so a caller that has
    the rows in hand (the create workflow, straight after inserting them) can pass
    them without relying on relationship state that a flush may not have refreshed.

    No INV-006 assertion here. The invariant is asserted *inside the creating
    transaction* where a failure still rolls back; raising on the read path would
    instead turn a historical bad row into an unusable order, hiding the data
    someone needs in order to repair it.

    ``shipments`` resolution order: an explicit argument wins, then a collection
    the caller attached to the order (``OrderService`` does that, so the service
    owns the query and this function keeps reading attributes only), then ``[]``.
    The last fallback is not a silent default - section 6 explicitly allows an
    empty list for an order with no packages, and the unit tests that build a bare
    object are asserting exactly that.

    ``refundable_amount`` is **not** passed explicitly: it now travels in
    ``to_summary``'s dump (Phase 5 put it on the base order shape, section 15.8),
    and ``OrderDetailOut`` inherits it. Repeating it by keyword here raised
    ``got multiple values for keyword argument`` - a ``TypeError`` on every order
    read, which is exactly the kind of failure the shared base field prevents.
    There is still one implementation: the ``Order.refundable_amount`` property.
    """
    summary = to_summary(order)
    lines = list(order.items if items is None else items)
    if not fulfillments:
        fulfillments = tuple(getattr(order, "shipments", None) or ())

    return OrderDetailOut(
        **summary.model_dump(),
        items=[_item_out(item) for item in lines],
        shipments=[_fulfillment_out(row) for row in fulfillments],
        full_address=mask_full_address(order.address_snapshot),
        remark=order.remark,
        cancel_reason=order.cancel_reason,
        completed_at=order.completed_at,
        cancelled_at=order.cancelled_at,
    )


def to_preview(cart: Any) -> OrderPreviewOut:
    """The preview payload (§14.2).

    Per-item discounts are read through :class:`CartPrice`'s own accessors
    (``promotion_allocation`` / ``coupon_allocation``) rather than by rebuilding a
    lookup here. They are the authority's own view of its allocation, and a second
    traversal written in the order module is precisely the "third opinion" §37
    forbids - if the two ever disagreed, the client would be shown a split that the
    database would not reproduce.

    ``warnings`` are pricing **codes**, not sentences - the console renders the
    localised text from the code, which is why the frozen wire field is a list of
    strings rather than a list of objects.
    """
    items: list[OrderPreviewItemOut] = []
    for item_price in cart.items:
        line = item_price.line
        sku_id = line.sku_id
        # Read through the authority's own accessors. It would be tempting to write
        # `original - promotion - coupon` here, and it would even be correct today - but
        # that is a second implementation of an identity pricing-author owns, and it is
        # the one that goes silently wrong the day the identity changes (a per-item
        # share of a shipping charge, say). §37 allows one authority, so the split is
        # asked for, not re-derived.
        items.append(
            OrderPreviewItemOut(
                sku_id=sku_id,
                product_id=line.product_id,
                product_name=line.product_name,
                sku_name=line.sku_name,
                image_url=line.image_url,
                unit_price=line.unit_price,
                quantity=line.quantity,
                original_amount=item_price.original_amount,
                promotion_discount_amount=cart.promotion_allocation(sku_id),
                coupon_discount_amount=cart.coupon_allocation(sku_id),
                allocated_discount_amount=cart.allocated_discount(sku_id),
                payable_amount=cart.item_payable_amount(sku_id),
            )
        )

    return OrderPreviewOut(
        items=items,
        original_amount=cart.original_amount,
        promotion_discount_amount=cart.promotion_discount_amount,
        coupon_discount_amount=cart.coupon_discount_amount,
        shipping_amount=cart.shipping_amount,
        payable_amount=cart.payable_amount,
        warnings=[str(warning) for warning in cart.warnings],
    )
