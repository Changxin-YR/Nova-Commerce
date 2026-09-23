"""Unit tests for the two pure rules of the ship path.

    compute_fulfillment_status - the order's axis, from *all* its packages
    compute_residual           - what a partial shipment must leave behind

Both are pure functions with no session, which is the point: the two rules that
decide whether an order is ``PARTIAL_SHIPPED`` or ``SHIPPED`` and whether a split
parcel loses goods are testable exhaustively here, and the integration test only has
to prove that the workflow *calls* them with the right inputs.
"""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.modules.fulfillment.service import compute_fulfillment_status, compute_residual
from app.modules.order.enums import FulfillmentStatus

pytestmark = pytest.mark.unit

UNFULFILLED = FulfillmentStatus.UNFULFILLED.value
PARTIAL = FulfillmentStatus.PARTIAL_SHIPPED.value
SHIPPED = FulfillmentStatus.SHIPPED.value
DELIVERED = FulfillmentStatus.DELIVERED.value


# ---------------------------------------------------------------------------
# compute_fulfillment_status
# ---------------------------------------------------------------------------
def test_nothing_shipped_is_unfulfilled() -> None:
    assert compute_fulfillment_status(ordered={1: 3}, shipped={}) == UNFULFILLED


def test_some_shipped_is_partial() -> None:
    assert compute_fulfillment_status(ordered={1: 3}, shipped={1: 1}) == PARTIAL


def test_everything_shipped_is_shipped() -> None:
    assert compute_fulfillment_status(ordered={1: 3}, shipped={1: 3}) == SHIPPED


def test_the_axis_is_derived_from_every_line_not_the_shipped_one() -> None:
    """The defect a per-package check produces: one line fully out, one not.

    A rule that looked only at the line it just shipped would call this ``SHIPPED``
    and the customer would be told the whole order is on its way.
    """
    assert compute_fulfillment_status(ordered={1: 1, 2: 1}, shipped={1: 1}) == PARTIAL


def test_a_fully_shipped_order_with_one_line_at_zero_is_still_partial() -> None:
    assert compute_fulfillment_status(ordered={1: 2, 2: 2}, shipped={1: 2, 2: 0}) == PARTIAL


def test_an_untouched_second_line_keeps_the_order_unfulfilled() -> None:
    """Nothing has shipped *from this line*, and nothing has shipped at all."""
    assert compute_fulfillment_status(ordered={1: 2, 2: 2}, shipped={1: 0}) == UNFULFILLED


def test_over_shipping_is_reported_as_shipped_not_partial() -> None:
    """A cumulative total above the ordered quantity satisfies the line.

    It is a defect that the ship path refuses (``FULFILLMENT_QUANTITY_EXCEEDS_ORDER``,
    70 001), so reaching here means the guard was bypassed; reporting ``SHIPPED``
    keeps the axis monotonic rather than inventing a fifth state, and the anomaly is
    visible in the data as ``shipped > ordered``.
    """
    assert compute_fulfillment_status(ordered={1: 2}, shipped={1: 3}) == SHIPPED


def test_delivered_is_terminal() -> None:
    """A delivered order must not be dragged back to ``SHIPPED``.

    A reship or a later package would otherwise tell the customer their delivered
    order is out for delivery again.
    """
    assert compute_fulfillment_status(ordered={1: 2}, shipped={1: 2}, current=DELIVERED) == DELIVERED
    assert compute_fulfillment_status(ordered={1: 2}, shipped={}, current=DELIVERED) == DELIVERED


def test_shipped_is_not_sticky() -> None:
    """The one state that *is* allowed to move backwards, and why.

    An order that shipped and then gained a partially-shipped line reads
    ``PARTIAL_SHIPPED`` again, because some of it genuinely has not arrived. Unlike
    ``DELIVERED``, this is not a regression of a fact - it is the arrival of a new
    one.
    """
    assert compute_fulfillment_status(ordered={1: 2, 2: 2}, shipped={1: 2}, current=SHIPPED) == PARTIAL


def test_an_order_with_no_lines_is_unfulfilled() -> None:
    """The empty-set trap: ``all()`` over nothing is true, so this must be explicit.

    An order with no lines has shipped nothing. Returning ``SHIPPED`` here would tell
    a customer that an empty order is on its way.
    """
    assert compute_fulfillment_status(ordered={}, shipped={}) == UNFULFILLED


def test_a_package_line_for_an_unknown_order_line_is_ignored() -> None:
    """A shipment for a line the order does not have is a data defect.

    It must not invent a quantity the order never contained, so it is ignored rather
    than counted - and it cannot make the order look complete.
    """
    assert compute_fulfillment_status(ordered={1: 1}, shipped={1: 1, 99: 5}) == SHIPPED


# ---------------------------------------------------------------------------
# compute_residual
# ---------------------------------------------------------------------------
def test_a_full_shipment_leaves_no_residual() -> None:
    assert compute_residual(package_lines={1: 3}, requested={1: 3}) == {}


def test_a_partial_shipment_leaves_the_remainder() -> None:
    """Design 6.3 step 5: the remainder must not be dropped."""
    assert compute_residual(package_lines={1: 3}, requested={1: 2}) == {1: 1}


def test_a_line_held_back_entirely_becomes_the_residual() -> None:
    assert compute_residual(package_lines={1: 1, 2: 1}, requested={1: 1}) == {2: 1}


def test_the_residual_is_per_line() -> None:
    """Line 1 ships in full while line 2 is held back *and* line 3 partly."""
    assert compute_residual(package_lines={1: 1, 2: 2, 3: 5}, requested={1: 1, 3: 3}) == {
        2: 2,
        3: 2,
    }


def test_zero_quantity_entries_are_dropped() -> None:
    """A package line of zero units would violate ``CHECK (quantity > 0)``.

    Returning ``{1: 0}`` here would turn a correct shipment into a database error at
    flush time, which is the kind of late failure the pure function exists to prevent.
    """
    residual = compute_residual(package_lines={1: 1, 2: 4}, requested={1: 1, 2: 4})
    assert residual == {}
    assert 0 not in residual.values()


def test_requesting_a_line_the_package_does_not_carry_is_refused() -> None:
    with pytest.raises(ValidationError):
        compute_residual(package_lines={1: 3}, requested={2: 1})


def test_requesting_more_than_the_package_holds_is_refused() -> None:
    with pytest.raises(ValidationError):
        compute_residual(package_lines={1: 3}, requested={1: 4})


def test_an_empty_request_is_a_complete_residual() -> None:
    """Degenerate but well-defined: the ship path refuses an empty request before
    reaching here (``item_quantities`` has ``min_length=1``), so this documents the
    function's contract rather than a reachable path."""
    assert compute_residual(package_lines={1: 3, 2: 2}, requested={}) == {1: 3, 2: 2}
