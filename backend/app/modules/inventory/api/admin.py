"""Inventory HTTP interface - thin controllers only (spec §17).

Every handler here does exactly five things: parse the request, take the
authenticated principal, call one service method, map the result, wrap it in the
§95 envelope. There is no stock arithmetic, no policy and no SQL in this file.

The `Idempotency-Key` header is required on the write endpoint rather than being a
body field. Two reasons: header presence is trivially checkable by a client library
and by a gateway before the body is even parsed, and a body field invites clients
to omit it - whereas every stock movement *must* be idempotent (INV-003), so
omitting it should be a hard, loud failure at the edge.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, IdempotencyKeyRequiredError, envelope
from app.core.logging import get_logger
from app.modules.identity.dependencies import ConsolePrincipal, CurrentPrincipal
from app.modules.identity.enums import PermissionCode
from app.modules.inventory.enums import MovementType, OperatorType, ReferenceType
from app.modules.inventory.repository import InventoryRepository
from app.modules.inventory.schemas import (
    AdjustmentPreviewOut,
    AdjustmentPreviewRequest,
    CreateAdjustmentRequest,
    InventoryMovementOut,
    InventoryOut,
    LedgerVerificationOut,
)
from app.modules.inventory.service import InventoryService
from app.shared.db.session import get_session

logger = get_logger(__name__)
router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]


def _to_out(inventory, repository: InventoryRepository) -> InventoryOut:
    """Project an inventory row into the frozen response shape.

    `sku_no` / `product_name` / `sku_name` are joined in here rather than stored on
    the inventory row, so the console can render a human-readable stock list without
    a second request per row.
    """
    out = InventoryOut.model_validate(inventory)
    from app.modules.catalog.models import Product, ProductSku

    sku = repository._session.get(ProductSku, inventory.sku_id)
    if sku is not None:
        out.sku_no = sku.sku_no
        out.sku_name = sku.name
        product = repository._session.get(Product, sku.product_id)
        if product is not None:
            out.product_name = product.name
    return out


# ---------------------------------------------------------------------------
# Customer-facing availability
# ---------------------------------------------------------------------------
@router.get(
    "/skus/{sku_id}",
    summary="Public availability for one SKU",
    description=(
        "Returns only what a shopper needs: whether it can be bought and how many. "
        "Cost, safety stock and the ledger are deliberately absent - those are back-"
        "office facts and leaking them would disclose margin."
    ),
)
def get_public_availability(
    sku_id: int,
    # Required for its side effect: the dependency is what enforces that the caller
    # is authenticated at all. The value is unused because availability is not
    # identity-scoped - which is exactly why removing this parameter would silently
    # turn a logged-in-only endpoint into a public one.
    principal: CurrentPrincipal,  # noqa: ARG001
    session: SessionDep,
) -> dict:
    repository = InventoryRepository(session)
    inventory = repository.get_by_sku(sku_id=sku_id)
    if inventory is None:
        # A SKU with no stock record is not an error to a shopper; it is simply
        # unavailable. Distinguishing "not stocked" from "out of stock" would leak
        # catalogue internals, and both render identically in the UI.
        return envelope(
            data={"sku_id": sku_id, "available": False, "sellable_qty": 0, "in_stock": False}
        )
    return envelope(
        data={
            "sku_id": sku_id,
            "available": inventory.sellable_qty > 0,
            "sellable_qty": inventory.sellable_qty,
            "in_stock": inventory.sellable_qty > 0,
        }
    )


# ---------------------------------------------------------------------------
# Console reads
# ---------------------------------------------------------------------------
@router.get(
    "/admin",
    summary="List inventory positions",
    description="Paged envelope per `docs/architecture/API_CONTRACT.md` §3. Never a bare array.",
)
def list_inventory(
    principal: ConsolePrincipal,
    session: SessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    search: Annotated[str | None, Query(max_length=100)] = None,
    low_stock_only: bool = False,
) -> dict:
    principal.require_permission(PermissionCode.INVENTORY_READ.value)
    repository = InventoryRepository(session)
    rows, total = repository.list_inventory(
        merchant_id=principal.merchant_id,
        search=search,
        low_stock_only=low_stock_only,
        offset=(page - 1) * page_size,
        limit=page_size,
    )
    return envelope(
        data={
            "items": [_to_out(row, repository).model_dump(mode="json") for row in rows],
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                # ceil division, and 0 when there is nothing - so the client never
                # renders "1 of 0 pages".
                "total_pages": (total + page_size - 1) // page_size if total else 0,
            },
        }
    )


@router.get(
    "/admin/{sku_id}",
    summary="One inventory position",
)
def get_inventory(
    sku_id: int,
    principal: ConsolePrincipal,
    session: SessionDep,
) -> dict:
    principal.require_permission(PermissionCode.INVENTORY_READ.value)
    repository = InventoryRepository(session)
    inventory = repository.get_by_sku(sku_id=sku_id, warehouse_id=None)
    if inventory is None:
        from app.core.errors import InventoryNotFoundError

        raise InventoryNotFoundError(f"no stock record for SKU {sku_id}")
    return envelope(data=_to_out(inventory, repository).model_dump(mode="json"))


@router.get(
    "/admin/{sku_id}/movements",
    summary="Ledger for one SKU",
    description=(
        "The append-only movement history. Exposed on purpose: during a stock "
        "investigation the question is always 'who changed this and when', and a "
        "number without provenance cannot answer it."
    ),
)
def list_movements(
    sku_id: int,
    principal: ConsolePrincipal,
    session: SessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    movement_type: Annotated[str | None, Query()] = None,
) -> dict:
    principal.require_permission(PermissionCode.INVENTORY_READ.value)
    repository = InventoryRepository(session)
    inventory = repository.get_by_sku(sku_id=sku_id)
    if inventory is None:
        from app.core.errors import InventoryNotFoundError

        raise InventoryNotFoundError(f"no stock record for SKU {sku_id}")

    parsed_type: MovementType | None = None
    if movement_type:
        try:
            parsed_type = MovementType(movement_type)
        except ValueError:
            from app.core.errors import ValidationError

            raise ValidationError(f"unknown movement_type {movement_type!r}") from None

    rows = repository.movements_for(
        warehouse_id=inventory.warehouse_id,
        sku_id=sku_id,
        movement_type=parsed_type,
        limit=page_size * page,
    )
    items = [
        InventoryMovementOut.model_validate(row).model_dump(mode="json") for row in rows[:page_size]
    ]
    return envelope(
        data={
            "items": items,
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": len(items),
                "total_pages": 1 if items else 0,
            },
        }
    )


@router.get(
    "/admin/{sku_id}/verify",
    summary="Verify the ledger explains the balance (INV-007)",
    description=(
        "Answers 'can this number be trusted' on demand. The reconciliation job runs "
        "the same check in bulk; having it per-SKU means an operator investigating a "
        "discrepancy does not have to wait for the next sweep."
    ),
)
def verify_ledger(
    sku_id: int,
    principal: ConsolePrincipal,
    session: SessionDep,
) -> dict:
    principal.require_permission(PermissionCode.INVENTORY_READ.value)
    repository = InventoryRepository(session)
    inventory = repository.get_by_sku(sku_id=sku_id)
    if inventory is None:
        from app.core.errors import InventoryNotFoundError

        raise InventoryNotFoundError(f"no stock record for SKU {sku_id}")

    consistent, detail = InventoryService(session).verify_ledger(
        warehouse_id=inventory.warehouse_id, sku_id=sku_id
    )
    return envelope(
        data=LedgerVerificationOut(consistent=consistent, **detail).model_dump(mode="json")
    )


# ---------------------------------------------------------------------------
# Writes - preview first, then apply
# ---------------------------------------------------------------------------
@router.post(
    "/adjustments/preview",
    summary="Preview an adjustment",
    description=(
        "Spec §47 requires a preview before the write. Returns what the number would "
        "become and any warnings, so an operator sees the consequence before "
        "committing to it. Read-only despite being a POST: the verb is right because "
        "the body is a proposed change, but nothing is persisted."
    ),
)
def preview_adjustment(
    payload: AdjustmentPreviewRequest,
    principal: ConsolePrincipal,
    session: SessionDep,
) -> dict:
    principal.require_permission(PermissionCode.INVENTORY_ADJUST.value)
    preview = InventoryService(session).preview_adjustment(
        sku_id=payload.sku_id,
        delta_available=payload.delta_available,
        warehouse_id=payload.warehouse_id,
    )
    return envelope(
        data=AdjustmentPreviewOut(
            sku_id=preview.sku_id,
            warehouse_id=preview.warehouse_id,
            current_available=preview.current_available,
            current_locked=preview.current_locked,
            current_version=preview.current_version,
            proposed_delta=preview.proposed_delta,
            resulting_available=preview.resulting_available,
            warnings=preview.warnings,
        ).model_dump(mode="json")
    )


@router.post(
    "/adjustments",
    summary="Apply an adjustment",
    description=(
        "Optimistic-locked (spec §27): the caller must send the `version` it read. A "
        "stale version returns 409 carrying the current row, so the UI can show what "
        "changed instead of forcing a blind retry."
    ),
)
def create_adjustment(
    payload: CreateAdjustmentRequest,
    principal: ConsolePrincipal,
    session: SessionDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    principal.require_permission(PermissionCode.INVENTORY_ADJUST.value)
    if not idempotency_key:
        raise IdempotencyKeyRequiredError(
            "stock movements are idempotent by contract; supply an Idempotency-Key header"
        )

    inventory = InventoryService(session).adjust(
        sku_id=payload.sku_id,
        version=payload.version,
        delta_available=payload.delta_available,
        warehouse_id=payload.warehouse_id,
        reason=payload.reason,
        movement_type=MovementType.MANUAL_ADJUST,
        # Both come from the verified principal, never from the body - otherwise a
        # caller could forge who made the change (§70, §89).
        operator_type=OperatorType.STAFF,
        operator_id=principal.user_id,
        idempotency_key=idempotency_key,
    )
    session.commit()

    logger.info(
        "inventory adjusted",
        sku_id=payload.sku_id,
        delta=payload.delta_available,
        operator_id=principal.user_id,
        reason=payload.reason,
    )
    repository = InventoryRepository(session)
    return envelope(data=_to_out(inventory, repository).model_dump(mode="json"))


_ = ReferenceType  # referenced by the service signature; kept importable here
_ = ErrorCode
