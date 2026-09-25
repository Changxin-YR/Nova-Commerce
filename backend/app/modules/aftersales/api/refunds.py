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

## Why this file has no query in it

Both handlers call exactly one ``AfterSaleService`` method and shape its result. The
merchant scope, the status vocabulary and the ``IN`` lookup that resolves each row's
claim number all live in the service, so a non-HTTP caller (the Phase 9 tool gateway)
gets the same scoping rather than a second, weaker implementation.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.aftersales.schemas import page_meta
from app.modules.aftersales.serializers import to_refund
from app.modules.aftersales.service import AfterSaleService
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
    page_of_refunds = AfterSaleService(session).list_admin_refunds(
        principal=principal,
        refund_status=status,
        order_no=order_no,
        page=page,
        page_size=page_size,
    )
    return envelope(
        data={
            "items": [
                to_refund(
                    row, after_sale_no=page_of_refunds.claim_numbers[row.after_sale_id]
                ).model_dump(mode="json")
                for row in page_of_refunds.rows
            ],
            "meta": page_meta(
                page=page, page_size=page_size, total=page_of_refunds.total
            ).model_dump(),
        }
    )


@router.get(
    "/refunds/admin/{refund_no}",
    summary="One refund movement (console)",
    description="Scoped by merchant in the query, so a foreign refund is `REFUND_NOT_FOUND (80003)`.",
)
def get_admin_refund(refund_no: str, principal: ConsolePrincipal, session: SessionDep) -> dict:
    refund, after_sale_no = AfterSaleService(session).get_admin_refund(
        principal=principal, refund_no=refund_no
    )
    return envelope(data=to_refund(refund, after_sale_no=after_sale_no).model_dump(mode="json"))
