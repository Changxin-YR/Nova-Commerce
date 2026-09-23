"""Unit tests for the fulfillment request models and the carrier vocabulary.

These are the boundary tests for section 110's mass-assignment rule, applied to the
one write endpoint this module owns. They need no database, so they run in the fast
feedback loop - which matters, because the body shape is frozen by
``API_CONTRACT`` section 5 and the frontend's ``frozen-contract.ts`` mirrors it
byte for byte. A field that creeps into this model is a wire change.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.modules.fulfillment.enums import CARRIER_CODES, Carrier, is_valid_carrier
from app.modules.fulfillment.schemas import (
    MAX_SHIP_LINES,
    ShipFulfillmentRequest,
    ShipLineIn,
    normalise_carrier,
)

pytestmark = pytest.mark.unit


def _body(**overrides: object) -> dict:
    """A minimal valid ship body, so each test can vary exactly one thing."""
    body: dict = {
        "carrier": "SF",
        "tracking_no": "SF1234567890",
        "item_quantities": [{"order_item_id": 9, "quantity": 1}],
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# The body is exactly three fields
# ---------------------------------------------------------------------------
def test_a_valid_body_is_accepted() -> None:
    parsed = ShipFulfillmentRequest(**_body())
    assert parsed.carrier == "SF"
    assert parsed.tracking_no == "SF1234567890"
    assert parsed.item_quantities == [ShipLineIn(order_item_id=9, quantity=1)]


def test_the_model_has_exactly_three_fields() -> None:
    """The frozen shape, asserted as a set rather than described in prose.

    A fourth field is not a local convenience: ``API_CONTRACT`` section 5 and
    ``frontend/src/types/frozen-contract.ts`` both pin this request to three fields,
    so this test fails at the moment the contract would have to change. It is the
    cheapest possible place to catch a mass-assignment hole - ``extra="forbid"``
    already refuses *unknown* fields, and this refuses a *known* one that should not
    exist.
    """
    assert set(ShipFulfillmentRequest.model_fields) == {
        "carrier",
        "tracking_no",
        "item_quantities",
    }


@pytest.mark.parametrize(
    "forbidden",
    [
        "fulfillment_status",  # the state guard's decision, not the client's
        "shipped_at",  # the server clock
        "order_id",  # which order this is
        "order_no",
        "merchant_id",
        "warehouse_id",
        "fulfillment_no",
        "id",
        "package_count",
        "remark",
        "delivered_at",
        "created_at",
    ],
)
def test_a_client_cannot_set_a_server_owned_field(forbidden: str) -> None:
    """Every server-owned field is refused, not ignored.

    The distinction matters: a field that is silently dropped lets a client believe it
    set ``fulfillment_status`` and go on to render an optimistic UI that disagrees
    with the server. ``extra="forbid"`` makes it a 422 instead.
    """
    with pytest.raises(ValidationError):
        ShipFulfillmentRequest(**_body(**{forbidden: "anything"}))


def test_a_ship_line_carries_only_a_line_and_a_quantity() -> None:
    assert set(ShipLineIn.model_fields) == {"order_item_id", "quantity"}


@pytest.mark.parametrize("forbidden", ["sku_id", "quantity_shipped", "price", "payable_amount"])
def test_a_client_cannot_set_a_server_owned_field_on_a_line(forbidden: str) -> None:
    """The same guard one level down.

    ``sku_id`` is the interesting one: it appears in the *response* shape
    (``API_CONTRACT`` section 5) but must never appear in the request. Letting a client
    name a SKU would let it ship a line the order does not contain.
    """
    with pytest.raises(ValidationError):
        ShipFulfillmentRequest(**_body(item_quantities=[{"order_item_id": 9, "quantity": 1, forbidden: 1}]))


def test_at_least_one_line_is_required() -> None:
    with pytest.raises(ValidationError):
        ShipFulfillmentRequest(**_body(item_quantities=[]))


def test_a_line_count_is_bounded() -> None:
    too_many = [{"order_item_id": index, "quantity": 1} for index in range(1, MAX_SHIP_LINES + 2)]
    with pytest.raises(ValidationError):
        ShipFulfillmentRequest(**_body(item_quantities=too_many))


@pytest.mark.parametrize("quantity", [0, -1])
def test_a_non_positive_quantity_is_refused(quantity: int) -> None:
    """A zero-unit line would be a shipped line that ships nothing."""
    with pytest.raises(ValidationError):
        ShipFulfillmentRequest(**_body(item_quantities=[{"order_item_id": 9, "quantity": quantity}]))


def test_a_non_positive_order_item_id_is_refused() -> None:
    with pytest.raises(ValidationError):
        ShipFulfillmentRequest(**_body(item_quantities=[{"order_item_id": 0, "quantity": 1}]))


def test_the_same_line_twice_is_refused() -> None:
    """Ambiguous input is refused rather than summed.

    ``2`` then ``3`` for one line cannot be distinguished from a double-clicked row,
    and summing it would make acceptance depend on list order rather than on the
    request as a whole.
    """
    with pytest.raises(ValidationError):
        ShipFulfillmentRequest(
            **_body(
                item_quantities=[
                    {"order_item_id": 9, "quantity": 2},
                    {"order_item_id": 9, "quantity": 3},
                ]
            )
        )


def test_distinct_lines_are_accepted() -> None:
    parsed = ShipFulfillmentRequest(
        **_body(
            item_quantities=[
                {"order_item_id": 9, "quantity": 2},
                {"order_item_id": 10, "quantity": 1},
            ]
        )
    )
    assert len(parsed.item_quantities) == 2


# ---------------------------------------------------------------------------
# Carrier: a code from a closed allowlist
# ---------------------------------------------------------------------------
def test_a_known_carrier_is_accepted() -> None:
    assert normalise_carrier("SF") == "SF"


@pytest.mark.parametrize("written", ["sf", " SF ", "\tsf\n", "Yto"])
def test_a_human_typed_carrier_is_normalised_not_rejected(written: str) -> None:
    """Case and surrounding whitespace are normalised, not refused.

    The allowlist exists to refuse carriers we do not know, not to refuse the same
    carrier typed by a human. Without this, a console select that sent ``sf`` would
    be a validation error for a perfectly recognised carrier.
    """
    assert normalise_carrier(written) == written.strip().upper()


@pytest.mark.parametrize("unknown", ["DHL", "SF Express", "顺丰", "", "  ", "SF;DROP TABLE"])
def test_an_unknown_carrier_is_refused(unknown: str) -> None:
    """A free-text carrier would be stored as truth - three spellings, one carrier."""
    with pytest.raises(ValueError):
        normalise_carrier(unknown)


def test_an_unknown_carrier_in_a_body_is_a_validation_error() -> None:
    with pytest.raises(ValidationError):
        ShipFulfillmentRequest(**_body(carrier="SF Express"))


def test_the_stored_carrier_is_the_code_not_what_was_typed() -> None:
    """The model normalises, so the service never sees the raw string."""
    assert ShipFulfillmentRequest(**_body(carrier="sf ")).carrier == "SF"


def test_the_carrier_vocabulary_is_stable() -> None:
    """A guard against silently widening the allowlist.

    Adding a carrier is a one-line enum change **plus** a migration for the
    ``CHECK (carrier IN (...))`` on ``fulfillments`` (Alembic does not autogenerate
    CHECK changes on MySQL). This test makes that pairing visible: it fails on the
    enum edit, next to the note about the migration, instead of failing later as a
    rejected insert in production.
    """
    assert set(CARRIER_CODES) == {"SF", "YTO", "JD", "ZTO", "STO", "YD", "EMS", "OTHER"}
    assert len(CARRIER_CODES) == len(set(CARRIER_CODES)), "a duplicate code is not two carriers"
    assert tuple(member.value for member in Carrier) == CARRIER_CODES


def test_an_unshipped_package_may_have_no_carrier() -> None:
    """``None`` is valid: a package in UNFULFILLED has no carrier yet.

    Rejecting ``None`` would force the shell to invent a placeholder carrier - a
    stored lie in the column the customer's tracking page renders.
    """
    assert is_valid_carrier(None) is True


def test_max_tracking_length_is_the_column_width() -> None:
    """64 is ``fulfillments.tracking_no``'s width; a longer value is refused here.

    Refusing at the edge means the failure names the field. Letting it through would
    reach MySQL and come back as a truncation error - or, with a laxer SQL mode,
    silently store a truncated tracking number that resolves to the wrong parcel.
    """
    assert ShipFulfillmentRequest(**_body(tracking_no="T" * 64)).tracking_no == "T" * 64
    with pytest.raises(ValidationError):
        ShipFulfillmentRequest(**_body(tracking_no="T" * 65))


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_tracking_number_is_refused(blank: str) -> None:
    """``min_length=1`` alone would accept ``"   "`` - a blank tracking number.

    Stripping before the emptiness check is what makes the constraint mean what it
    says, and a shipped parcel with an unusable tracking number is exactly the state
    the required field exists to prevent.
    """
    with pytest.raises(ValidationError):
        ShipFulfillmentRequest(**_body(tracking_no=blank))


def test_a_padded_tracking_number_is_stored_trimmed() -> None:
    assert ShipFulfillmentRequest(**_body(tracking_no="  SF123  ")).tracking_no == "SF123"
