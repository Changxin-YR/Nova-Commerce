"""After-sales wire shapes - requests and responses (PHASE5_DESIGN 搂7).

The paths are the **frozen frontend paths**
(``frontend/src/api/endpoints.ts::afterSales``), and the response shapes are the
frontend's frozen ``AfterSale`` and ``RefundRecord`` interfaces
(``frontend/src/types/domain.ts``). Where this module and the frontend disagree,
the frontend wins: it is the contract, and this is the implementation.

## Two request shapes that refuse what the frontend would send

1. ``ApplyAfterSaleRequest`` carries ``client_request_id`` **and** requires an
   ``Idempotency-Key`` header (the frontend sends the same value in both, which is
   the order-create precedent). Two guards, at two layers: the header is
   namespaced through ``idempotency_records``, and ``UNIQUE (user_id,
   client_request_id)`` on ``after_sales`` catches the client that lost the header.
2. ``RefundRequest`` carries ``idempotency_key`` in the **body** as well as the
   ``Idempotency-Key`` header. The frontend sends both, so both are validated and a
   **mismatch is refused** rather than silently preferring one: a client that sends
   a fresh key in the header while replaying an old body would otherwise get a
   second refund for what it believed was a retry - and the reverse would silently
   turn a deliberate second refund into a replay of the first.

## Amounts are integers in minor units, and validated as such

Every money field is ``int`` with a bound. ``float`` is refused by the type before
any business code runs, which is the same protection
:class:`app.shared.db.types.MoneyMinor` applies at the persistence boundary - two
layers, because "19.99" arriving as a float and being truncated is a class of bug
that is invisible in a test that only checks a happy path.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.aftersales.enums import (
    AFTER_SALE_CLAIM_STATUSES,
    AFTER_SALE_TYPES,
    CARRIER_CODES,
    REFUND_STATUSES,
)
from app.modules.order.schemas import MetaOut, UtcTimestamp, page_meta as page_meta_impl

__all__ = [
    "MAX_CLAIM_LINES",
    "AfterSaleDetailOut",
    "AfterSaleItemOut",
    "AfterSaleSummaryOut",
    "ApplyAfterSaleItemIn",
    "ApplyAfterSaleRequest",
    "ApproveAfterSaleRequest",
    "CancelAfterSaleRequest",
    "MetaOut",
    "RefundOut",
    "RefundRequest",
    "RejectAfterSaleRequest",
    "ReturnShipmentRequest",
    "UtcTimestamp",
    "page_meta",
]


def page_meta(*, page: int, page_size: int, total: int) -> MetaOut:
    """The 搂3 paging meta, from the order module's single implementation.

    Re-exported rather than reimplemented: two implementations of
    ``total_pages`` is exactly how one surface starts answering "1 of 0 pages"
    for an empty result while the other answers "0 of 0".
    """
    return page_meta_impl(page=page, page_size=page_size, total=total)


#: A ceiling on distinct lines - a rail against a fat-fingered or hostile body
#: building an unbounded transaction, not a merchandising rule. Same reasoning as
#: ``MAX_ORDER_LINES`` in the order module.
MAX_CLAIM_LINES = 100


# ---------------------------------------------------------------------------
# Responses - the frontend's frozen shapes
# ---------------------------------------------------------------------------
class AfterSaleItemOut(BaseModel):
    """One claimed line, as the frontend's ``AfterSale.items[]`` expects it.

    ``order_item_id`` is a **string** on the wire (``{order_item_id: string,
    quantity: number}`` in ``domain.ts``), while ``id`` is the frontend's ``Id``
    alias. Both are rendered from the integer columns. The conversion happens here
    rather than in every handler so there is exactly one place that decides, and
    the frozen shape is not something each handler re-interprets.

    The snapshot fields (``product_name``, ``sku_name``, ``unit_price``,
    ``payable_amount``, ``refunded_amount``) are read from ``order_items`` - never
    from the live catalogue (INV-014) and never recomputed from today's pricing.
    """

    id: int
    order_item_id: str
    sku_id: int
    product_name: str
    sku_name: str
    quantity: int
    unit_price: int
    payable_amount: int
    refunded_amount: int

    @field_validator("order_item_id", mode="before")
    @classmethod
    def _stringify_order_item_id(cls, value: object) -> str:
        # ``mode="before"`` so an int from the ORM is accepted and rendered as the
        # string the frozen type declares, rather than being a strict-mode failure
        # at serialisation time on a read path that has nothing to validate.
        return str(value)


class RefundOut(BaseModel):
    """``RefundRecord`` from ``domain.ts``, field for field.

    ``operator`` is a human-readable actor ("STAFF #12"), not an enum: the console
    renders it directly and the frontend's type is a plain optional string. The raw
    ``operator_type``/``operator_id`` columns stay in the database for audit, where
    they belong - the wire shape is the display shape.
    """

    id: str
    after_sale_no: str
    order_no: str
    amount: int
    status: str
    reason: str | None = None
    operator: str | None = None
    created_at: UtcTimestamp
    completed_at: UtcTimestamp | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _stringify_id(cls, value: object) -> str:
        return str(value)

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        if value not in REFUND_STATUSES:
            raise ValueError(f"unknown refund status {value!r}")
        return value


class AfterSaleSummaryOut(BaseModel):
    """A list row. Deliberately carries **no** ``items[]`` and no ``refunds[]``.

    The console queue is where an N+1 hides: one row per claim times one query for
    its items plus one for its refunds, on a page of twenty, is forty queries
    nobody notices until the queue is five thousand rows deep. The detail endpoint
    is where the nested collections live.
    """

    id: str
    after_sale_no: str
    order_no: str
    type: str
    status: str
    requested_amount: int
    approved_amount: int
    refunded_amount: int
    reason: str
    description: str | None = None
    evidence_urls: list[str] | None = None
    reject_reason: str | None = None
    created_at: UtcTimestamp
    processed_at: UtcTimestamp | None = None

    @field_validator("id", mode="before")
    @classmethod
    def _stringify_id(cls, value: object) -> str:
        return str(value)

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in AFTER_SALE_TYPES:
            raise ValueError(f"unknown after-sale type {value!r}")
        return value

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        if value not in AFTER_SALE_CLAIM_STATUSES:
            raise ValueError(f"unknown claim status {value!r}")
        return value


class AfterSaleDetailOut(AfterSaleSummaryOut):
    """The frontend's ``AfterSale``, exactly: a summary plus both collections.

    ``refundable_amount`` and ``refund_cap`` are the two **server-owned** additions
    (INV-005's reasoning, applied to money out): the console renders the refund
    form's ``max`` from them. Deriving the figure client-side from
    ``approved_amount - refunded_amount`` would be wrong, because the real cap is
    the *smaller* of the claim's remaining approval and what the payment can still
    cover - so a client-side derivation enables a form whose submit always fails
    with ``REFUND_EXCEEDS_PAID_AMOUNT``. A money figure that decides whether a
    control is enabled belongs next to the rule that enforces it.
    """

    items: list[AfterSaleItemOut] = Field(default_factory=list)
    refunds: list[RefundOut] = Field(default_factory=list)
    #: ``min(approved - refunded, order payables still unrefunded)``, floored at 0.
    refundable_amount: int = 0
    #: The same figure, named for the form's ``max`` attribute.
    refund_cap: int = 0
    completed_at: UtcTimestamp | None = None


class ApplyAfterSaleItemIn(BaseModel):
    """One line of the claim: which order line, how many units.

    **No price field.** The amount a line can be refunded is computed from the
    order's own persisted ``payable_amount`` (PHASE5_DESIGN 搂5.5, INV-014), never
    read from the request - trusting a client-supplied unit price here would make
    the refund cap a client input, and the cap is the whole of FG-12.
    """

    model_config = ConfigDict(extra="forbid")

    order_item_id: int = Field(gt=0, description="The order line being claimed.")
    quantity: int = Field(gt=0, le=1_000_000, description="Units claimed from that line.")


class ApplyAfterSaleRequest(BaseModel):
    """``POST /api/v1/after-sales/customer/after-sales`` (搂7).

    ``requested_amount`` is the customer's **ask**, and it is capped server-side
    against what the order can actually still refund. A request above that cap is
    ``AFTER_SALE_NOT_ELIGIBLE (80001)`` - refused at apply time rather than
    approved later, because an approve step that can never be refunded is a queue
    item that wastes an operator's attention and then fails at the money step.
    """

    model_config = ConfigDict(extra="forbid")

    order_no: str = Field(min_length=1, max_length=32)
    type: str = Field(description="REFUND_ONLY or RETURN_REFUND.")
    items: list[ApplyAfterSaleItemIn] = Field(
        min_length=1,
        max_length=MAX_CLAIM_LINES,
        description="The lines and quantities being claimed.",
    )
    requested_amount: int = Field(gt=0, description="Integer minor units requested by the buyer.")
    reason: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=1000)
    evidence_urls: list[str] | None = Field(default=None, max_length=20)
    client_request_id: str = Field(
        min_length=1,
        max_length=64,
        description="Client-generated idempotency token; unique per customer (second guard).",
    )

    @field_validator("type")
    @classmethod
    def _type_in_vocabulary(cls, value: str) -> str:
        # Validated against the enum here rather than with a ``Literal`` so the two
        # vocabularies cannot drift: adding a member to ``AfterSaleType`` widens
        # this automatically, while a ``Literal`` would silently keep rejecting it
        # in the API layer only.
        if value not in AFTER_SALE_TYPES:
            raise ValueError(f"type must be one of {AFTER_SALE_TYPES}")
        return value

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reason must not be blank")
        return cleaned

    @field_validator("client_request_id")
    @classmethod
    def _client_request_id_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("client_request_id must not be blank")
        return cleaned

    @field_validator("items")
    @classmethod
    def _no_duplicate_lines(
        cls, value: list[ApplyAfterSaleItemIn]
    ) -> list[ApplyAfterSaleItemIn]:
        """Two entries for one line are merged, not refused.

        The order-create path merges duplicate SKUs before pricing
        (API_CONTRACT 搂14.2), and the same reasoning applies here: a client that
        lists line 7 twice is describing one intention, and refusing it would be a
        validation error the user cannot act on. Merging in the validator keeps the
        service's per-line arithmetic unambiguous - and
        ``UNIQUE (after_sale_id, order_item_id)`` would reject the second row
        anyway, which would surface as a 500 rather than a merge.

        The merged quantity keeps the *first* occurrence's position so the stored
        order of lines is the client's order.
        """
        merged: dict[int, ApplyAfterSaleItemIn] = {}
        for line in value:
            existing = merged.get(line.order_item_id)
            if existing is None:
                merged[line.order_item_id] = ApplyAfterSaleItemIn(
                    order_item_id=line.order_item_id, quantity=line.quantity
                )
            else:
                merged[line.order_item_id] = ApplyAfterSaleItemIn(
                    order_item_id=line.order_item_id,
                    quantity=existing.quantity + line.quantity,
                )
        return list(merged.values())


class CancelAfterSaleRequest(BaseModel):
    """``POST .../cancel``. An optional reason; the claim has to be ``PENDING``."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def _blank_becomes_absent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class ApproveAfterSaleRequest(BaseModel):
    """``POST .../approve`` (搂99 task endpoint).

    The approved amount **may be lower** than requested. It may not be higher:
    V1's policy is that the merchant approves at most what was asked
    (PHASE5_DESIGN 搂5.5 - deliberately not a database constraint, because that is
    a policy that a later phase could relax without a migration, while the two
    money caps *are* constraints because they are arithmetic truths).
    """

    model_config = ConfigDict(extra="forbid")

    approved_amount: int = Field(
        gt=0, description="Integer minor units. Must not exceed the requested amount."
    )
    remark: str | None = Field(default=None, max_length=500)


class RejectAfterSaleRequest(BaseModel):
    """``POST .../reject``. A reason is required - the customer is told why."""

    model_config = ConfigDict(extra="forbid")

    reject_reason: str = Field(min_length=1, max_length=500)

    @field_validator("reject_reason")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("reject_reason must not be blank")
        return cleaned


class RefundRequest(BaseModel):
    """``POST /after-sales/admin/after-sales/{after_sale_no}/refund`` (PHASE5_DESIGN 搂6.2).

    Two idempotency fields, and the reason both exist rather than one:

    * the ``Idempotency-Key`` **header** is the claim this endpoint takes through
      ``IdempotencyRepository`` with scope ``refund:execute``;
    * ``idempotency_key`` in the **body** is what the frontend sends, because it
      cannot set a header and read the body in one place without duplicating the
      value.

    They must agree. Preferring one silently splits a single logical key into two
    namespaces, and the failure that follows is asymmetric: a client that retries
    with a fresh header but a replayed body would be handed a second refund for
    what it believes is a retry.

    ``amount`` is revalidated in the workflow against **freshly locked** rows. This
    validator only rejects what is impossible on its face (a non-positive amount);
    every cap decision belongs inside the lock (PHASE5_DESIGN 搂8: "application
    checks are not the boundary").
    """

    model_config = ConfigDict(extra="forbid")

    amount: int = Field(gt=0, description="Integer minor units to refund.")
    reason: str | None = Field(default=None, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def _key_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("idempotency_key must not be blank")
        return cleaned

    @field_validator("reason")
    @classmethod
    def _blank_becomes_absent(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class ReturnShipmentRequest(BaseModel):
    """Optional console input recording the returned parcel's tracking data.

    Not a status write (搂99 forbids a generic ``PATCH {status}``) and not a
    required one: the refund is the money fact, and it must not be blocked on an
    operator having the courier's number to hand. When supplied, the ``carrier`` is
    validated against the same allowlist the fulfillment path uses - see
    ``CARRIER_CODES``.
    """

    model_config = ConfigDict(extra="forbid")

    carrier: str = Field(description=f"One of {CARRIER_CODES}.")
    tracking_no: str = Field(min_length=1, max_length=64)

    @field_validator("carrier")
    @classmethod
    def _carrier_in_allowlist(cls, value: str) -> str:
        if value not in CARRIER_CODES:
            raise ValueError(f"carrier must be one of {CARRIER_CODES}")
        return value

    @field_validator("tracking_no")
    @classmethod
    def _tracking_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("tracking_no must not be blank")
        return cleaned


def _validate_status_filter(value: str | None) -> str | None:
    """Refuse a claim-status filter that can never match.

    An operator who filters on a status that does not exist sees an empty list and
    concludes there is no such work - the same defect the order console list guards
    against. The check lives here so both the customer and the console path share
    it.
    """
    if value is None:
        return None
    if value not in AFTER_SALE_CLAIM_STATUSES:
        raise ValueError(f"status must be one of {AFTER_SALE_CLAIM_STATUSES}")
    return value


__all__ += ["MAX_CLAIM_LINES"]
