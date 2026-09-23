"""Consumer order endpoints - the frozen paths of §96 / §14.

Every handler does exactly four things: parse, take the authenticated principal,
call one service method, wrap the result in the §95 envelope. There is no pricing
arithmetic, no state-machine logic and no SQL in this file.

## The paths are the frozen ones, not the frontend's earlier guesses

`frontend/src/api/endpoints.ts` currently calls `/orders/orders/*`; that shape is an
older invention and §14/§9 freeze these instead. The frontend migrates in Phase 7
integration. Implementing both would create two contracts, and the one that is
merely *reachable* is the one that starts being used.

## Why the create endpoint answers 200 and not 201

An idempotent replay returns the *same* order. A client that could not tell
"created" from "replayed" would have to guess whether its retry was safe, and
guessing is how a client ends up sending the same order twice or, worse, not
retrying something that failed (§14.2, INV-015).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyKeyRequiredError, envelope
from app.core.logging import get_logger
from app.modules.identity.dependencies import CurrentPrincipal
from app.modules.order.schemas import (
    CancelOrderRequest,
    CreateOrderRequest,
    OrderPreviewRequest,
    page_meta,
)
from app.modules.order.serializers import to_detail, to_preview, to_summary
from app.modules.order.service import OrderService
from app.modules.order.workflow import OrderLineInput
from app.shared.db.session import get_session

logger = get_logger(__name__)

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]

#: ``Idempotency-Key`` is a **header**, and declared optional here on purpose.
#:
#: Declaring it required would let FastAPI answer 422 with its own body shape, and
#: §14.3 freezes ``IDEMPOTENCY_KEY_REQUIRED (10010)`` for this case. The explicit
#: check below is what produces that code, which is the same pattern the inventory
#: adjustment endpoint already uses - and the reason is not cosmetic: every order
#: create must be idempotent (INV-015), so omitting the key has to be a hard, loud
#: failure at the edge rather than a validator message the client may not map.
IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]


def _lines(payload: OrderPreviewRequest) -> list[OrderLineInput]:
    """Map the wire lines to the workflow's input type.

    ``sku_id``/``quantity`` and nothing else - the request model has no price field
    to copy, which is §38 enforced by the type rather than by discipline.
    """
    return [OrderLineInput(sku_id=line.sku_id, quantity=line.quantity) for line in payload.items]


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
@router.post(
    "/preview",
    summary="Price a selection without creating anything",
    description=(
        "§29/§105: the cart is a client-side selection plus a server preview. There is "
        "no cart resource and no cart table in V1, because a persisted cart would be a "
        "second place where a price exists - which §37 forbids.\n\n"
        "The price returned here is **not** trusted by the create path, which recomputes "
        "it from the same authority. Trusting this response would make the price a client "
        "input."
    ),
)
def preview_order(
    payload: OrderPreviewRequest,
    principal: CurrentPrincipal,
    session: SessionDep,
) -> dict:
    service = OrderService(session)
    cart = service.preview(
        principal=principal,
        items=_lines(payload),
        address_id=payload.address_id,
        coupon_id=payload.coupon_id,
    )
    return envelope(data=to_preview(cart).model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.post(
    "",
    summary="Create an order from a selection",
    description=(
        "Requires an `Idempotency-Key` header *and* a `client_request_id` body field "
        "(§14.3). Two guards, because they cover different failures: the header is the "
        "correct client's retry, and the body field is the client that lost its header.\n\n"
        "Returns HTTP 200 with the frozen `OrderDetail`, whether the order was created "
        "now or is the original of a replay."
    ),
)
def create_order(
    payload: CreateOrderRequest,
    principal: CurrentPrincipal,
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> dict:
    if not idempotency_key:
        raise IdempotencyKeyRequiredError(
            "order creation is idempotent by contract; supply an Idempotency-Key header"
        )

    result = OrderService(session).create_order(
        principal=principal,
        items=_lines(payload),
        address_id=payload.address_id,
        client_request_id=payload.client_request_id,
        idempotency_key=idempotency_key,
        coupon_id=payload.coupon_id,
        remark=payload.remark,
    )
    logger.info(
        "order create request served",
        order_no=result.order.order_no,
        replayed=result.replayed,
        user_id=principal.user_id,
    )
    return envelope(data=to_detail(result.order).model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
@router.get(
    "",
    summary="List the caller's own orders",
    description=(
        "Paged envelope per §3, never a bare array. Own orders only, newest first. "
        "`receiver_name`/`receiver_phone` arrive already masked (§94) - the client must "
        "not attempt to un-mask them."
    ),
)
def list_orders(
    principal: CurrentPrincipal,
    session: SessionDep,
    order_status: Annotated[str | None, Query(max_length=32)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    page_result = OrderService(session).list_customer_orders(
        principal=principal,
        order_status=order_status,
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
    "/{order_no}",
    summary="One of the caller's own orders",
    description=(
        "Returns the frozen `OrderDetail` of §6: the lines, the (always empty in Phase 4) "
        "`shipments`, the masked address and the server-owned `refundable_amount`.\n\n"
        "An order belonging to somebody else is `ORDER_NOT_FOUND (50003)`, never 403 - "
        "distinguishing the two would make this endpoint an existence oracle (§109)."
    ),
)
def get_order(order_no: str, principal: CurrentPrincipal, session: SessionDep) -> dict:
    order = OrderService(session).get_customer_order(principal=principal, order_no=order_no)
    return envelope(data=to_detail(order).model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------
@router.post(
    "/{order_no}/cancel",
    summary="Cancel an unpaid order",
    description=(
        "`PENDING_PAYMENT -> CANCELLED`, releasing the reserved stock in the same "
        "transaction. Cancelling anything else is `ORDER_NOT_CANCELLABLE (50010)`.\n\n"
        "`cancel_reason` is server-owned: the body may supply one, and the stored value "
        "is null unless the status is CANCELLED/CLOSED (§14.5)."
    ),
)
def cancel_order(
    order_no: str,
    principal: CurrentPrincipal,
    session: SessionDep,
    payload: CancelOrderRequest | None = None,
) -> dict:
    order = OrderService(session).cancel(
        principal=principal,
        order_no=order_no,
        reason=payload.reason if payload is not None else None,
    )
    return envelope(data=to_detail(order).model_dump(mode="json"))


@router.post(
    "/{order_no}/confirm-receipt",
    summary="Confirm receipt of a paid order",
    description=(
        "`PROCESSING -> COMPLETED`. From any other state it is "
        "`ORDER_NOT_CONFIRMABLE (50011)`.\n\n"
        "This deliberately does **not** change `fulfillment_status`: §31 keeps the four "
        "status axes independent, and a receipt confirmation is a customer fact rather "
        "than a delivery fact."
    ),
)
def confirm_receipt(
    order_no: str, principal: CurrentPrincipal, session: SessionDep
) -> dict:
    order = OrderService(session).confirm_receipt(principal=principal, order_no=order_no)
    return envelope(data=to_detail(order).model_dump(mode="json"))
