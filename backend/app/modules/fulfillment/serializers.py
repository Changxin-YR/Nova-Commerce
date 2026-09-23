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

from collections.abc import Mapping
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


def _item_out(row: Any, sku_by_line: Mapping[int, int]) -> FulfillmentItemOut:
    """One shipped line.

    ``product_name``/``sku_name`` come from the fulfillment row's own snapshot, not
    from a join to the catalogue: the parcel shipped what the order said, and a
    product renamed after shipping must not retitle a package that is already in
    transit (INV-014, applied to the goods-out side).

    ## ``sku_id`` is DERIVED through the order line - there is no column, and none is coming

    ``fulfillment_items`` does not store ``sku_id``: REQ-FUL-002 in
    ``PROJECT_BASELINE.yaml`` freezes its columns as ``fulfillment_id, order_item_id,
    quantity``, and duplicating a key reachable through ``order_items`` would create a
    second place for one fact to be wrong - a package line whose ``sku_id`` disagreed
    with the order line it claims to be part of is a shipping error nobody can detect.
    The frozen wire shape still requires the field (``API_CONTRACT`` section 5,
    ``FulfillmentItemOut``), so it is **derived here** from ``order_item_id``.

    That derivation reads ``order_items``, which is itself a snapshot table, so INV-014
    is untouched: nothing about a shipped parcel depends on the live catalogue.

    **This is the permanent design, not a workaround awaiting a schema change.** The
    captain withdrew ``PHASE5_DESIGN`` section 5.4's ``sku_id FK RESTRICT`` and ruled
    that the baseline wins, so a later reader must not "restore" the column; doing so
    would contradict REQ-FUL-002 and re-open a settled question. There is deliberately
    no ``getattr(row, "sku_id", None)`` fallback: one path, so the derivation cannot
    silently bypass itself.

    ``sku_by_line`` is passed in rather than looked up per row because the list
    endpoint would otherwise run one query per line - the N+1 that only becomes
    visible on an order that shipped in five packages with four lines each. The caller
    loads one mapping per page, which is the same rule the order module's
    ``_fulfillment_out`` follows for ``shipments[]``.
    """
    return FulfillmentItemOut(
        id=row.id,
        order_item_id=row.order_item_id,
        sku_id=sku_by_line[row.order_item_id],
        product_name=row.product_name,
        sku_name=row.sku_name,
        quantity=row.quantity,
    )


def to_fulfillment(row: Any, sku_by_line: Mapping[int, int] | None = None) -> FulfillmentOut:
    """Project one fulfillment ORM row into the frozen shape.

    ``carrier``/``tracking_no``/``shipped_at`` are ``None`` until shipped and are
    returned as ``None`` rather than replaced by ``""``: the console distinguishes
    "no tracking number" from "an empty tracking number", and a placeholder would
    erase that distinction on the wire.
    """
    mapping: Mapping[int, int] = sku_by_line or {}
    items = [_item_out(line, mapping) for line in (getattr(row, "items", None) or ())]
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
    sku_by_line: Mapping[int, int] | None = None,
) -> FulfillmentPageOut:
    """The paged envelope payload of section 3 (``items`` + ``meta``).

    ``page``/``page_size``/``total`` are passed in rather than derived from ``rows``,
    because ``total`` is the count *before* the page slice - deriving it from
    ``len(rows)`` would silently report every page as the last one.

    ``sku_by_line`` covers **every line on the page**, resolved in one query by the
    caller.
    """
    mapping: Mapping[int, int] = sku_by_line or {}
    return FulfillmentPageOut(
        items=[to_fulfillment(row, mapping) for row in rows],
        meta=page_meta(page=page, page_size=page_size, total=total),
    )
