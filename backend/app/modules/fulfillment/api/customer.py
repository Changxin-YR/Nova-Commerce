"""Consumer fulfillment endpoints - ``/api/v1/fulfillments/customer/*``.

One read: the shipments of one order. There is deliberately **no consumer ship
endpoint** - shipping is a merchant staff action (it consumes stock and creates the
evidence that goods moved), and a customer who could POST ``ship`` could mark their
own parcel on its way and then claim it never arrived.

The path is the frozen one from ``PHASE5_DESIGN`` section 7
(``GET /fulfillments/customer/orders/{order_no}/shipments``). It is keyed by
``order_no`` rather than by fulfillment id because a consumer navigates from the
order they placed, and the packages are what that order turned out to be.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.fulfillment.serializers import to_fulfillment
from app.modules.fulfillment.service import FulfillmentService
from app.modules.identity.dependencies import CurrentPrincipal
from app.shared.db.session import get_session

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]


@router.get(
    "/customer/orders/{order_no}/shipments",
    summary="List an order's shipments (consumer)",
    description=(
        "Every package of one order, oldest first, in the frozen Fulfillment shape "
        "(`API_CONTRACT` section 5). An empty list is a normal answer: an order that "
        "has not been packed has no packages.\n\n"
        "An order belonging to somebody else answers `ORDER_NOT_FOUND (50003)` "
        "rather than an empty list or a permission error - the ownership filter is "
        "applied in the query, so the foreign row is never loaded (IDOR)."
    ),
)
def list_order_shipments(
    order_no: str,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> dict:
    service = FulfillmentService(session)
    rows = service.get_for_order_no(principal=principal, order_no=order_no)
    return envelope(data=[to_fulfillment(row).model_dump(mode="json") for row in rows])
