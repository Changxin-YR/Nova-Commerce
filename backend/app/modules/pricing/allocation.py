"""Pro-rata allocation with an exact sum (spec §41, INV-006).

An order-level discount has to live *somewhere* per item, because
``order_items`` carries ``allocated_discount_amount`` and ``payable_amount``
and the database enforces ``payable = original - allocated`` per row while
INV-006 enforces ``SUM(item.payable_amount) = order.payable_amount`` across
rows. The only way both hold is if the per-item shares add up to the order
discount **exactly** - not "to the cent".

Naive pro-rata does not: ``100 * 1 // 3 == 33`` three times is 99, and the
missing unit has to be given to somebody deliberately rather than lost.

The rule, frozen by PHASE4_DESIGN §6 and API_CONTRACT §14.4:

* each line's share is ``floor(total * weight / sum(weights))``;
* the leftover units are handed out **one minor unit at a time, starting at the
  last line and walking backwards**, which for every ordinary cart is exactly
  "the remainder is absorbed by the last eligible line";
* when the total does not exceed the sum of the weights, no line receives more
  than its own weight.

That last clause is the one place this implementation is stricter than a
literal reading of "remainder to the last element, even when it is
zero-weight", and the strictness is not a preference - it is forced by two
other frozen requirements. The remainder can be larger than the last line's own
amount: with 100 eligible lines of 1 minor unit each, ``sum(weights) == 100``,
and a 50-unit discount floors to zero everywhere, so the literal rule would
hand all 50 units to the last line - a line whose whole original amount is 1.
That drives ``item.payable_amount`` to ``-49``, which PHASE4_DESIGN §11
requires to be non-negative ("pricing: ... ``payable >= 0``") and which the
``ck_order_items_amounts_non_negative`` CHECK constraint rejects at the
database. The same happens with a zero-weight trailing line: weights
``(1, 1, 0)`` and ``total == 1`` would put the unit on the line that has
nothing to discount.

Walking backwards from the end preserves the frozen rule whenever the last line
*can* absorb the remainder (which is every case where the shares are in
proportion to the weights), and distributes it over the final lines when it
cannot. The result is still the single, deterministic answer for a given input
- no randomness, no dependence on iteration order, no floating point.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.modules.pricing.errors import PricingInvariantError

__all__ = ["allocate_pro_rata"]


def allocate_pro_rata(total: int, weights: Sequence[int]) -> tuple[int, ...]:
    """Split ``total`` across ``weights`` in proportion, exactly.

    Guarantees, for every accepted input:

    * ``sum(result) == total``
    * ``result[i] >= 0``
    * ``total == 0`` -> all zeros
    * ``len(result) == len(weights)``
    * **when** ``total <= sum(weights)``, also ``result[i] <= weights[i]``

    The last clause is conditional because ``weights`` are *proportions*, not
    caps: PHASE4_DESIGN §6 pins the example ``weights (1, 1, 1)`` with
    ``total == 100``, where a per-line "share" of 33 is three times the weight
    it came from and that is the correct answer. Every caller in
    :mod:`app.modules.pricing.service` caps its discount at the eligible amount
    first, so the priced path always operates in the regime where the cap holds -
    and that cap is what keeps per-item ``payable_amount`` non-negative.

    A zero weight is a line that must not receive anything (an out-of-scope or
    already fully-discounted line): it is skipped when the remainder is handed
    out, and never receives a share larger than zero.

    Args:
        total: minor units to distribute; must be non-negative.
        weights: the per-line basis of the split (usually the line's original
            amount, or what is left of it). Non-negative; a zero means "not
            eligible for any of this discount".

    Returns:
        A tuple of the same length as ``weights``, summing exactly to ``total``.

    Raises:
        PricingInvariantError: for a negative ``total``, a negative weight, a
            non-integer input, or an empty/all-zero ``weights`` with a positive
            ``total``. Each of these means a caller computed a discount with no
            basis to allocate it against; allocating it anyway would have to
            invent a line to charge, which is precisely the silent bug INV-006
            exists to prevent.
    """
    if not isinstance(total, int) or isinstance(total, bool):
        msg = f"total must be integer minor units, got {type(total).__name__}"
        raise PricingInvariantError(msg)
    if total < 0:
        raise PricingInvariantError("total must not be negative", context={"total": total})

    counts = list(weights)
    for weight in counts:
        if not isinstance(weight, int) or isinstance(weight, bool):
            msg = f"weights must be integer minor units, got {type(weight).__name__}"
            raise PricingInvariantError(msg)
        if weight < 0:
            raise PricingInvariantError("weights must not be negative", context={"weights": tuple(counts)})

    if total == 0:
        return tuple(0 for _ in counts)

    if not counts:
        raise PricingInvariantError(
            "cannot allocate a positive total with no weights", context={"total": total}
        )

    weight_sum = sum(counts)
    if weight_sum == 0:
        raise PricingInvariantError(
            "cannot allocate a positive total when every weight is zero",
            context={"total": total, "weights": tuple(counts)},
        )

    shares = [total * weight // weight_sum for weight in counts]
    remainder = total - sum(shares)

    # Hand the leftover units out from the end. `remainder` is provably
    # `sum(frac_i) < number_of_positive_weights`, so a single backward pass over
    # the positive weights always distributes it all; the loop below can only
    # exit with `remainder == 0`.
    for index in range(len(counts) - 1, -1, -1):
        if remainder == 0:
            break
        if counts[index] == 0:
            continue
        shares[index] += 1
        remainder -= 1

    if remainder != 0:  # pragma: no cover - unreachable by the proof above
        raise PricingInvariantError(
            "remainder could not be distributed",
            context={"total": total, "undistributed": remainder, "weights": tuple(counts)},
        )

    return tuple(shares)
