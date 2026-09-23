"""Console payment endpoints - API_CONTRACT section 15.2's admin list.

One route: ``GET /api/v1/payments/admin/payments``, paged, filters ``status``,
``order_no`` and ``channel`` (section 3's envelope, never a bare array).

## Why there is no ``/{payment_id}`` admin detail in V1

The console reaches a payment through the **order** it belongs to (the order detail
page links to it), so a second lookup path would be a fourth way to address the same
row without a question it answers better. Section 15.7 is explicit that absence is
not permission: a single-payment admin read is simply not frozen yet.

The consequence worth stating: ``ConsolePrincipal`` requires a staff account whose
``data_scope`` is broader than ``SELF``, and the query is scoped by ``merchant_id`` in
the database rather than filtered after loading - a console that fetched every
merchant's payments and then filtered in Python would be one ``if`` away from leaking
another tenant's money traffic.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.errors import ValidationError, envelope
from app.modules.identity.dependencies import ConsolePrincipal
from app.modules.order.schemas import page_meta
from app.modules.payment.enums import PaymentChannel, PaymentRecordStatus
from app.modules.payment.serializers import to_payment_list
from app.modules.payment.service import PaymentService
from app.shared.db.session import get_session

__all__ = ["router"]

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]

#: The two closed vocabularies this route filters on, as plain sets for the
#: query-parameter guard below.
_STATUS_VALUES = frozenset(member.value for member in PaymentRecordStatus)
_CHANNEL_VALUES = frozenset(member.value for member in PaymentChannel)


@router.get(
    "/admin/payments",
    summary="The payment queue (console)",
    description=(
        "Paged envelope, newest first. Filters `status`, `order_no` and `channel`.\n\n"
        "Failed and duplicate callback deliveries are **not** rows here - they live in "
        "`payment_callbacks`, which is deliberately not part of this contract yet "
        "(section 15.7): the console's question is \"which payments need attention\", "
        "not \"what did the provider send\"."
    ),
)
def list_payments(
    principal: ConsolePrincipal,
    session: SessionDep,
    status: Annotated[str | None, Query(max_length=20)] = None,
    order_no: Annotated[str | None, Query(max_length=32)] = None,
    channel: Annotated[str | None, Query(max_length=16)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    # An unknown filter value is refused rather than dropped. Dropping it would turn
    # "status=PAYED" (a typo) into "no status filter", and the console would render
    # every payment while the operator believed they were looking at failures - a
    # wrong answer that looks exactly like a right one.
    normalized_status = _vocabulary_value(status, _STATUS_VALUES, name="status")
    normalized_channel = _vocabulary_value(channel, _CHANNEL_VALUES, name="channel")

    page_result = PaymentService(session).list_admin_payments(
        merchant_id=principal.merchant_id,
        status=normalized_status,
        order_no=order_no,
        channel=normalized_channel,
        page=page,
        page_size=page_size,
    )
    return envelope(
        data={
            "items": [
                row.model_dump(mode="json") for row in to_payment_list(page_result.rows)
            ],
            "meta": page_meta(
                page=page, page_size=page_size, total=page_result.total
            ).model_dump(),
        }
    )


def _vocabulary_value(
    value: str | None, vocabulary: frozenset[str], *, name: str
) -> str | None:
    """Normalise a filter against a closed vocabulary, or refuse it.

    Case is normalised because a console URL is typed by a human and ``paying`` is not
    a different filter from ``PAYING``. An unrecognised *member* is a 422 naming the
    field, which is actionable; a filter silently dropped is not.
    """
    if value is None:
        return None
    cleaned = value.strip().upper()
    if cleaned not in vocabulary:
        raise ValidationError(
            f"{name} is not a known value",
            context={"field": name, "value": value, "allowed": sorted(vocabulary)},
        )
    return cleaned
