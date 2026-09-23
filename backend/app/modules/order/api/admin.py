"""Console order endpoints - ``/api/v1/orders/admin*`` (§6, §14.6).

Two read-only handlers. There is deliberately no console cancel, no console
"mark paid" and no console status editor in Phase 4: each of those is a write that
needs its own compensating logic (a stock release, a payment record, a refund
path), and §31's whole point is that a status is moved by a workflow that knows
what the move costs - never by a generic field update.

## Thin, but not unprotected

``ConsolePrincipal`` (a dependency) enforces the account type and the merchant
scope before any handler body runs, and the merchant filter is applied again
**inside** the query by the service, so a scoped principal cannot widen its own
result set. The ``ORDER_READ`` permission check lives on the service rather than on
the route decorator: Phase 9's tool gateway calls services directly, and a check
that exists only in a route is a check a non-HTTP caller never sees.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.identity.dependencies import ConsolePrincipal
from app.modules.order.schemas import page_meta
from app.modules.order.serializers import to_detail, to_summary
from app.modules.order.service import OrderService
from app.shared.db.session import get_session

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]


@router.get(
    "/admin",
    summary="List orders (console)",
    description=(
        "Paged envelope per §3. Filters: `order_status`, `payment_status`, "
        "`fulfillment_status`, `order_no`.\n\n"
        "Each filter is validated against **its own** vocabulary, so an unmeetable "
        "filter is a validation error rather than an empty page - an operator who sees "
        "an empty list concludes there are no such orders and acts on it."
    ),
)
def list_admin_orders(
    principal: ConsolePrincipal,
    session: SessionDep,
    order_status: Annotated[str | None, Query(max_length=32)] = None,
    payment_status: Annotated[str | None, Query(max_length=32)] = None,
    fulfillment_status: Annotated[str | None, Query(max_length=32)] = None,
    order_no: Annotated[str | None, Query(max_length=32)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    page_result = OrderService(session).list_admin_orders(
        principal=principal,
        order_status=order_status,
        payment_status=payment_status,
        fulfillment_status=fulfillment_status,
        order_no=order_no,
        page=page,
        page_size=page_size,
    )
    return envelope(
        data={
            "items": [to_summary(row).model_dump(mode="json") for row in page_result.rows],
            "meta": page_meta(
                page=page, page_size=page_size, total=page_result.total
            ).model_dump(),
        }
    )


@router.get(
    "/admin/{order_no}",
    summary="One order (console)",
    description=(
        "The same `OrderDetail` the consumer surface returns, including the masked "
        "receiver (§14.6). An order outside the caller's merchant scope is "
        "`ORDER_NOT_FOUND (50003)`, because the scope is applied in the query and the "
        "foreign row is never fetched."
    ),
)
def get_admin_order(order_no: str, principal: ConsolePrincipal, session: SessionDep) -> dict:
    order = OrderService(session).get_admin_order(principal=principal, order_no=order_no)
    return envelope(data=to_detail(order).model_dump(mode="json"))
