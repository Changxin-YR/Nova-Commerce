"""Consumer after-sales endpoints - ``/api/v1/after-sales/customer/*``.

The frozen frontend paths (``frontend/src/api/endpoints.ts::afterSales``). Each handler
does exactly four things: parse, take the authenticated principal, call one service
method, wrap the result in the section 95 envelope. No eligibility arithmetic, no cap
logic and no SQL in this file - the service computes, the serializer renders.

## ``Idempotency-Key`` is a header, and declared optional here on purpose

Declaring it required would let FastAPI answer 422 with its own body shape, while the
contract freezes ``IDEMPOTENCY_KEY_REQUIRED (10010)`` for this case - the same
reasoning ``order/api/customer.py`` documents, and not cosmetic: every claim must be
idempotent, so omitting the key has to be a loud, mappable failure at the edge rather
than a validator message a client may not map.

The **body** also carries ``client_request_id`` (the frontend sends the same value in
both places). Two guards at two layers: the header is namespaced through
``idempotency_records``, and ``UNIQUE (user_id, client_request_id)`` on ``after_sales``
catches the client that lost the header.

## Why there is no ``POST /customer/after-sales/{no}/refund``

A refund is a console act in V1: ``REFUND_EXECUTE`` is a staff permission, and a
customer-triggered refund endpoint would move money on the customer's word. The
customer's side of a refund is the *claim* - apply, cancel, and read.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyKeyRequiredError, envelope
from app.modules.aftersales.schemas import (
    ApplyAfterSaleRequest,
    CancelAfterSaleRequest,
    page_meta,
)
from app.modules.aftersales.serializers import to_summary
from app.modules.aftersales.service import AfterSaleService
from app.modules.aftersales.views import detail_view
from app.modules.identity.dependencies import CurrentPrincipal
from app.shared.db.session import get_session

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]

#: Declared optional so the missing case produces our own frozen business code rather
#: than FastAPI's 422 (see the module docstring).
IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]


def _require_idempotency_key(value: str | None) -> str:
    """The frozen ``IDEMPOTENCY_KEY_REQUIRED (10010)``, raised at the edge."""
    if value is None or not value.strip():
        raise IdempotencyKeyRequiredError("Idempotency-Key header is required")
    return value


@router.post(
    "/customer/after-sales",
    summary="Apply for after-sales service",
    description=(
        "Creates a **claim** - a business fact, nothing more. No money moves and no order "
        "axis changes here: `orders.after_sale_status` is written only by the refund "
        "path, so a claim that has merely been filed cannot make an order look "
        "refunded.\n\n"
        "`requested_amount` and per-line `quantity` are **checked against caps the server "
        "computes** from the order's persisted line amounts and from what has already "
        "been claimed; neither is trusted. `order_no` is scoped to the caller, so another "
        "customer's order is `ORDER_NOT_FOUND (50003)` rather than a 403 - a 403 would "
        "confirm the order exists."
    ),
)
def apply_after_sale(
    payload: ApplyAfterSaleRequest,
    principal: CurrentPrincipal,
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> dict:
    claim = AfterSaleService(session).apply(
        principal=principal,
        payload=payload,
        idempotency_key=_require_idempotency_key(idempotency_key),
    )
    return envelope(data=to_summary(claim).model_dump(mode="json"))


@router.get(
    "/customer/after-sales",
    summary="List my after-sales claims",
    description=(
        "Paged envelope per section 3. `status` is validated against the **claim** "
        "vocabulary; an unmeetable filter is a validation error rather than an empty "
        "page, because an empty page is a conclusion the caller acts on."
    ),
)
def list_after_sales(
    principal: CurrentPrincipal,
    session: SessionDep,
    status: Annotated[str | None, Query(max_length=32)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    result = AfterSaleService(session).list_customer_claims(
        principal=principal, claim_status=status, page=page, page_size=page_size
    )
    return envelope(
        data={
            "items": [to_summary(row).model_dump(mode="json") for row in result.rows],
            "meta": page_meta(page=page, page_size=page_size, total=result.total).model_dump(),
        }
    )


@router.get(
    "/customer/after-sales/{after_sale_no}",
    summary="One of my after-sales claims",
    description=(
        "The full `AfterSale` shape: both collections plus the server-computed "
        "`refundable_amount`/`refund_cap`. Those two are server-owned because the real "
        "cap is the *smaller* of the claim's remaining approval and what the payment can "
        "still cover - a client deriving it from `approved_amount - refunded_amount` "
        "would enable a form whose submit always fails with "
        "`REFUND_EXCEEDS_PAID_AMOUNT (80004)`."
    ),
)
def get_after_sale(after_sale_no: str, principal: CurrentPrincipal, session: SessionDep) -> dict:
    service = AfterSaleService(session)
    claim = service.get_customer_claim(principal=principal, after_sale_no=after_sale_no)
    detail = detail_view(session=session, service=service, claim=claim)
    return envelope(data=detail.model_dump(mode="json"))


@router.post(
    "/customer/after-sales/{after_sale_no}/cancel",
    summary="Cancel my pending claim",
    description=(
        "Only a `PENDING` claim, because this is the customer withdrawing an unanswered "
        "ask. Withdrawing an **approved** claim is a different act with money already "
        "promised and belongs to the console, so it is `AFTER_SALE_STATE_INVALID (80002)` "
        "rather than a silent no-op: a client that believes it cancelled something must "
        "not be told OK when nothing changed."
    ),
)
def cancel_after_sale(
    after_sale_no: str,
    principal: CurrentPrincipal,
    session: SessionDep,
    payload: CancelAfterSaleRequest | None = None,
) -> dict:
    claim = AfterSaleService(session).cancel(
        principal=principal,
        after_sale_no=after_sale_no,
        payload=payload or CancelAfterSaleRequest(),
    )
    return envelope(data=to_summary(claim).model_dump(mode="json"))
