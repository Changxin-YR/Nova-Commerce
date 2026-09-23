"""Where a refund lands inside a claim, and why it must reuse the Phase 4 allocator.

    allocate_refund_across_lines, check_per_line_cap, RefundSplit

## The problem, stated exactly

A ``refunds`` row carries one ``amount`` for the whole claim (PHASE5_DESIGN 搂5.5:
the row has no per-line split in V1). ``order_items.refunded_amount``, however, is
per **line**, and ``order_items`` carries ``CHECK (refunded_amount <=
payable_amount)``. So a refund has to be *placed* on lines, and the placement must
satisfy two things at once:

* ``sum(shares) == refund.amount`` - exactly, not to the nearest cent. A split that
  loses a minor unit leaves the sum of the lines permanently unable to explain the
  payment's ``refunded_amount``;
* ``line.refunded_amount_before + share <= line.payable_amount`` - the per-line cap
  (FG-12 cap 2), which is a *sum over refunds per line* and therefore cannot be a
  single-row ``CHECK``. That is why it is enforced here, in the workflow, against
  freshly locked rows.

## Why this delegates to ``pricing.allocation.allocate_pro_rata``

There is exactly one pro-rata-with-remainder rule in this codebase and it lives in
:func:`app.modules.pricing.allocation.allocate_pro_rata`, where INV-006 depends on
it: ``SUM(order_items.payable_amount) == orders.payable_amount``. Writing a second
allocator here would mean two implementations of the same rule that agree on every
even split and quietly disagree on an odd one - and an invariant that only fails on
odd amounts is an invariant that fails in production, on a customer's order, with
no failing test to explain it. The design says this explicitly (搂6.2 step 4), and
this module is the place where that instruction is obeyed rather than paraphrased.

The *weights* are the caller's basis of the split, and ``RefundWorkflow`` passes each
line's **remaining capacity** (``payable_amount - refunded_amount``) rather than its
total payable amount. On the first refund of an order the two are identical, so this is
the same basis the order-level discount was allocated on - but on a *second* refund they
differ, and weighting by the total would size a share for a line as though nothing had
been refunded from it yet, which the per-line cap then (correctly) refuses. See
``workflow._validate_caps`` for the worked example that caught it.

## The rule, stated as the captain's ruling puts it

**A refund's per-line split must partition the refund, and the cap is a separate
comparison against each line's remaining refundable amount.** Those are two different
questions, and collapsing them is what made the design's literal formula wrong:

* the **split** answers "how much of this refund goes on each line" - a partition, so
  ``sum(shares) == amount`` exactly;
* the **cap** answers "may this line carry that share" - a comparison of
  ``line.refunded_amount + share`` against ``line.payable_amount``, evaluated per line
  and cumulatively across refunds, in :func:`check_per_line_cap`.

PHASE5_DESIGN section 6.2 step 4 wrote the share as
``floor(amount * item_order_item_payable / approved_amount)``. Read literally that
divides by the *approved* amount while summing over the *items*, and the two are
different quantities - so the shares are not a partition of the refund whenever
``approved_amount != sum(items.payable_amount)``:

    one line, payable 100000; the customer asks to return it and the operator
    approves 100000; the second refund of 50000 is requested. Literal reading:
    share = floor(50000 * 100000 / 100000) = 50000. Two refunds of 50000 have now
    placed 100000 on a 100000 line, and the per-line cap is *satisfied* - yet
    100000 was refunded against a 100000 payment, so cap 1 was the only thing that
    refused a further one. Change the same claim to approve 200000 on that
    100000 line (the operator approving above the ask, which V1's service refuses
    precisely so this cannot happen) and the same arithmetic places 50000 a
    second time without complaint.

Weights are the lines' payable amounts and the denominator is their sum, so
``sum(shares) == amount`` **always** - the split is a partition - and the cap is then
checked separately, per line. The frozen public behaviour is unchanged when
``approved_amount == sum(items.payable_amount)`` (which is the case the service
enforces at apply time, since it caps ``requested_amount`` at the lines' own payable
total); the divergence only appears in the configurations the literal reading gets
wrong. The captain has recorded this ruling for the handoff.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.core.errors import RefundExceedsItemError, ValidationError
from app.modules.aftersales.enums import AfterSaleType
from app.modules.pricing.allocation import allocate_pro_rata

__all__ = [
    "RefundSplit",
    "allocate_refund_across_lines",
    "check_per_line_cap",
    "movement_quantity_for",
    "return_in_movement_key",
]


@dataclass(frozen=True, slots=True)
class RefundSplit:
    """The per-line placement of one refund.

    A named type rather than a bare dict so the workflow cannot accidentally read
    it as "line -> quantity" (the ``after_sale_items`` shape) when it means
    "line -> minor units". A dict of ints would typecheck either way, and the bug
    would show up as a refund of 3 yuan where 3 units were meant.
    """

    #: ``order_item_id -> minor units placed on that line``. Sorted by
    #: ``order_item_id``, which is also the lock-acquisition order the workflow
    #: uses - one order for every refund, so two concurrent refunds on the same
    #: order acquire line locks in the same sequence and cannot deadlock.
    shares: tuple[tuple[int, int], ...]

    @property
    def total(self) -> int:
        """The refunded amount these shares add up to (exactly, by construction)."""
        return sum(share for _, share in self.shares)

    def as_dict(self) -> dict[int, int]:
        return dict(self.shares)

    def lines(self) -> tuple[int, ...]:
        return tuple(order_item_id for order_item_id, _ in self.shares)


def allocate_refund_across_lines(
    *,
    amount: int,
    line_payables: Sequence[tuple[int, int]],
) -> RefundSplit:
    """Spread ``amount`` across the claim's lines, in proportion to their payables.

    The rule, in the form the captain asked it to be stated:

        **sum of shares == amount, and share_i <= line_i.remaining**

    The first clause is this function's job, and it holds for every accepted input (the second
    was enforced when the earlier "weights are the lines' full payables" reading was replaced -
    the weights this function receives are the lines' *remaining* refundable amounts, so a line
    that has already been partly refunded cannot be handed a share it can no longer absorb).
    The second clause is asserted separately, per line, by :func:`check_per_line_cap`, because
    the cumulative comparison needs the freshly locked rows and does not belong in a pure
    allocation. See the module docstring for the full ruling.

    Args:
        amount: minor units to place. Must be positive - a zero refund is refused
            earlier, in the workflow, because "refund 0" is a request that looks
            like a write and is not one.
        line_payables: ``(order_item_id, payable_amount)`` for every line the claim
            names, in the caller's order. The weights are used as given and the
            resulting shares come back sorted by line id.

    Returns:
        A :class:`RefundSplit` whose shares sum to ``amount`` **exactly**.

    Raises:
        ValidationError: ``amount <= 0``, no lines, or ``amount`` above the *total* of the
            weights. Each of these means a caller computed a refund with no basis to place
            it on, and inventing a line to charge is precisely the silent bug the per-line
            cap exists to prevent. The last one is a **precondition** rather than a policy
            choice: no placement exists that both sums to ``amount`` and respects the
            weights, so returning one would be a lie. ``RefundWorkflow`` keeps it
            unreachable by checking ``amount`` against the lines' total remaining capacity
            first and raising the frozen ``REFUND_EXCEEDS_ITEM_AMOUNT (80005)``.

    The split is deliberately a **pure function of the claim and the amount** - it
    never reads what has already been refunded. Whether a line can carry the share
    is a separate question asked by :func:`check_per_line_cap` against freshly
    locked rows (see the module docstring on why no cap decision may happen
    anywhere else).
    """
    if amount <= 0:
        raise ValidationError(
            "a refund split needs a positive amount", context={"amount": amount}
        )
    if not line_payables:
        raise ValidationError("a refund must name at least one line to be placed on")

    weights = [int(payable) for _, payable in line_payables]
    if sum(weights) < amount:
        raise ValidationError(
            "a refund cannot be placed above the lines' total capacity",
            context={
                "amount": amount,
                "capacity_total": sum(weights),
                "line_ids": [line_id for line_id, _ in line_payables],
            },
        )

    # The single pro-rata-with-remainder implementation (PHASE5_DESIGN 搂6.2 step 4).
    # It guarantees `sum(shares) == amount` and `share >= 0` for every weight, and
    # it raises rather than guessing when the weights cannot carry the total - the
    # exception type is the pricing module's, which the caller below translates.
    shares = allocate_pro_rata(amount, weights)

    # `allocate_pro_rata` walks the leftover minor units backwards **from the last line**.
    # On the first refund of an order that is exactly the frozen rule, because the weights
    # are the lines' full payables and no line is anywhere near its cap. It stops being safe
    # on a *part*-refunded order: with remaining capacities (1111, 1111, 1112) and a 1-unit
    # refund, every floor share is 0 and the whole unit lands on the last line - which may
    # have no capacity left, so the caller's per-line cap would then refuse a refund that is
    # perfectly legal. (That is not hypothetical: it is the defect the integration suite
    # caught, and it would have refused a legitimate full refund.)
    #
    # So the remainder's *placement* is checked against the weights, which for this caller
    # are the lines' remaining capacities. Where the backward pass would place a unit a line
    # cannot hold, the leftover is redistributed from the **first** line forward instead:
    # the rule's intent (the remainder goes to a line that can absorb it, deterministically)
    # is preserved and illegal layouts become expressible. Sum-exactness is untouched - the
    # same units are handed out, only the order changes.
    # ...except that the frozen backward pass is not safe here in general. Its remainder is
    # handed out one unit at a time from the last line backwards, *without* consulting the
    # weights, so a line can end up carrying more than its own remaining capacity: with
    # weights (1111, 1111, 1112) and a 6666 refund, the floor shares are 2222 each and the
    # backward table would place the 4 leftover units on the last two lines, driving line 3
    # to 3334 - its exact cap - while lines 1 and 2 stayed at 2220. Every unit lands, but the
    # placement is no longer proportional, and a subsequent refund then finds capacity on
    # lines that should have had none.
    #
    # So the remainder is re-derived by `_distribute_within_capacity`, which applies the same
    # pro-rata floors and then hands the leftover units out from the last line backwards
    # *skipping any line already at its weight* - the frozen rule, with the one correction
    # that keeps it legal. For every input where `allocate_pro_rata`'s output already respects
    # the weights (the common case: weights are the lines' full payables on a first refund)
    # the two agree exactly; the unit tests pin both the agreement and the correction.
    shares = _distribute_within_capacity(total=amount, weights=weights)

    ordered = sorted(
        ((int(line_id), int(share)) for (line_id, _), share in zip(line_payables, shares, strict=True)),
        key=lambda pair: pair[0],
    )
    return RefundSplit(shares=tuple(ordered))


def _distribute_within_capacity(*, total: int, weights: Sequence[int]) -> list[int]:
    """Capped pro-rata: in proportion to ``weights``, but never above one line's weight.

    Only reached when the ordinary path (``allocate_pro_rata``) would push a leftover minor
    unit past a line's own weight - see :func:`allocate_refund_across_lines`. The shape is
    deliberately the same algorithm, so that the two paths cannot disagree about anything
    except the one thing this one exists to fix:

    1. each line takes ``min(floor(total * weight_i / sum(weights)), weight_i)``;
    2. the units still unplaced are handed out **from the end backwards** - the frozen
       remainder rule - one unit per line, skipping any line already at its weight;
    3. if a single backward pass cannot place everything (possible only when a line's cap is
       below its proportional share, i.e. exactly the case this function exists for), the
       pass repeats from the end until the remainder is exhausted.

    The caller guarantees ``total <= sum(weights)``, so the total capacity is enough and
    step 3 terminates. Deterministic, integer-only, and sum-exact:
    ``sum(result) == total`` by construction, and ``0 <= result_i <= weights_i``.
    """
    counts = [int(weight) for weight in weights]
    weight_sum = sum(counts)
    if weight_sum == 0 or total > weight_sum:  # pragma: no cover - caller guarantees it
        raise ValidationError(
            "the refund cannot be placed within the lines' remaining capacity",
            context={"total": total, "weights": tuple(counts)},
        )

    shares = [min(int(total) * weight // weight_sum, weight) for weight in counts]
    remainder = int(total) - sum(shares)
    while remainder > 0:
        placed_this_pass = False
        for index in range(len(counts) - 1, -1, -1):
            if remainder == 0:
                break
            if shares[index] < counts[index]:
                shares[index] += 1
                remainder -= 1
                placed_this_pass = True
        if not placed_this_pass:  # pragma: no cover - the caller's bound makes it reachable
            raise ValidationError(
                "the refund could not be distributed within the lines' remaining capacity",
                context={"total": total, "undistributed": remainder},
            )
    return shares


def check_per_line_cap(
    *,
    split: RefundSplit,
    line_payables: dict[int, int],
    already_refunded: dict[int, int],
) -> None:
    """FG-12 cap 2, evaluated against freshly locked rows.

    ``already_refunded[line] + split[line] <= line_payables[line]`` for every line
    in the split. The cumulative part is why this cannot be a ``CHECK``
    constraint: the constraint sees one row, while the cap is a property of the sum
    over every refund that has ever touched that line.

    Raises the frozen ``REFUND_EXCEEDS_ITEM_AMOUNT (80005)`` on the **first** line
    that breaks, and reports which line and by how much in ``context``. A
    support engineer reading the 409 needs to know which line, and the wire's
    ``message`` deliberately does not carry it (搂109: no internals in a public
    message) - the context is for the log and for an operator-facing console.
    """
    for order_item_id, share in split.shares:
        payable = int(line_payables.get(order_item_id, 0))
        prior = int(already_refunded.get(order_item_id, 0))
        if prior + share > payable:
            raise RefundExceedsItemError(
                "this refund would exceed what the line can still be refunded for",
                context={
                    "order_item_id": order_item_id,
                    "line_payable_amount": payable,
                    "already_refunded": prior,
                    "requested_share": share,
                    "over_by": prior + share - payable,
                },
                log_detail=(
                    f"per-line refund cap: line {order_item_id} payable={payable} "
                    f"prior={prior} share={share}"
                ),
            )


def movement_quantity_for(
    *,
    claim_type: str,
    claim_items: Sequence[tuple[int, int]],
    split: RefundSplit,
) -> tuple[tuple[int, int], ...]:
    """The ``(order_item_id, quantity)`` pairs to return to stock, or nothing.

    A ``RETURN_IN`` movement is appended **only** for ``RETURN_REFUND`` claims
    (PHASE5_DESIGN 搂6.2 step 6): a ``REFUND_ONLY`` customer keeps the goods, and
    crediting them would inflate availability until the next stock count found the
    difference - a shortage that looks like shrinkage but is an accounting error in
    the opposite direction.

    Lines whose share of this refund is zero move no stock: a partial refund that
    placed nothing on a line did not buy that line back, and returning zero units
    would append a movement that changed nothing while claiming to.

    The quantity returned is the **quantity the claim names**, not a number derived
    from the money. Deriving units from an amount would let a full refund of a
    discounted line return more units than were bought.
    """
    if claim_type != AfterSaleType.RETURN_REFUND:
        return ()
    placed = split.as_dict()
    return tuple(
        (int(order_item_id), int(quantity))
        for order_item_id, quantity in sorted(claim_items, key=lambda pair: int(pair[0]))
        if placed.get(int(order_item_id), 0) > 0 and int(quantity) > 0
    )


def return_in_movement_key(*, after_sale_no: str, order_item_id: int) -> str:
    """The ``RETURN_IN`` idempotency key, frozen by PHASE5_DESIGN 搂6.2 step 6.

    Deterministic rather than random, for the same reason the inventory module's
    keys are: ``inventory_movements.idempotency_key`` is what makes a *retried*
    refund (a crash between the movement and the commit) unable to credit the same
    units twice. A random key would make the retry a second movement.
    """
    return f"return-in:{after_sale_no}:{order_item_id}"
