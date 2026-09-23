"""ORM -> wire projection for a fulfillment (API_CONTRACT section 5, frozen).

``frontend/src/types/frozen-contract.ts`` is the mirror of this file. The two shapes
are the same object seen from the two sides, so a change here that is not made there
(or the reverse) is an integration defect rather than a nicety - which is why the
projection is written once, here, and reused by the admin queue, the consumer
shipment list and ``OrderDetail.shipments[]``.

## Attribute access only, deliberately

Every function below reads attributes rather than importing a model class. The order
module's own ``_fulfillment_out`` makes the same choice for the same reason: a
serializer that imported ``app.modules.fulfillment.models`` would couple the order
detail read to a Phase 5 table, and the cross-module import is exactly what
``ORDER_WORKFLOW.md`` forbids. Reading attributes means the same projection works
for an ORM row, a lightweight row proxy, or a test double - and it cannot drift with
a model change that renames nothing but relocates everything.

## ``items`` is never lazy here

The shape carries ``items[]`` for every package. A serializer that triggered a lazy
load per package would be the N+1 that only shows up on the order that shipped in
five parcels, so ``Fulfillment.items`` is ``lazy="selectin"`` and the repository
methods that feed this projection load the items eagerly. The
``(getattr(row, "items", None) or ())`` fallback is for the two legitimate cases
where the collection genuinely is not loaded: a ``None`` from a detached row, and a
test double that models only the columns.
"""

from __future__ import annotations

from typing import Any

from app.modules.fulfillment.schemas import (
    FulfillmentItemOut,
    FulfillmentOut,
    FulfillmentPageOut,
    page_meta,
)

__all__ = [
    "to_fulfillment",
    "to_page",
]


def _item_out(row: Any) -> FulfillmentItemOut:
    """One shipped line.

    ``product_name``/``sku_name`` come from the fulfillment row's own snapshot, not
    from a join to the catalogue: the parcel shipped what the order said, and a
    product renamed after shipping must not retitle a package that is already in
    transit (INV-014, applied to the goods-out side).

    ## ``sku_id`` is a stored snapshot column, read straight off the row

    ``fulfillment_items.sku_id`` was ruled in, out, and back in during Phase 5; the
    final ruling is the column, and ``models.py`` records the reasoning. The decisive
    argument is the snapshot-table one: these rows already store ``product_name`` and
    ``sku_name`` as values, so carrying a line's names but not the id they came from
    was the one genuinely inconsistent combination. ``order_item_id`` remains the
    authoritative link (INV-014); the id sits beside the names so a reader never needs
    a second query.

    An earlier revision derived it here from a ``sku_by_line`` map the caller built for
    the whole page. That was correct but no longer necessary - and it was not free: it
    cost one extra query per page and classified a caller's missing map as
    ``InternalError``, the same error reserved for a package line pointing at an order
    line that is not on the order. With the column, this function reads one attribute
    and cannot be handed incomplete input.
    """
    return FulfillmentItemOut(
        id=row.id,
        order_item_id=row.order_item_id,
        sku_id=row.sku_id,
        product_name=row.product_name,
        sku_name=row.sku_name,
        quantity=row.quantity,
    )


def to_fulfillment(row: Any) -> FulfillmentOut:
    """Project one fulfillment ORM row into the frozen shape.

    ``carrier``/``tracking_no``/``shipped_at`` are ``None`` until shipped and are
    returned as ``None`` rather than replaced by ``""``: the console distinguishes
    "no tracking number" from "an empty tracking number", and a placeholder would
    erase that distinction on the wire.
    """
    items = [_item_out(line) for line in (getattr(row, "items", None) or ())]
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


def to_page(
    rows: list[Any],
    *,
    page: int,
    page_size: int,
    total: int,
) -> FulfillmentPageOut:
    """The paged envelope payload of section 3 (``items`` + ``meta``).

    ``page``/``page_size``/``total`` are passed in rather than derived from ``rows``,
    because ``total`` is the count *before* the page slice - deriving it from
    ``len(rows)`` would silently report every page as the last one.


    It takes no ``sku_by_line`` map: each item now carries its own ``sku_id`` column,
    so a page costs no extra query for it. An earlier revision required the caller to
    build a page-wide map - correct, but it cost one query per page and turned a
    caller's forgotten map into an ``InternalError`` that blamed the data.
    """
    return FulfillmentPageOut(
        items=[to_fulfillment(row) for row in rows],
        meta=page_meta(page=page, page_size=page_size, total=total),
    )
