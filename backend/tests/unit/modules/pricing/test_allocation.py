"""Allocation must be exact, not exact-to-the-cent (spec §41, INV-006).

The allocation function is where a rounding decision becomes a ledger fact, so
the cases below are chosen to *break* naive pro-rata rather than to illustrate
it: weights that do not divide the total, weights with zeros, and a total that
is shared by many small lines each of which floors to zero.

The last family is the interesting one. "The remainder goes to the last eligible
line" is the frozen rule and is what every ordinary cart exercises; it stops
being sufficient when the remainder is larger than the last line's own amount,
because then the frozen rule would allocate more discount to a line than that
line has money - a negative ``item.payable_amount``, which PHASE4_DESIGN §11
requires to be non-negative and ``ck_order_items_amounts_non_negative`` rejects
at the database. These tests pin both halves: the frozen rule for the ordinary
cases, and the cap for the cases where the frozen rule cannot be executed
literally.
"""

from __future__ import annotations

from itertools import product

import pytest

from app.modules.pricing import PricingInvariantError, allocate_pro_rata

# (weights, total, expected) - every expectation hand-computed from the rule
# "floor each share, then hand the leftover units out from the end".
ALLOCATION_CASES: list[tuple[tuple[int, ...], int, tuple[int, ...]]] = [
    # -- the frozen example: three equal lines sharing 100 (§6) -------------
    ((1, 1, 1), 100, (33, 33, 34)),
    # -- single line: it takes everything, remainder and all ----------------
    ((1,), 1, (1,)),
    ((5,), 5, (5,)),
    ((7,), 0, (0,)),
    # -- divides evenly: no remainder to place ------------------------------
    ((2, 2, 3), 7, (2, 2, 3)),
    ((3, 4, 5), 12, (3, 4, 5)),
    # -- the "(2,2,3)-style" awkward case: remainder fits on the last line --
    ((2, 2, 3), 5, (1, 1, 3)),
    # -- "remainder to the last eligible line", zeros skipped ---------------
    ((1, 1, 1), 1, (0, 0, 1)),
    ((1, 2), 1, (0, 1)),
    ((2, 1), 1, (0, 1)),
    ((3, 0, 3), 1, (0, 0, 1)),
    ((1, 0, 0), 1, (1, 0, 0)),
    # -- remainder larger than the last line: walk backwards ----------------
    ((2, 2, 3), 6, (1, 2, 3)),
    ((0, 0, 1), 1, (0, 0, 1)),
    # -- mixed magnitudes ---------------------------------------------------
    ((7, 11, 13), 17, (3, 6, 8)),
    ((999, 1), 500, (499, 1)),
    ((1, 999), 2, (0, 2)),
]


@pytest.mark.parametrize(("weights", "total", "expected"), ALLOCATION_CASES)
def test_allocations_match_the_frozen_rule(
    weights: tuple[int, ...], total: int, expected: tuple[int, ...]
) -> None:
    assert allocate_pro_rata(total, weights) == expected


@pytest.mark.parametrize(("weights", "total", "expected"), ALLOCATION_CASES)
def test_every_case_sums_exactly(weights: tuple[int, ...], total: int, expected: tuple[int, ...]) -> None:
    """The only guarantee INV-006 actually rests on."""
    result = allocate_pro_rata(total, weights)
    assert sum(result) == total
    assert expected == result


def test_zero_total_is_all_zeros() -> None:
    assert allocate_pro_rata(0, (3, 5, 7)) == (0, 0, 0)
    assert allocate_pro_rata(0, (0, 0)) == (0, 0)
    assert allocate_pro_rata(0, ()) == ()


def test_many_small_lines_do_not_overdraw_the_last_one() -> None:
    """100 lines of 1 minor unit, a 50-unit discount.

    Every line floors to zero and the remainder is 50 - larger than the last
    line's entire amount. The frozen "remainder to the last line" reading would
    put all 50 on it, making that item's payable ``1 - 50``. The units are
    therefore spread over the last 50 lines instead: exact sum, no overdraw.
    """
    weights = tuple([1] * 100)
    result = allocate_pro_rata(50, weights)

    assert sum(result) == 50
    assert set(result) == {0, 1}
    assert result[50:] == tuple([1] * 50)
    assert all(share <= weight for share, weight in zip(result, weights, strict=True))


def test_remainder_goes_to_the_last_eligible_line_not_the_last_line() -> None:
    """A trailing out-of-scope line must not receive the remainder.

    Weight 0 means "this line is outside the rule's scope" or "already fully
    discounted": either way it has no money left to discount, so handing it the
    remainder would be the same overdraw as above, wearing a scope.
    """
    assert allocate_pro_rata(1, (1, 1, 0)) == (0, 1, 0)
    assert allocate_pro_rata(1, (1, 0, 1, 0)) == (0, 0, 1, 0)


def test_allocation_never_exceeds_its_weight_exhaustively() -> None:
    """Property sweep: exact sum, per-line cap, for every awkward small input.

    Small and exhaustive beats large and sampled here: the failure mode is an
    off-by-one in the remainder loop, which a random sample of large numbers
    reaches about as often as a coin lands on its edge.
    """
    checked = 0
    for length in (1, 2, 3, 4):
        for weights in product(range(4), repeat=length):
            weight_sum = sum(weights)
            for total in range(weight_sum + 1):
                result = allocate_pro_rata(total, weights)
                assert sum(result) == total, (weights, total, result)
                assert len(result) == length
                assert all(share >= 0 for share in result), (weights, total, result)
                assert all(share <= weight for share, weight in zip(result, weights, strict=True)), (
                    weights,
                    total,
                    result,
                )
                checked += 1
    assert checked > 300


def test_allocation_is_deterministic() -> None:
    """Same input, same answer - no set iteration, no randomness, no dict order."""
    weights = (13, 7, 29, 3)
    first = allocate_pro_rata(50, weights)
    second = allocate_pro_rata(50, list(weights))
    third = allocate_pro_rata(50, tuple(reversed(list(reversed(weights)))))
    assert first == second == third


def test_full_weight_total_is_identity() -> None:
    """Discounting exactly the whole amount leaves every line at zero payable."""
    weights = (3, 4, 5)
    assert allocate_pro_rata(sum(weights), weights) == weights


# ---------------------------------------------------------------------------
# Refusals: every one of these means a caller allocated against the wrong basis
# ---------------------------------------------------------------------------
def test_total_above_the_weight_sum_is_a_proportional_split() -> None:
    """``(1, 1, 1)``/100 is the frozen example: weights are proportions.

    Each line's share (33) is far larger than the weight it came from (1), and
    that is the correct answer - §6 pins this exact case. The per-line cap is
    therefore a guarantee *when the total does not exceed the weights' sum*,
    which is the regime the pricing service always calls from because it caps
    each discount at the eligible amount first.
    """
    assert allocate_pro_rata(100, (1, 1, 1)) == (33, 33, 34)
    assert allocate_pro_rata(100, (1, 2)) == (33, 67)
    assert allocate_pro_rata(10, (1, 1)) == (5, 5)


def test_negative_total_is_refused() -> None:
    with pytest.raises(PricingInvariantError, match="negative"):
        allocate_pro_rata(-1, (5,))


def test_positive_total_with_no_weights_is_refused() -> None:
    with pytest.raises(PricingInvariantError, match="no weights"):
        allocate_pro_rata(1, ())


def test_positive_total_with_all_zero_weights_is_refused() -> None:
    """Refused rather than dumped on a zero line: that is the overdraw again."""
    with pytest.raises(PricingInvariantError, match="every weight is zero"):
        allocate_pro_rata(1, (0, 0))


def test_negative_weight_is_refused() -> None:
    with pytest.raises(PricingInvariantError, match="negative"):
        allocate_pro_rata(1, (2, -1))


@pytest.mark.parametrize("bad_total", [1.0, "1", None, True])
def test_non_integer_total_is_refused(bad_total: object) -> None:
    with pytest.raises(PricingInvariantError):
        allocate_pro_rata(bad_total, (5,))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad_weight", [1.0, "1", None, True])
def test_non_integer_weight_is_refused(bad_weight: object) -> None:
    with pytest.raises(PricingInvariantError):
        allocate_pro_rata(1, (bad_weight,))  # type: ignore[arg-type]
