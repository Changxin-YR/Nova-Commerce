"""FG-12 through the RefundWorkflow: the caps no database constraint can express.

test_refund_invariants.py proves the three row-level caps are enforced by MySQL. This module
covers the half a single-row constraint CANNOT reach, and the invariants that make the other
half unreachable.

Why these probes exist rather than a re-run of the authors tests
PHASE5_DESIGN section 6.2 defines four refusals. Two of them are properties of a sum over rows,
not of a row, and the design says so explicitly:

* the per-line cumulative cap - sum(refunds for a line) + this share <= line.payable_amount
  (REFUND_EXCEEDS_ITEM_AMOUNT, 80005): enforced in RefundWorkflow because it is a sum over
  refunds per line, which a single-row CHECK cannot express;
* the claim cap - claim.refunded_amount + amount <= claim.approved_amount (80006), where
  section 5.5 states outright that refunded_amount <= approved_amount <= requested_amount is
  NOT a constraint.

Neither has a database guard, so neither raises a database error when it is broken. There is no
backstop and a defect in either is silent. That is exactly why they need adversarial probes.

Why no probe asserts REFUND_EXCEEDS_PAID_AMOUNT (80004) through the workflow
Cap 1 is real and the database enforces it - errno 3819, covered in the sibling module. But it
CANNOT be the guard that refuses a refund through the workflow, which after-sales established
and this module verifies independently: sum(order_items.payable_amount) == orders.payable_amount
== payments.paid_amount. Any amount above paid - refunded is therefore also above some claimed
lines remaining capacity, so cap 2 refuses first with 80005. A probe asserting 80004 would fail
for an arithmetic reason and read as a bug.

So this module pins the two facts that are both true and checkable instead:

* the inclusive boundary - a refund that takes refunded_amount to exactly paid_amount is
  ACCEPTED (<=, never <); and
* the identity that makes cap 1 unreachable - paid == sum(item.payable_amount). That identity
  is the premise the argument rests on, so if it ever stops holding, cap 1s refusal becomes
  reachable and the designs reasoning needs revisiting.

Not editing the authors tests
The claim lifecycle, the permission grants and the settlement come from the after-sales authors
conftest and are IMPORTED, not reimplemented - duplicating a fixture is how two seeds drift
(PHASE5_DESIGN section 12). The probes and their assertions are mine.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

# The whole fixture chain is imported, not just the leaf: pytest resolves a fixture's own
# parameters in the namespace where it is *used*, so importing `seeded_shop` without
# `_commerce_shop_instance` and `engine` leaves its dependencies unsatisfiable - observed as
# `fixture '_commerce_shop_instance' not found`.
from tests.integration.aftersales.conftest import (
    LINE_QUANTITY,
    AfterSalesShop,
    _commerce_shop_instance as _commerce_shop_fixture,
    engine as _engine_fixture,
    read_money,
    seeded_shop as _seeded_shop_fixture,
)

from app.core.errors import AppError, ErrorCode, RefundExceedsItemError
from app.shared.db.session import get_session_factory

#: Re-exported under their canonical names, because pytest resolves a fixture by the name it is
#: bound to in the requesting module. Imported directly they would collide with the `seeded_shop`
#: parameters below (ruff F811) and with the `AfterSalesShop` annotation. The whole chain is
#: re-exported, not just the leaf: pytest resolves a fixture's own parameters in the namespace
#: where it is used, so importing `seeded_shop` alone leaves `_commerce_shop_instance` and
#: `engine` unsatisfiable - observed as `fixture '_commerce_shop_instance' not found`.
_commerce_shop_instance = _commerce_shop_fixture
engine = _engine_fixture
seeded_shop = _seeded_shop_fixture

pytestmark = [pytest.mark.integration]

def _lines(session: Session, order_id: int) -> dict[int, tuple[int, int]]:
    rows = session.execute(
        text(
            "SELECT id, payable_amount, refunded_amount FROM order_items "
            "WHERE order_id = :oid ORDER BY id"
        ),
        {"oid": order_id},
    ).all()
    return {int(r[0]): (int(r[1]), int(r[2])) for r in rows}


def _approve(seeded_shop: AfterSalesShop, claim, amount: int) -> None:
    session = get_session_factory()()
    try:
        seeded_shop.approve(session, claim, amount=amount)
        session.commit()
    finally:
        session.close()


def _file(seeded_shop: AfterSalesShop, amount: int, items, suffix: str):
    """File a claim. `items` is bare item indexes (one unit each) or (index, quantity) pairs.

    The pair form exists because the eligibility cap is
    `floor(line_remaining * claimable_units / quantity)`: a claim that names only one unit of a
    three-unit line is capped at a third of that line, so a probe needing the full line's headroom
    must name every unit. Writing it the bare way first produced a correct refusal at *apply* time
    ("the requested amount is above what the order can still be refunded for") that looked like a
    defect and was a broken probe.
    """
    pairs = list(items)
    if pairs and isinstance(pairs[0], tuple):
        item_indexes = tuple(index for index, _ in pairs)
        quantities = tuple(quantity for _, quantity in pairs)
    else:
        item_indexes = tuple(pairs)
        quantities = None
    session = get_session_factory()()
    try:
        return seeded_shop.file_claim(
            session,
            amount=amount,
            item_indexes=item_indexes,
            quantities=quantities,
            suffix=suffix,
        )
    finally:
        session.close()


def test_a_refund_to_exactly_paid_amount_is_accepted(seeded_shop: AfterSalesShop) -> None:
    paid = seeded_shop.paid_amount
    assert paid > 0, "the seeded order must have been settled"

    session = get_session_factory()()
    try:
        lines = _lines(session, seeded_shop.order_id)
        items_total = sum(payable for payable, _ in lines.values())
        order_payable = int(
            session.execute(
                text("SELECT payable_amount FROM orders WHERE id = :oid"),
                {"oid": seeded_shop.order_id},
            ).scalar_one()
        )
    finally:
        session.close()

    assert items_total == order_payable == paid, (
        "sum(order_items.payable) == orders.payable_amount == payments.paid_amount is the "
        "identity that makes REFUND_EXCEEDS_PAID_AMOUNT unreachable through the workflow; got "
        f"{items_total} / {order_payable} / {paid}"
    )

    # ## Why this is ONE full-width claim, and not two claims reaching the boundary
    #
    # Eligibility caps a claim at, **per line**,
    #     floor((payable_amount - refunded_amount) * claimable_units / line_quantity)
    # and a claim **permanently consumes** the units it names. Both facts were established by
    # building this probe the other way and being refused, correctly, twice:
    #
    # * naming one unit of a three-unit line caps the claim at a third of that line, so a later
    #   claim cannot "pick up the remainder";
    # * naming all three units leaves nothing for a later claim ("the claim asks for more units
    #   than the line can still be claimed for").
    #
    # With `q = 3` that gives three nested headroom levels only - 1/3, 2/3, 3/3 of each line - and
    # they cannot be composed to reach `refunded == paid` exactly: after a first refund of `a1`
    # naming all units, the second can name at most 2 units and so is capped at
    # `floor(2*(paid - a1)/3)`, which is >= `paid - a1` only when `a1 >= paid`. A refund of zero
    # is not a refund. (Checked against the implemented allocator; the shortfall is real, not an
    # artefact of my earlier equal-line assumption.)
    #
    # A single claim at full width reaches the boundary exactly - the caps at 3 units per line
    # sum to precisely the paid amount - so that is what this probe uses. It is also the shape the
    # frontier actually takes: the last refund is the one that closes the order.
    session = get_session_factory()()
    try:
        lines = _lines(session, seeded_shop.order_id)
    finally:
        session.close()
    ids = sorted(lines)
    assert len(ids) == 3, f"the seed gives three lines, got {len(ids)}"
    assert sum(payable for payable, _ in lines.values()) == paid, (
        "the lines must sum to the paid amount, or a full-width claim cannot reach the boundary: "
        f"{lines} vs paid={paid}"
    )

    # One claim naming every unit of every line, for the whole paid amount.
    claim = _file(seeded_shop, paid, ((index, LINE_QUANTITY) for index in range(3)), "exact-1")
    _approve(seeded_shop, claim, paid)
    seeded_shop.refund(None, claim, amount=paid, suffix="exact-r1")

    session = get_session_factory()()
    try:
        money = read_money(session, order_no=seeded_shop.order_no, payment_no=seeded_shop.payment_no)
        assert money["payment_refunded"] == paid, (
            "the second refund must take refunded to exactly paid: "
            f"{money['payment_refunded']} vs {paid}"
        )
        assert money["payment_refunded"] <= money["payment_paid"]
        assert money["payment_status"] == "REFUNDED", money
        assert money["after_sale_status"] == "REFUNDED", money
        assert money["order_refunded"] == paid, money
        assert money["line_refunded_total"] == paid, money
    finally:
        session.close()

    session = get_session_factory()()
    try:
        with pytest.raises(AppError) as caught:
            seeded_shop.file_claim(session, amount=1, item_indexes=(0,), suffix="exact-3")
        # Asserted by CODE, not by class name: `_error()` builds the classes dynamically, so a
        # name-based assertion silently stops meaning anything the moment one is renamed.
        assert caught.value.code == ErrorCode.AFTER_SALE_NOT_ELIGIBLE, (
            "a further claim against a fully refunded order must be refused as "
            f"AFTER_SALE_NOT_ELIGIBLE, got {caught.value.code}"
        )
    finally:
        session.rollback()
        session.close()


def test_the_per_line_cumulative_cap_binds_while_the_order_still_has_room(
    seeded_shop: AfterSalesShop,
) -> None:
    session = get_session_factory()()
    try:
        lines = _lines(session, seeded_shop.order_id)
    finally:
        session.close()

    ids = sorted(lines)
    assert len(ids) == 3, f"the seed gives three lines, got {len(ids)}"
    target = ids[0]
    target_payable = lines[target][0]

    claim = _file(seeded_shop, target_payable, (0,), "line-1")
    _approve(seeded_shop, claim, target_payable)
    seeded_shop.refund(None, claim, amount=target_payable, suffix="line-r1")

    session = get_session_factory()()
    try:
        money = read_money(session, order_no=seeded_shop.order_no, payment_no=seeded_shop.payment_no)
        after = _lines(session, seeded_shop.order_id)
        assert after[target] == (target_payable, target_payable), (
            f"the refunded line must sit exactly at its payable amount: {after[target]}"
        )
        assert money["order_refunded"] == target_payable
        assert money["order_refunded"] < money["order_paid"], (
            "this probe is only meaningful while the order has refundable money left; "
            f"order refunded={money['order_refunded']} of paid={money['order_paid']}"
        )
        assert money["payment_refunded"] < money["payment_paid"]
        assert money["payment_status"] == "PARTIAL_REFUNDED", money
        assert money["after_sale_status"] == "PARTIAL_REFUNDED", money
    finally:
        session.close()

    session = get_session_factory()()
    try:
        with pytest.raises(AppError) as caught:
            seeded_shop.file_claim(session, amount=1, item_indexes=(0,), suffix="line-2")
        assert caught.value.code == ErrorCode.AFTER_SALE_NOT_ELIGIBLE, (
            "claiming an exhausted line must be refused as AFTER_SALE_NOT_ELIGIBLE, got "
            f"{caught.value.code}"
        )
    finally:
        session.rollback()
        session.close()

    # The claim was refunded in full, so it is COMPLETED and its state guard fires. Asserted as
    # a typed AppError, which is the property that matters: no raw database error escapes.
    with pytest.raises(AppError) as caught:
        seeded_shop.refund(None, claim, amount=1, suffix="line-r2")
    assert caught.value.code in {
        ErrorCode.AFTER_SALE_STATE_INVALID,
        ErrorCode.REFUND_AMOUNT_INVALID,
        ErrorCode.REFUND_EXCEEDS_ITEM_AMOUNT,
        ErrorCode.AFTER_SALE_NOT_ELIGIBLE,
    }, f"unexpected refusal code {caught.value.code}"

    session = get_session_factory()()
    try:
        assert _lines(session, seeded_shop.order_id)[target] == (target_payable, target_payable)
        money = read_money(session, order_no=seeded_shop.order_no, payment_no=seeded_shop.payment_no)
        assert money["order_refunded"] == target_payable
        assert money["line_refunded_total"] == target_payable
    finally:
        session.close()


def test_the_workflow_revalidates_the_caps_against_locked_rows(
    seeded_shop: AfterSalesShop,
) -> None:
    session = get_session_factory()()
    try:
        lines = _lines(session, seeded_shop.order_id)
    finally:
        session.close()

    ids = sorted(lines)
    line_a, line_b = ids[0], ids[1]
    amount_a = lines[line_a][0]
    amount_b = lines[line_b][0]
    paid = seeded_shop.paid_amount

    claim_a = _file(seeded_shop, amount_a, (0,), "stale-a")
    _approve(seeded_shop, claim_a, amount_a)
    claim_b = _file(seeded_shop, amount_b, (1,), "stale-b")
    _approve(seeded_shop, claim_b, amount_b)

    seeded_shop.refund(None, claim_a, amount=amount_a, suffix="stale-ra")

    session = get_session_factory()()
    try:
        after = _lines(session, seeded_shop.order_id)
        money = read_money(session, order_no=seeded_shop.order_no, payment_no=seeded_shop.payment_no)
        assert after[line_a] == (amount_a, amount_a), "line A must be at its cap"
        assert money["order_refunded"] == amount_a
        assert money["payment_refunded"] + amount_b <= paid, (
            "cap 1 must not be what refuses this, or the probe tests the wrong guard: "
            f"refunded={money['payment_refunded']} + {amount_b} <= paid {paid}"
        )
        assert after[line_b] == (amount_b, 0), "line B must be untouched at this point"
    finally:
        session.close()

    try:
        seeded_shop.refund(None, claim_b, amount=amount_b, suffix="stale-rb")
        accepted = True
    except RefundExceedsItemError as refusal:
        accepted = False
        assert refusal.code == ErrorCode.REFUND_EXCEEDS_ITEM_AMOUNT, refusal.code

    session = get_session_factory()()
    try:
        after = _lines(session, seeded_shop.order_id)
        for line_id, (payable, refunded) in after.items():
            assert refunded <= payable, (
                f"line {line_id} was refunded past its payable amount: {refunded} > {payable}"
            )
        money = read_money(session, order_no=seeded_shop.order_no, payment_no=seeded_shop.payment_no)
        assert money["payment_refunded"] <= money["payment_paid"]
        assert money["order_refunded"] <= money["order_paid"]
        assert money["line_refunded_total"] <= money["line_paid_total"]
        expected = (
            "REFUNDED" if money["order_refunded"] == money["order_paid"] else "PARTIAL_REFUNDED"
        )
        assert money["payment_status"] == expected, (accepted, money)
        assert money["after_sale_status"] == expected, (accepted, money)
    finally:
        session.close()
