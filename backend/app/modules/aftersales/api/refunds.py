"""Read-only refund endpoints - ``/api/v1/after-sales/refunds/*``.

PHASE5_DESIGN section 7 lists this submodule as **read-only**, and that is a design
statement rather than a gap: a refund is executed by the task endpoint
``POST /after-sales/admin/after-sales/{after_sale_no}/refund``, which is the only path
that may move money. There is deliberately no ``POST /refunds`` and no
``PATCH /refunds/{no}`` - a refund that could be created or edited on its own would be
a second money-out path with its own caps to get wrong, and FG-12 exists because the
caps must hold at exactly one place.

The router is mounted under the module's prefix (``/after-sales``), so the list is
``GET /api/v1/after-sales/refunds/admin``.

## Why the refund list is worth an endpoint at all

``payments.refunded_amount``, ``orders.refunded_amount`` and
``order_items.refunded_amount`` are denormalised counters, and the rows in this table
are what explain them. A finance query that can read the movements next to the
counters is the difference between "the total is wrong" and "this movement is wrong",
which is the money-out form of INV-007: a balance no ledger explains is worse than no
ledger.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import RefundNotFoundError, ValidationError, envelope
from app.modules.aftersales.enums import RefundStatus
from app.modules.aftersales.models import AfterSale
from app.modules.aftersales.repository import RefundRepository
from app.modules.aftersales.schemas import page_meta
from app.modules.aftersales.serializers import to_refund
from app.modules.identity.dependencies import ConsolePrincipal
from app.shared.db.session import get_session

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]


@router.get(
    "/refunds/admin",
    summary="List refund movements (console)",
    description=(
        "Paged envelope per section 3. Filters: `status` (`PENDING`/`SUCCEEDED`/"
        "`FAILED`), `order_no`. Read-only by design: this endpoint exists so the "
        "denormalised `refunded_amount` counters can be reconciled against the rows that "
        "explain them."
    ),
)
def list_admin_refunds(
    principal: ConsolePrincipal,
    session: SessionDep,
    status: Annotated[str | None, Query(max_length=16)] = None,
    order_no: Annotated[str | None, Query(max_length=32)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    _validate_status_filter(status)
    rows, total = RefundRepository(session).list_admin_refunds(
        merchant_id=_merchant_filter(principal),
        status=status,
        order_no=order_no,
        page=page,
        page_size=page_size,
    )
    # `refunds` holds `after_sale_id`; the wire shape needs the claim's public identifier. One
    # query for the page's claims (by id, via the unique index) rather than one per row.
    claim_numbers = _claim_numbers(session, rows)
    return envelope(
        data={
            "items": [
                to_refund(row, after_sale_no=claim_numbers[row.after_sale_id]).model_dump(mode="json")
                for row in rows
            ],
            "meta": page_meta(page=page, page_size=page_size, total=total).model_dump(),
        }
    )


@router.get(
    "/refunds/admin/{refund_no}",
    summary="One refund movement (console)",
    description="Scoped by merchant in the query, so a foreign refund is `REFUND_NOT_FOUND (80003)`.",
)
def get_admin_refund(refund_no: str, principal: ConsolePrincipal, session: SessionDep) -> dict:
    refund = RefundRepository(session).get_by_refund_no(
        refund_no, merchant_id=_merchant_filter(principal)
    )
    if refund is None:
        raise RefundNotFoundError("refund not found")
    return envelope(data=to_refund(refund).model_dump(mode="json"))


def _claim_numbers(session: Session, refunds: list) -> dict[int, str]:
    """``after_sale_id -> after_sale_no`` for one page of refunds.

    A single ``IN`` query for the page rather than one lookup per row: a refund list is the
    kind of read where a per-row lookup quietly becomes fifty queries on a busy day.
    """
    ids = {row.after_sale_id for row in refunds}
    if not ids:
        return {}
    rows = session.execute(
        select(AfterSale.id, AfterSale.after_sale_no).where(AfterSale.id.in_(ids))
    ).all()
    return {int(row[0]): str(row[1]) for row in rows}


def _merchant_filter(principal: object) -> int:
    """The merchant scope for a console refund query.

    ``None`` would make the query unscoped - the one direction a tenant filter must
    never fail in - so a staff principal with no merchant is refused rather than
    answered with every merchant's money movements.
    """
    merchant_id = getattr(principal, "merchant_id", None)
    if merchant_id is None:
        raise ValidationError("this staff account is not attached to a merchant")
    return int(merchant_id)


def _validate_status_filter(value: str | None) -> None:
    """Refuse a refund-status filter that can never match (the order module's pattern)."""
    if value is None:
        return
    allowed = tuple(member.value for member in RefundStatus)
    if value not in allowed:
        raise ValidationError(
            f"status must be one of {list(allowed)}",
            context={"status": value},
        )
