"""Console fulfillment endpoints - ``/api/v1/fulfillments/admin*`` and the ship task.

Two endpoints, and the second one is the phase's goods-out write:

* ``GET /admin`` - the fulfillment queue (``API_CONTRACT`` section 5.2), so an
  operator works from a queue rather than opening orders one at a time;
* ``POST /{id}/ship`` - the frozen task endpoint of section 4, keyed by
  **fulfillment id, not order number**: an order may ship in several packages, so
  "ship this order" is not a well-formed instruction.

## Where the permission check lives

``ConsolePrincipal`` (a dependency) enforces the account type and the merchant scope
before any handler body runs - a consumer token cannot reach these routes at all. The
``fulfillment:ship`` / ``fulfillment:read`` permission check lives on the service,
next to the row-level scope, rather than on the route decorator.

That placement is deliberate and follows ``order/api/admin.py``, which makes the same
choice for ``order:read``: Phase 9's tool gateway calls services **directly**, so a
check that exists only in a route is a check a non-HTTP caller never sees. It also
avoids expressing the permission check as a second principal-typed parameter on the
signature, which would make the dependency graph ambiguous about which principal is
being authorised.

``GET /fulfillments/{id}`` is deliberately **absent**: the contract needs the id for
ship and it arrives through ``GET /orders/{order_no}`` (``shipments[]``) or the queue
above. An endpoint nothing calls is surface area that has to be maintained and
secured for nobody.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.fulfillment.schemas import ShipFulfillmentRequest
from app.modules.fulfillment.serializers import to_fulfillment, to_page
from app.modules.fulfillment.service import FulfillmentService
from app.modules.identity.dependencies import ConsolePrincipal
from app.shared.db.session import get_session

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]


@router.get(
    "/admin",
    summary="List packages (console queue)",
    description=(
        "Paged envelope per `API_CONTRACT` section 3. Filters: `order_no`, "
        "`fulfillment_status`.\n\n"
        "The status filter is validated against the frozen vocabulary, so an "
        "unmeetable filter is a validation error rather than an empty page - an "
        "operator who sees an empty queue concludes there is nothing to ship and "
        "acts on that conclusion."
    ),
)
def list_admin_fulfillments(
    principal: ConsolePrincipal,
    session: SessionDep,
    order_no: Annotated[str | None, Query(max_length=32)] = None,
    fulfillment_status: Annotated[str | None, Query(max_length=32)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    service = FulfillmentService(session)
    result = service.list_admin_fulfillments(
        principal=principal,
        order_no=order_no,
        fulfillment_status=fulfillment_status,
        page=page,
        page_size=page_size,
    )
    return envelope(
        data=to_page(
            result.rows,
            page=page,
            page_size=page_size,
            total=result.total,
        ).model_dump(mode="json")
    )


@router.post(
    "/{fulfillment_id}/ship",
    summary="Ship a package",
    description=(
        "Body is **exactly** `{carrier, tracking_no, item_quantities}` (`API_CONTRACT` "
        "section 5); anything else is refused rather than ignored.\n\n"
        "Returns the updated Fulfillment, because a task endpoint that returned `204` "
        "would leave the operator unable to confirm what changed.\n\n"
        "Effects: stamps carrier/tracking/`shipped_at`, records the shipped quantities, "
        "creates a residual package for anything the operator held back, and recomputes "
        "the **order's** `fulfillment_status` from all of its packages. It never touches "
        "`order_status`: shipping and the order lifecycle are independent axes "
        "(spec section 31).\n\n"
        "Refusals: `FULFILLMENT_NOT_FOUND (70000)` for a missing or foreign package, "
        "`FULFILLMENT_ALREADY_SHIPPED (70003)` for a package that already left, and "
        "`FULFILLMENT_QUANTITY_EXCEEDS_ORDER (70001)` when the request would ship more "
        "units of a line - cumulatively across every package - than the order line holds."
    ),
)
def ship_fulfillment(
    fulfillment_id: int,
    payload: ShipFulfillmentRequest,
    principal: ConsolePrincipal,
    session: SessionDep,
) -> dict:
    service = FulfillmentService(session)
    fulfillment = service.ship(principal=principal, fulfillment_id=fulfillment_id, payload=payload)
    return envelope(data=to_fulfillment(fulfillment).model_dump(mode="json"))
