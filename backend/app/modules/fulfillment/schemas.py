"""Fulfillment request/response models.

## ``POST /fulfillments/{id}/ship`` accepts **exactly three fields**

That is not a style choice. Section 110's mass-assignment rule, applied to a task
endpoint, means the body may name *what to ship* and nothing else:

* ``carrier`` - the code the parcel travels with (validated against the closed
  allowlist of ``fulfillment/enums.py``);
* ``tracking_no`` - the operator's tracking number;
* ``item_quantities`` - which lines of *this package* are in *this parcel*, and how
  many units of each.

Everything else about the consequence is server-owned and therefore absent from the
model: the order's ``fulfillment_status`` is recomputed from all of the order's
packages rather than asserted by the client; ``shipped_at`` is stamped from the
server clock; ``fulfillment_status`` on the row is set by the state guard. A field
that is not in the model cannot be smuggled in through ``extra="forbid"`` either -
Pydantic rejects it rather than ignoring it, so a client that tries to set
``fulfillment_status: "SHIPPED"`` gets a 422 instead of a silently discarded wish.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.fulfillment.enums import CARRIER_CODES
from app.modules.order.schemas import FulfillmentItemOut, FulfillmentOut, MetaOut, page_meta

__all__ = [
    "MAX_SHIP_LINES",
    "FulfillmentItemOut",
    "FulfillmentOut",
    "FulfillmentPageOut",
    "MetaOut",
    "ShipFulfillmentRequest",
    "ShipLineIn",
    "normalise_carrier",
    "page_meta",
]


class FulfillmentPageOut(BaseModel):
    """The paged payload of a fulfillment list (API_CONTRACT section 3).

    ``items``/``meta`` is the frozen list envelope; it is declared here rather than
    imported from the order module because the order module has no fulfillment queue
    and should not grow one just to lend its type.

    **The element type is the order module's** ``FulfillmentOut``, not a second copy
    of the same fields. It is the identical frozen shape (``API_CONTRACT`` section 5,
    mirrored by ``frontend/src/types/frozen-contract.ts``), and the order detail
    already returns these objects inside ``shipments[]``. Declaring a parallel
    ``FulfillmentOut`` here would mean two Python classes for one wire object - the
    exact "two shapes for one resource" defect ``frontend/src/types/domain.ts``
    deleted its old models over - and the first divergence would be an admin queue
    that disagrees with the order the customer is looking at.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[FulfillmentOut] = Field(default_factory=list)
    meta: MetaOut


def normalise_carrier(value: str) -> str:
    """Canonicalise a carrier code from client input, or refuse it.

    Uppercases and strips surrounding whitespace **before** the allowlist check, so
    ``"sf"`` and ``" SF "`` are accepted as ``SF`` rather than rejected as unknown.
    Normalising first is the difference between a strict vocabulary and a hostile
    one: the goal is to refuse carriers we do not know, not to refuse the same
    carrier typed by a human.

    **Why this lives here rather than in ``enums.py``.** The *vocabulary*
    (``Carrier``/``CARRIER_CODES``) is the data layer's, and it is imported rather
    than re-declared - one source of truth for the migration's ``CHECK``. The
    *coercion* of untrusted input is an edge concern that belongs with the request
    model that accepts it, so it sits next to the only thing that calls it. If
    ``enums.py`` ever gains a canonical normaliser, this delegates to it in one line.

    Raises :class:`ValueError`; Pydantic converts that into a validation error,
    which the handler renders as ``VALIDATION_ERROR (10 001)`` - never a stored
    arbitrary string.
    """
    if not isinstance(value, str):
        msg = f"carrier must be a string code, got {type(value).__name__}"
        raise ValueError(msg)
    canonical = value.strip().upper()
    if canonical not in CARRIER_CODES:
        msg = f"unknown carrier code {value!r}; expected one of {', '.join(CARRIER_CODES)}"
        raise ValueError(msg)
    return canonical


#: A ceiling on distinct lines in one shipment request. Like ``MAX_ORDER_LINES``
#: this is a rail, not merchandising policy: each line participates in the
#: cumulative per-line quantity check, and an unbounded list is unbounded work
#: inside a transaction that holds a ``FOR UPDATE`` lock on the fulfillment row.
MAX_SHIP_LINES = 100


class ShipLineIn(BaseModel):
    """One line of the parcel: an order line and how many units of it shipped.

    ``quantity`` is bounded below by 1. A zero quantity would create a shipped line
    that ships nothing - it would consume a row, make "some lines shipped" true in a
    naive rollup, and mean nothing. A caller that wants to ship nothing should send
    no line at all.
    """

    model_config = ConfigDict(extra="forbid")

    order_item_id: int = Field(gt=0, description="The order line this parcel ships.")
    quantity: int = Field(
        gt=0,
        description="Units of that line in this parcel. Must be positive.",
    )


class ShipFulfillmentRequest(BaseModel):
    """The frozen ship body: ``{carrier, tracking_no, item_quantities}``.

    **This model has exactly three fields and must keep exactly three.** The
    frontend's ``ShipFulfillmentRequest`` in ``frontend/src/types/frozen-contract.ts``
    is the mirror of this one; a field added here without adding it there is an
    integration defect, not a convenience.

    ``carrier`` and ``tracking_no`` are both required. A shipped parcel with no
    tracking number is untrackable (the customer's "where is my order" answer
    becomes "ask the operator"), and a parcel with no carrier is a parcel whose
    tracking number cannot be resolved to a URL. Both are cheap to require at the
    moment of shipping and impossible to reconstruct later - the operator who knew
    them is the one pressing the button.
    """

    model_config = ConfigDict(extra="forbid")

    carrier: str = Field(
        min_length=1,
        max_length=16,
        description="Carrier code from the frozen allowlist (e.g. `SF`, `YTO`, `JD`).",
    )
    tracking_no: str = Field(
        min_length=1,
        max_length=64,
        description="The carrier's tracking number for this parcel.",
    )
    item_quantities: list[ShipLineIn] = Field(
        min_length=1,
        max_length=MAX_SHIP_LINES,
        description="The lines of this package that are in this parcel.",
    )

    @field_validator("carrier")
    @classmethod
    def _validate_carrier(cls, value: str) -> str:
        """Normalise to the canonical code, or refuse.

        ``normalise_carrier`` raises ``ValueError`` on an unknown code; Pydantic
        converts that into a validation error, which the error handler renders as
        ``VALIDATION_ERROR (10 001)``. Refusing here rather than in the service is
        deliberate: an unknown carrier must never reach the database, and the
        request model is the last place that is true for *every* caller (HTTP, tool
        gateway, CLI) without each of them remembering to check.
        """
        return normalise_carrier(value)

    @field_validator("tracking_no")
    @classmethod
    def _validate_tracking_no(cls, value: str) -> str:
        """Trim the tracking number, and refuse one that is only whitespace.

        ``min_length=1`` alone would accept ``"   "``, which is a tracking number the
        console renders as blank and the customer cannot use. Stripping *before* the
        emptiness check is what makes the constraint mean what it says.
        """
        stripped = value.strip()
        if not stripped:
            msg = "tracking_no must not be blank"
            raise ValueError(msg)
        return stripped

    @field_validator("item_quantities")
    @classmethod
    def _reject_duplicate_lines(cls, value: list[ShipLineIn]) -> list[ShipLineIn]:
        """Refuse the same order line twice in one request.

        A duplicate is not a convenience to be summed: ``2`` then ``3`` for the same
        line is ambiguous about what the operator meant (5 units, or an accident of
        a double-clicked row?). Summing it would also make the cumulative check
        depend on list order rather than on the request as a whole, so the same
        semantic request could be accepted or refused depending on how the client
        happened to order two identical rows. Refusing is the honest answer, and it
        mirrors ``uq_order_items_order_sku`` - one row per line is the invariant
        everywhere else in this codebase.
        """
        seen: set[int] = set()
        for line in value:
            if line.order_item_id in seen:
                msg = f"order_item_id {line.order_item_id} appears more than once"
                raise ValueError(msg)
            seen.add(line.order_item_id)
        return value
