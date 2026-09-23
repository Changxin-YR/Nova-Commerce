"""Unit tests for the refund split - the arithmetic FG-12 cap 2 is measured against.

No database, no session, no HTTP: these tests exist to pin the **arithmetic** of
``aftersales.refunds``, because the failure mode this module guards against is an
off-by-one-minor-unit that only appears on amounts that do not divide evenly. A suite
that only tested round numbers would be green while ``sum(shares) != amount``.
"""

from __future__ import annotations

import pytest

from app.core.errors import RefundExceedsItemError, ValidationError
from app.modules.aftersales.enums import AfterSaleType
from app.modules.aftersales.refunds import (
    allocate_refund_across_lines,
    check_per_line_cap,
    movement_quantity_for,
    return_in_movement_key,
)

# ---------------------------------------------------------------------------
# allocate_refund_across_lines
# ---------------------------------------------------------------------------


def test_split_sums_exactly_on_a_repeating_amount() -> None:
    """100 across three equal lines places every minor unit.

    ``floor(100/3) == 33`` three times is 99, so this fails for any implementation that
    forgets the remainder - which is the entire reason the split delegates to
    ``pricing.allocation``.
    """
    split = allocate_refund_across_lines(amount=100, line_payables=[(1, 100), (2, 100), (3, 100)])
    assert sum(share for _, share in split.shares) == 100
    assert split.total == 100


def test_split_puts_the_remainder_on_the_last_line() -> None:
    """The frozen rule: remainder absorbed by the last eligible line.

    The exact shape matters, not just the sum: the console shows per-line refunds, and
    "which line absorbed the odd cent" must be stable or two operators reading the same
    refund see different numbers.
    """
    split = allocate_refund_across_lines(amount=100, line_payables=[(1, 100), (2, 100), (3, 99)])
    assert split.as_dict() == {1: 33, 2: 33, 3: 34}


def test_split_of_one_minor_unit_lands_on_the_last_line() -> None:
    split = allocate_refund_across_lines(amount=1, line_payables=[(1, 1), (2, 1), (3, 1)])
    assert split.as_dict() == {1: 0, 2: 0, 3: 1}


@pytest.mark.parametrize(
    ("amount", "payables"),
    [
        (1, [(1, 7)]),
        (7, [(1, 7)]),
        (13, [(1, 100), (2, 3)]),
        (999999, [(1, 333333), (2, 333333), (3, 333334)]),
        (3, [(1, 10), (2, 0), (3, 10)]),
    ],
)
def test_split_always_partitions_the_amount(amount: int, payables: list[tuple[int, int]]) -> None:
    """Property, over the shapes that actually break naive allocators.

    The zero-weight case is included on purpose: a line with nothing left to refund has
    a payable of zero, and an allocator that hands the remainder to it would place money
    on a line that cannot carry any.
    """
    split = allocate_refund_across_lines(amount=amount, line_payables=payables)
    assert split.total == amount
    assert all(share >= 0 for _, share in split.shares)
    assert len(split.shares) == len(payables)


def test_split_never_places_money_on_a_zero_capacity_line() -> None:
    """A line weighted zero can carry nothing, so the money is placed on the lines that can.

    Weight is a **proportion** (that is ``allocate_pro_rata``'s documented contract, pinned
    by ``PHASE4_DESIGN`` section 6's ``weights (1, 1, 1), total 100`` example), and a zero
    weight in this caller is always a line with no remaining capacity. The allocation
    therefore concentrates on line 2, and the per-line cap is satisfied by construction -
    which is the property that matters.
    """
    split = allocate_refund_across_lines(amount=5, line_payables=[(1, 0), (2, 5)])
    assert split.as_dict() == {1: 0, 2: 5}
    with pytest.raises(ValidationError):
        # 5 > the only capacity there is: no legal placement exists.
        allocate_refund_across_lines(amount=5, line_payables=[(1, 0), (2, 4)])


def test_split_refuses_more_than_the_total_capacity() -> None:
    """``sum(shares) == amount`` and ``share <= capacity`` cannot both hold above the total.

    The precondition is explicit rather than a silent over-allocation: a refund larger than
    the lines can carry has no legal placement, and returning one would be a lie that the
    per-line cap only catches later (as a raw CHECK violation at the database, if the
    workflow's own guard were ever bypassed).
    """
    with pytest.raises(ValidationError):
        allocate_refund_across_lines(amount=2, line_payables=[(1, 1)])


def test_split_stays_within_every_line_capacity_on_a_part_refunded_order() -> None:
    """The regression the integration suite caught, pinned at the unit level.

    Remaining capacities ``(1111, 1111, 1112)`` and a 1-unit refund: every proportional share
    floors to 0, and the frozen rule hands the leftover to the **last** line - which here has
    capacity to spare. The dangerous arrangement is the mirror of this one, where the last
    line is the exhausted one; this asserts the allocator never exceeds any weight, so the
    per-line cap cannot be tripped by a legal refund.
    """
    capacities = [(1, 1111), (2, 1111), (3, 1112)]
    for amount in (1, 2, 1111, 1112, 3333, 3334):
        split = allocate_refund_across_lines(amount=amount, line_payables=capacities)
        assert split.total == amount
        for line_id, share in split.shares:
            capacity = dict(capacities)[line_id]
            assert 0 <= share <= capacity, f"line {line_id} got {share} of {capacity}"


def test_split_shares_are_sorted_by_line_id() -> None:
    """The split is sorted, and that is the lock-acquisition order too.

    Two concurrent refunds must take line locks in one agreed sequence or they deadlock
    instead of serialising, so the sort is load-bearing rather than cosmetic.
    """
    split = allocate_refund_across_lines(amount=10, line_payables=[(9, 100), (3, 100), (7, 100)])
    assert split.lines() == (3, 7, 9)


def test_split_refuses_a_non_positive_amount() -> None:
    with pytest.raises(ValidationError):
        allocate_refund_across_lines(amount=0, line_payables=[(1, 100)])
    with pytest.raises(ValidationError):
        allocate_refund_across_lines(amount=-5, line_payables=[(1, 100)])


def test_split_refuses_when_there_is_no_line_to_place_money_on() -> None:
    with pytest.raises(ValidationError):
        allocate_refund_across_lines(amount=10, line_payables=[])
    with pytest.raises(ValidationError):
        allocate_refund_across_lines(amount=10, line_payables=[(1, 0), (2, 0)])


# ---------------------------------------------------------------------------
# check_per_line_cap - FG-12 cap 2
# ---------------------------------------------------------------------------


def test_per_line_cap_allows_a_share_equal_to_the_remaining_payable() -> None:
    """``prior + share == payable`` is legal - the cap is inclusive.

    An exclusive comparison (``>=``) would refuse the exact full refund, which is the
    most common legal case there is.
    """
    split = allocate_refund_across_lines(amount=40, line_payables=[(1, 100)])
    check_per_line_cap(split=split, line_payables={1: 100}, already_refunded={1: 60})


def test_per_line_cap_refuses_one_minor_unit_over() -> None:
    split = allocate_refund_across_lines(amount=41, line_payables=[(1, 100)])
    with pytest.raises(RefundExceedsItemError) as excinfo:
        check_per_line_cap(split=split, line_payables={1: 100}, already_refunded={1: 60})
    assert "order_item_id" in excinfo.value.context
    assert excinfo.value.context["over_by"] == 1


def test_per_line_cap_is_cumulative_across_refunds() -> None:
    """The cap is a sum over refunds, which is exactly why no CHECK can express it.

    Two 60-unit refunds on a 100-unit line: the second is refused even though its own
    amount is well inside the line. A per-refund check (rather than a cumulative one)
    would pass both and over-refund the line by 20.
    """
    first = allocate_refund_across_lines(amount=60, line_payables=[(1, 100)])
    check_per_line_cap(split=first, line_payables={1: 100}, already_refunded={1: 0})
    second = allocate_refund_across_lines(amount=60, line_payables=[(1, 100)])
    with pytest.raises(RefundExceedsItemError):
        check_per_line_cap(split=second, line_payables={1: 100}, already_refunded={1: 60})


# ---------------------------------------------------------------------------
# movement_quantity_for - RETURN_IN only, and only for returned lines
# ---------------------------------------------------------------------------


def test_return_refund_moves_the_claimed_units_back() -> None:
    split = allocate_refund_across_lines(amount=100, line_payables=[(1, 100)])
    moved = movement_quantity_for(
        claim_type=AfterSaleType.RETURN_REFUND,
        claim_items=[(1, 2)],
        split=split,
    )
    assert moved == ((1, 2),)


def test_refund_only_moves_no_stock() -> None:
    """The customer kept the goods; crediting them would invent inventory.

    This is the defect the rule exists for: a REFUND_ONLY claim that credited stock
    would inflate availability until the next stock count found the difference, which
    looks like shrinkage and is actually a bookkeeping error in the opposite direction.
    """
    split = allocate_refund_across_lines(amount=100, line_payables=[(1, 100)])
    assert (
        movement_quantity_for(claim_type=AfterSaleType.REFUND_ONLY, claim_items=[(1, 2)], split=split)
        == ()
    )


def test_a_line_that_received_no_money_moves_no_stock() -> None:
    """A zero share means this refund did not buy that line back.

    A zero-quantity movement would put a row in the ledger claiming something happened.
    """
    split = allocate_refund_across_lines(amount=1, line_payables=[(1, 1), (2, 1), (3, 1)])
    moved = movement_quantity_for(
        claim_type=AfterSaleType.RETURN_REFUND,
        claim_items=[(1, 2), (2, 2), (3, 2)],
        split=split,
    )
    assert moved == ((3, 2),)


def test_return_in_key_is_deterministic_and_per_line() -> None:
    """Deterministic, because a retried refund must not credit stock twice.

    A random key would make the retry a second movement; ``inventory_movements``
    idempotency is what stops that, so the key must be a pure function of the claim and
    the line.
    """
    assert return_in_movement_key(after_sale_no="NVAS20260101000001", order_item_id=7) == (
        "return-in:NVAS20260101000001:7"
    )
    assert return_in_movement_key(
        after_sale_no="NVAS20260101000001", order_item_id=8
    ) != return_in_movement_key(after_sale_no="NVAS20260101000001", order_item_id=7)
