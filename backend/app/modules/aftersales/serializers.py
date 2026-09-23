"""After-sales read-path projections - ORM rows to the frozen wire shapes.

PHASE5_DESIGN section 7 and ``frontend/src/types/domain.ts``: the response shapes are
the frontend's frozen ``AfterSale`` / ``RefundRecord``, so this module's whole job is
to render one from what is actually in the database, **field by field**.

## Why not ``model_validate(row)``

Four things happen here that no ORM projection can express:

1. **The nested collections are fetched deliberately.** ``items[]`` comes from
   ``after_sale_items`` joined to ``order_items`` (the snapshot), and ``refunds[]``
   from ``refunds``. A detail read is a fixed number of queries rather than whatever
   lazy loading happens to do - and the *summary* shape has no collections at all,
   which is what keeps the console queue off an N+1.
2. **The snapshot rule (INV-014).** ``product_name``/``sku_name``/``unit_price`` are
   read from ``order_items``, never from the live catalogue. A claim filed last month
   must still render the product as it was bought.
3. **Server-owned money figures.** ``refundable_amount`` / ``refund_cap`` are
   computed from the *order* and the *claim*, not the claim alone: the real cap is
   the smallest of what remains approved, what the payment can still cover, and what
   the lines can still carry. A client deriving it from ``approved - refunded``
   builds a form whose submit always fails with ``REFUND_EXCEEDS_PAID_AMOUNT``.
4. **Explicit absence.** An omitted field is never a silent ``null`` (section 2):
   every field below is written from a named source, so "not set" stays
   distinguishable from "not projected".

## Types are the real ORM classes, not ``object``

Taking ``AfterSale``/``Refund``/``Order`` rather than a duck-typed ``object`` is what
lets this module read attributes directly. A serializer written against ``object``
has to use ``getattr(row, "x", default)`` everywhere, and that default silently
papers over the one case that matters: a field renamed in the model, which would then
be projected as ``0``/``""``/``None`` on every response instead of failing the read.

## No masking here, and why that is not an oversight

Order serializers mask the receiver (section 94) because an order carries PII. An
after-sales claim carries a reason, a description and evidence URLs - the customer's
own words about their own purchase, returned only to their owner (the customer
endpoints filter by ``user_id``) or to merchant staff on the console. The rule that
applies is row-level scope, enforced in the repository query, not masking.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from app.core.errors import ValidationError
from app.modules.aftersales.enums import AfterSaleClaimStatus
from app.modules.aftersales.models import AfterSale, AfterSaleItem, Refund
from app.modules.aftersales.schemas import (
    AfterSaleDetailOut,
    AfterSaleItemOut,
    AfterSaleSummaryOut,
    RefundOut,
)
from app.modules.order.models import Order, OrderItem

__all__ = [
    "CLAIM_STATUSES",
    "claim_is_complete",
    "claimed_quantity_by_order_item",
    "operator_label",
    "order_payable_unrefunded",
    "refund_cap",
    "to_detail",
    "to_refund",
    "to_summary",
]

#: The claim vocabulary, re-exported so the API layer has one import site for what
#: it renders - matching the order module's style.
CLAIM_STATUSES: tuple[str, ...] = tuple(member.value for member in AfterSaleClaimStatus)


def operator_label(operator_type: str | None, operator_id: int | None) -> str | None:
    """Render the refund's actor for display (``RefundRecord.operator``).

    The columns stay in the database for audit; the wire shape is the display shape.
    ``STAFF #12`` rather than ``#12`` because the console shows this next to a
    "system" refund from a reconciliation job, and an operator who cannot tell a
    person from an automated writer will escalate the wrong one.
    """
    if not operator_type:
        return None
    if operator_id is None:
        return operator_type
    return f"{operator_type} #{operator_id}"


def order_payable_unrefunded(order: Order) -> int:
    """What the order's **lines** can still carry, floored at zero.

    Not ``order.refundable_amount``: that property is ``paid - refunded``, which is
    the correct cap on money out and is what the workflow's cap 1 uses. This is the
    line-level basis, ``sum(payable) - sum(refunded)`` over ``order_items``, which is
    the sum the per-line cap is measured against. The two agree on every well-formed
    order (INV-006 makes ``sum(item.payable) == order.payable``, and the workflow
    writes both axes from the same refund); they stay separate because they are
    enforced by different caps, and conflating them would hide which one failed.
    """
    return max(
        sum(item.payable_amount for item in order.items)
        - sum(item.refunded_amount for item in order.items),
        0,
    )


def claimed_quantity_by_order_item(items: Iterable[AfterSaleItem]) -> dict[int, int]:
    """Sum claimed quantities per order line across a set of ``after_sale_items``.

    Answers "how many units of this line are already spoken for" when a new claim
    arrives. Without it, two claims of three units each on a line of four would both
    look eligible, and the second would fail only at the refund step - after an
    operator had approved it and told the customer so.
    """
    totals: dict[int, int] = {}
    for row in items:
        totals[row.order_item_id] = totals.get(row.order_item_id, 0) + int(row.quantity)
    return totals


def _item_out(row: AfterSaleItem, order_item: OrderItem | None) -> AfterSaleItemOut:
    """One claimed line: the claimed quantity plus the *order line's* snapshot.

    ``order_item`` comes from a map the caller built from ``order_items`` for this
    order - one query for the whole detail read, not one per line. ``None`` is
    reachable only for a claim written before the "line belongs to the order" guard
    existed, and degrades to the claimed quantity with zeroed snapshot columns rather
    than raising: a read path must not answer 500 for a historical row.
    """
    return AfterSaleItemOut(
        id=row.id,
        order_item_id=str(row.order_item_id),
        sku_id=order_item.sku_id if order_item is not None else 0,
        product_name=order_item.product_name if order_item is not None else "",
        sku_name=order_item.sku_name if order_item is not None else "",
        quantity=int(row.quantity),
        unit_price=order_item.unit_price if order_item is not None else 0,
        payable_amount=order_item.payable_amount if order_item is not None else 0,
        refunded_amount=order_item.refunded_amount if order_item is not None else 0,
    )


def _refund_out(row: Refund, *, after_sale_no: str | None = None) -> RefundOut:
    """One refund row as the frontend's ``RefundRecord``.

    ``after_sale_no`` is passed in rather than read from the row, because the ``refunds``
    table is normalised: it carries ``after_sale_id`` (a ``RESTRICT`` foreign key to the claim)
    and not the claim's public identifier. The frozen ``RefundRecord`` shape in
    ``frontend/src/types/domain.ts`` requires ``after_sale_no``, so it has to be resolved from
    the claim - which the caller always has to hand, because it just executed or listed that
    claim's refunds. Denormalising the column onto ``refunds`` would be a migration and would
    also create a second copy of the identifier to keep in step, so resolving it at the
    serializer boundary is both cheaper and safer.

    An explicit *parameter* rather than a lazy ``row.after_sale`` relationship lookup keeps the
    query count predictable: the refund list for a claim is one query plus the claim it already
    loaded, not one query per refund row.
    """
    resolved = after_sale_no
    if resolved is None:  # pragma: no cover - every call site passes it
        resolved = getattr(row, "after_sale_no", None)
    if resolved is None:
        raise ValidationError(
            "a refund record needs its claim's identifier to be rendered",
            context={"refund_id": row.id, "after_sale_id": row.after_sale_id},
        )
    return RefundOut(
        id=str(row.id),
        after_sale_no=resolved,
        order_no=row.order_no,
        amount=row.amount,
        status=str(row.status),
        reason=row.reason,
        operator=operator_label(row.operator_type, row.operator_id),
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


def to_refund(row: Refund, *, after_sale_no: str) -> RefundOut:
    """Public entry point for the refund shape.

    The one place that decides what a refund looks like on the wire. The API layer calls this
    rather than building a ``RefundOut`` itself, so the endpoint that *executes* a refund and
    the endpoint that *lists* them cannot render two slightly different records for the same
    row - the kind of drift that shows up as a console column blank on one screen and filled
    on another.

    ``after_sale_no`` is required (see ``_refund_out``): the row holds the id, the wire shape
    holds the identifier, and the caller is the layer that knows both.
    """
    return _refund_out(row, after_sale_no=after_sale_no)


def to_summary(claim: AfterSale) -> AfterSaleSummaryOut:
    """One claim row, without its collections (see the module docstring).

    ``status`` is the **claim** status, from ``after_sales.claim_status``. It is not
    the order's ``after_sale_status`` and the two are not derived from each other
    (PHASE5_DESIGN section 4.4): a claim can be ``COMPLETED`` while its order is only
    ``PARTIAL_REFUNDED``, and an order can be ``REFUNDED`` on one completed claim
    while a second claim is still ``PENDING``.
    """
    return AfterSaleSummaryOut(
        id=str(claim.id),
        after_sale_no=claim.after_sale_no,
        order_no=claim.order_no,
        type=str(claim.type),
        status=str(claim.claim_status),
        requested_amount=claim.requested_amount,
        approved_amount=claim.approved_amount,
        refunded_amount=claim.refunded_amount,
        reason=claim.reason,
        description=claim.description,
        evidence_urls=claim.evidence_urls,
        reject_reason=claim.reject_reason,
        created_at=claim.created_at,
        processed_at=claim.processed_at,
    )


def refund_cap(
    claim: AfterSale,
    *,
    order: Order | None,
    order_item_refunded: Mapping[int, int] | None = None,
) -> int:
    """The largest refund this claim may still execute, as of *these* rows.

    The **minimum** of three caps, because a refund must satisfy all three and the
    binding one is whichever is smallest:

    * the claim's remaining approval, ``approved - refunded``;
    * cap 1 (money): ``order.paid_amount - order.refunded_amount``;
    * cap 2 (lines): what the claim's lines can still carry, from
      ``order_item_refunded`` when the caller has it, otherwise from the loaded
      ``order.items``.

    Deliberately **not** a decision - it is a *display* figure for the form's
    ``max``. The workflow recomputes all three inside the row locks and refuses on
    the first that is exceeded; this function existing does not authorize anything
    (PHASE5_DESIGN section 8: application checks are not the boundary).
    """
    remaining = max(claim.approved_amount - claim.refunded_amount, 0)
    if order is None:
        return remaining

    money_left = max(int(order.paid_amount) - int(order.refunded_amount), 0)
    line_ids = {row.order_item_id for row in claim.items}
    if order_item_refunded is None:
        lines_left = sum(
            max(item.payable_amount - item.refunded_amount, 0)
            for item in order.items
            if item.id in line_ids
        )
    else:
        lines_left = sum(
            max(
                item.payable_amount - int(order_item_refunded.get(item.id, item.refunded_amount)),
                0,
            )
            for item in order.items
            if item.id in line_ids
        )
    return max(min(remaining, money_left, lines_left), 0)


def to_detail(
    claim: AfterSale,
    *,
    order: Order | None,
    items: Sequence[AfterSaleItem],
    refunds: Sequence[Refund],
    order_items: Mapping[int, OrderItem] | None = None,
    order_item_refunded: Mapping[int, int] | None = None,
) -> AfterSaleDetailOut:
    """The frontend's ``AfterSale``, with both collections and the server's cap.

    ``order`` is optional so a console can render a claim whose order row is
    momentarily unreadable (replica lag; a support tool reading one table) without
    the whole detail read failing. When it is absent the cap falls back to the
    claim's own remaining approval - the console's refund form is then *stricter*
    than it needs to be, which is the correct direction for a cap to fail in.
    """
    by_id: Mapping[int, OrderItem]
    if order_items is not None:
        by_id = order_items
    elif order is not None:
        by_id = {item.id: item for item in order.items}
    else:
        by_id = {}

    summary = to_summary(claim)
    cap = refund_cap(claim, order=order, order_item_refunded=order_item_refunded)
    return AfterSaleDetailOut(
        **summary.model_dump(),
        items=[_item_out(row, by_id.get(row.order_item_id)) for row in items],
        refunds=[_refund_out(row, after_sale_no=claim.after_sale_no) for row in refunds],
        refundable_amount=cap,
        refund_cap=cap,
        completed_at=claim.completed_at,
    )


def claim_is_complete(claim: AfterSale) -> bool:
    """Whether the claim's own approved amount has been fully refunded.

    A named predicate rather than an inline comparison, because it is the rule that
    moves ``claim_status -> COMPLETED`` and the naive form is wrong: a claim whose
    ``approved_amount`` is 0 must not be considered complete, and ``0 >= 0`` is true
    - which would complete every never-approved claim the moment somebody asked.
    """
    return claim.approved_amount > 0 and claim.refunded_amount >= claim.approved_amount
