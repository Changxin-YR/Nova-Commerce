"""Console after-sales endpoints - ``/api/v1/after-sales/admin/*``.

The frozen frontend paths plus the two task endpoints section 99 requires:
``.../approve``, ``.../reject`` and ``.../refund`` are **tasks**, not
``PATCH {status}`` writes. That shape is not cosmetic: each of the three has a
different precondition, a different set of consequences, and a different permission,
and a generic status editor would let a caller express combinations that are not real
(approving a rejected claim; refunding a pending one without approving it).

``ConsolePrincipal`` enforces the account type and the merchant scope before any
handler body runs, and the merchant filter is applied again **inside** the service's
queries, so a scoped principal cannot widen its own result set.

## The refund endpoint's two idempotency inputs

``Idempotency-Key`` (header) and ``idempotency_key`` (body) must agree. The frontend
sends both because it puts the value in the body for the request and in the header for
the transport, and the handler refuses a **mismatch** rather than preferring one:
preferring the header would let a client that retried with a fresh header while
replaying an old body be handed a second refund for what it believed was a retry, and
preferring the body would silently turn a deliberate second refund into a replay of
the first. ``IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD (10011)`` is not the right
code here - nothing was reused yet - so a mismatch is a plain ``VALIDATION_ERROR``
naming both values.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.orm import Session

from app.core.errors import IdempotencyKeyRequiredError, ValidationError, envelope
from app.modules.aftersales.schemas import (
    ApproveAfterSaleRequest,
    RefundRequest,
    RejectAfterSaleRequest,
    page_meta,
)
from app.modules.aftersales.serializers import to_refund, to_summary
from app.modules.aftersales.service import AfterSaleService
from app.modules.aftersales.views import detail_view
from app.modules.aftersales.workflow import RefundWorkflow
from app.modules.identity.dependencies import ConsolePrincipal
from app.shared.db.session import get_session

router = APIRouter()

SessionDep = Annotated[Session, Depends(get_session)]

IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key", max_length=128)]


@router.get(
    "/admin/after-sales",
    summary="List after-sales claims (console)",
    description=(
        "Paged envelope per section 3. Filters: `status` (the **claim** vocabulary), "
        "`order_no`. Both are applied in the query, so a claim outside the caller's "
        "merchant scope is never fetched."
    ),
)
def list_admin_after_sales(
    principal: ConsolePrincipal,
    session: SessionDep,
    status: Annotated[str | None, Query(max_length=32)] = None,
    order_no: Annotated[str | None, Query(max_length=32)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    result = AfterSaleService(session).list_admin_claims(
        principal=principal,
        claim_status=status,
        order_no=order_no,
        page=page,
        page_size=page_size,
    )
    return envelope(
        data={
            "items": [to_summary(row).model_dump(mode="json") for row in result.rows],
            "meta": page_meta(page=page, page_size=page_size, total=result.total).model_dump(),
        }
    )


@router.get(
    "/admin/after-sales/{after_sale_no}",
    summary="One after-sales claim (console)",
    description=(
        "The same `AfterSale` the customer surface returns - the console deliberately has "
        "no higher-fidelity variant, so the two views of one claim cannot show different "
        "`refund_cap` figures."
    ),
)
def get_admin_after_sale(
    after_sale_no: str, principal: ConsolePrincipal, session: SessionDep
) -> dict:
    service = AfterSaleService(session)
    claim = service.get_admin_claim(principal=principal, after_sale_no=after_sale_no)
    detail = detail_view(session=session, service=service, claim=claim)
    return envelope(data=detail.model_dump(mode="json"))


@router.post(
    "/admin/after-sales/{after_sale_no}/approve",
    summary="Approve an after-sales claim",
    description=(
        "Task endpoint (section 99). Approving writes the claim's status and amount and "
        "**nothing else**: no order axis and no stock, because the money has not moved "
        "yet. An approval that credited returned goods would book stock for a parcel that "
        "has not arrived.\n\n"
        "`approved_amount` may be **lower** than requested, never higher - V1 policy, "
        "which is why it is enforced here rather than by a database constraint (section "
        "5.5): relaxing it later must be a service change, not a migration."
    ),
)
def approve_after_sale(
    after_sale_no: str,
    payload: ApproveAfterSaleRequest,
    principal: ConsolePrincipal,
    session: SessionDep,
) -> dict:
    claim = AfterSaleService(session).approve(
        principal=principal, after_sale_no=after_sale_no, payload=payload
    )
    return envelope(data=to_summary(claim).model_dump(mode="json"))


@router.post(
    "/admin/after-sales/{after_sale_no}/reject",
    summary="Reject an after-sales claim",
    description=(
        "Task endpoint (section 99). Reachable from `PENDING` and from `APPROVED` - a "
        "merchant that approved and then found a problem must be able to say no, and "
        "refusing that would push the operator into a manual database edit. Not reachable "
        "once money has moved: `AFTER_SALE_STATE_INVALID (80002)`, because `REJECTED` "
        "beside a completed refund is a contradiction the console would render as a "
        "support ticket. `reject_reason` is required - the customer is told why."
    ),
)
def reject_after_sale(
    after_sale_no: str,
    payload: RejectAfterSaleRequest,
    principal: ConsolePrincipal,
    session: SessionDep,
) -> dict:
    claim = AfterSaleService(session).reject(
        principal=principal, after_sale_no=after_sale_no, payload=payload
    )
    return envelope(data=to_summary(claim).model_dump(mode="json"))


@router.post(
    "/admin/after-sales/{after_sale_no}/refund",
    summary="Execute the refund (RefundWorkflow)",
    description=(
        "Executes `RefundWorkflow` in one transaction. **Both caps are revalidated "
        "inside the row locks against freshly read rows**, never from the figures this "
        "request arrived with:\n\n"
        "* `REFUND_EXCEEDS_PAID_AMOUNT (80004)` - the payment cannot give back more than "
        "it received;\n"
        "* `REFUND_EXCEEDS_ITEM_AMOUNT (80005)` - no line may be refunded beyond its own "
        "payable amount, cumulatively across every refund that has touched it;\n"
        "* `REFUND_AMOUNT_INVALID (80006)` - non-positive, or above the claim's approved "
        "amount;\n"
        "* `REFUND_ALREADY_COMPLETED (80007)` - the same key used for a different "
        "amount.\n\n"
        "A retry of **the same** key and amount is an idempotent success returning the "
        "original refund row: `refunds.uq_refunds_merchant_idempotency_key` is the "
        "decision, so a retried request cannot send money twice.\n\n"
        "`RETURN_REFUND` claims append a `RETURN_IN` stock movement; `REFUND_ONLY` claims "
        "move no stock, because the customer kept the goods."
    ),
)
def refund_after_sale(
    after_sale_no: str,
    payload: RefundRequest,
    principal: ConsolePrincipal,
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> dict:
    key = _agreed_idempotency_key(header=idempotency_key, body=payload.idempotency_key)
    outcome = RefundWorkflow(session).execute(
        principal=principal,
        after_sale_no=after_sale_no,
        amount=payload.amount,
        reason=payload.reason,
        idempotency_key=key,
    )
    # The claim was loaded by the workflow (and is where the identifier lives), so the refund
    # record is rendered with it: `refunds` carries `after_sale_id`, the wire shape carries
    # `after_sale_no`.
    claim = AfterSaleService(session).get_admin_claim(
        principal=principal, after_sale_no=after_sale_no
    )
    return envelope(data=to_refund(outcome.refund, after_sale_no=claim.after_sale_no).model_dump(mode="json"))


def _agreed_idempotency_key(*, header: str | None, body: str) -> str:
    """Both idempotency inputs, or a refusal that names the disagreement.

    See the module docstring for why a mismatch is refused rather than resolved. The
    body copy is the one the frontend's retry logic reads, so it must not be ignored -
    but neither may the header be, because it is the value the transport retried with.
    """
    if header is None or not header.strip():
        raise IdempotencyKeyRequiredError("Idempotency-Key header is required")
    if header != body:
        raise ValidationError(
            "the Idempotency-Key header and the request body's idempotency_key must match",
            context={"header_key": header, "body_key": body},
        )
    return body
