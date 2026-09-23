"""INV-006 exactness, swept over the shapes that break naive pro-rata.

INV-006: ``Order.payable_amount == SUM(OrderItem.payable_amount)``, enforced by
the allocation algorithm *and* by the assertion in ``build_price_snapshot``.
This file is the executable evidence for the algorithm half: it walks every
combination of awkward item sets, promotion variants and coupon variants, and
asserts the whole chain of money identities plus per-item non-negativity.

The item sets are chosen adversarially, not representatively: a total that does
not divide, lines of one minor unit each, and free lines (unit price 0) in both
first and last position. Those are the shapes where "the remainder goes to the
last line" stops working if it is implemented literally, and where a wrong
implementation produces a negative ``item.payable_amount`` - money that exists in
the arithmetic and nowhere in the ledger.
"""

from __future__ import annotations

import pytest

from app.modules.pricing import (
    CouponRule,
    CouponType,
    DiscountAllocation,
    PricedLine,
    PricingService,
    PromotionRule,
    PromotionType,
)

# name -> ((unit_price, quantity), ...) with sku ids assigned 1..n in order
ITEM_SETS: dict[str, tuple[tuple[int, int], ...]] = {
    "single expensive line": ((299900, 1),),
    "two equal lines": ((1000, 1), (1000, 1)),
    "three lines that do not divide": ((333, 1), (333, 1), (334, 1)),
    "twenty unit lines": tuple((1, 1) for _ in range(20)),
    "mixed quantities": ((100, 2), (250, 1), (1, 3)),
    "free line last": ((999, 1), (1, 1), (0, 1)),
    "free line first": ((0, 1), (999, 1), (1, 1)),
    "quantity three hundred": ((333, 3),),
}

PROMOTIONS: dict[str, PromotionRule | None] = {
    "none": None,
    "direct 1 per unit": PromotionRule(
        promotion_id=1, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=1
    ),
    "direct 100 per unit": PromotionRule(
        promotion_id=1, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=100
    ),
    "direct absurd": PromotionRule(
        promotion_id=1, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=10_000_000
    ),
    "percent 1bp": PromotionRule(
        promotion_id=1, promotion_type=PromotionType.PERCENT_DISCOUNT, discount_bps=1
    ),
    "percent 33.33%": PromotionRule(
        promotion_id=1, promotion_type=PromotionType.PERCENT_DISCOUNT, discount_bps=3333
    ),
    "percent 100%": PromotionRule(
        promotion_id=1, promotion_type=PromotionType.PERCENT_DISCOUNT, discount_bps=10000
    ),
    "percent capped at 7": PromotionRule(
        promotion_id=1,
        promotion_type=PromotionType.PERCENT_DISCOUNT,
        discount_bps=5000,
        max_discount_amount=7,
    ),
    "reduction 1": PromotionRule(
        promotion_id=1,
        promotion_type=PromotionType.FULL_REDUCTION,
        threshold_amount=0,
        reduction_amount=1,
    ),
    "reduction 99": PromotionRule(
        promotion_id=1,
        promotion_type=PromotionType.FULL_REDUCTION,
        threshold_amount=1000,
        reduction_amount=99,
    ),
    "reduction absurd": PromotionRule(
        promotion_id=1,
        promotion_type=PromotionType.FULL_REDUCTION,
        threshold_amount=0,
        reduction_amount=999_999_999,
    ),
    "direct 50 on sku 1 only": PromotionRule(
        promotion_id=1,
        promotion_type=PromotionType.DIRECT_DISCOUNT,
        discount_amount=50,
        applicable_sku_ids=frozenset({1}),
    ),
}

COUPONS: dict[str, CouponRule | None] = {
    "none": None,
    "fixed 1": CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=1),
    "fixed 333": CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=333),
    "fixed absurd": CouponRule(
        coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=999_999_999
    ),
    "percent 1bp": CouponRule(coupon_id=5, coupon_type=CouponType.PERCENT_DISCOUNT, discount_bps=1),
    "percent 99.99%": CouponRule(coupon_id=5, coupon_type=CouponType.PERCENT_DISCOUNT, discount_bps=9999),
    "percent capped at 3": CouponRule(
        coupon_id=5,
        coupon_type=CouponType.PERCENT_DISCOUNT,
        discount_bps=2500,
        max_discount_amount=3,
    ),
    "fixed 100 above threshold 1000": CouponRule(
        coupon_id=5,
        coupon_type=CouponType.FIXED_AMOUNT,
        face_value_amount=100,
        threshold_amount=1000,
    ),
}


def _lines(make_line, spec: tuple[tuple[int, int], ...]) -> list[PricedLine]:
    return [
        make_line(sku_id=index + 1, unit_price=unit_price, quantity=quantity)
        for index, (unit_price, quantity) in enumerate(spec)
    ]


def _check_every_money_identity(pricing: PricingService, cart, label: str) -> None:
    """The full chain, asserted in one place so every case gets all of it."""
    item_total = sum(item.original_amount for item in cart.items)

    assert cart.original_amount == item_total, label
    assert cart.payable_amount == (
        cart.original_amount
        - cart.promotion_discount_amount
        - cart.coupon_discount_amount
        + cart.shipping_amount
    ), label
    assert cart.payable_amount >= 0, label
    assert sum(a.amount for a in cart.promotion_allocations) == cart.promotion_discount_amount, label
    assert sum(a.amount for a in cart.coupon_allocations) == cart.coupon_discount_amount, label
    assert [a.sku_id for a in cart.promotion_allocations] == [item.line.sku_id for item in cart.items], label
    assert [a.sku_id for a in cart.coupon_allocations] == [item.line.sku_id for item in cart.items], label

    per_item_payable = 0
    for item in cart.items:
        sku_id = item.line.sku_id
        allocated = cart.allocated_discount(sku_id)
        payable = cart.item_payable_amount(sku_id)

        assert 0 <= allocated <= item.original_amount, (label, sku_id, allocated)
        assert payable == item.original_amount - allocated, (label, sku_id)
        assert payable >= 0, (label, sku_id, payable)
        per_item_payable += payable

    assert per_item_payable == cart.payable_amount, (label, per_item_payable, cart.payable_amount)

    snapshot = pricing.build_price_snapshot(
        cart,
        promotion_ids=() if cart.promotion_discount_amount == 0 else (1,),
        coupon_ids=() if cart.coupon_discount_amount == 0 else (5,),
    )
    assert snapshot.payable_amount == cart.payable_amount, label
    assert (
        sum(snapshot.item_payable_amount(item.line.sku_id) for item in snapshot.items)
        == snapshot.payable_amount
    ), label


def test_inv006_holds_across_every_combination(pricing: PricingService, make_line) -> None:
    checked = 0
    for set_name, spec in ITEM_SETS.items():
        lines = _lines(make_line, spec)
        for promotion_name, promotion in PROMOTIONS.items():
            for coupon_name, coupon in COUPONS.items():
                label = f"{set_name} | {promotion_name} | {coupon_name}"
                cart = pricing.calculate_cart_price(lines, promotion=promotion, coupon=coupon)
                _check_every_money_identity(pricing, cart, label)
                checked += 1

    assert checked == len(ITEM_SETS) * len(PROMOTIONS) * len(COUPONS)
    assert checked >= 700


@pytest.mark.parametrize("sku_id", list(range(1, 101)))
def test_hundred_unit_lines_with_a_coarse_coupon(pricing: PricingService, make_line, sku_id: int) -> None:
    """100 lines of 1 minor unit, a 50-unit coupon: the frozen rule's worst case.

    Every share floors to zero, so the remainder (50) is larger than any single
    line. The literal "remainder to the last line" reading would give that line
    ``1 - 50`` payable - a negative amount, which the database CHECK rejects.
    Each of the 100 lines is checked by parameterisation so a failure names the
    line rather than the cart.
    """
    lines = [make_line(sku_id=number, unit_price=1) for number in range(1, 101)]
    cart = pricing.calculate_cart_price(
        lines, coupon=CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=50)
    )

    assert cart.coupon_discount_amount == 50
    assert cart.payable_amount == 50
    assert sum(a.amount for a in cart.coupon_allocations) == 50
    assert cart.coupon_allocation(sku_id) <= 1
    assert cart.item_payable_amount(sku_id) >= 0


def test_a_free_line_never_absorbs_the_remainder(pricing: PricingService, make_line) -> None:
    """Free line last, and the remainder must not land on it.

    ``1 - 1`` of nothing: the line has no money to discount, so a remainder
    handed to it would be a discount that exists only in the arithmetic.
    """
    lines = [
        make_line(sku_id=1, unit_price=1),
        make_line(sku_id=2, unit_price=1),
        make_line(sku_id=3, unit_price=0),
    ]
    cart = pricing.calculate_cart_price(
        lines, coupon=CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=1)
    )

    assert cart.coupon_discount_amount == 1
    assert cart.coupon_allocations == (
        DiscountAllocation(sku_id=1, amount=0),
        DiscountAllocation(sku_id=2, amount=1),
        DiscountAllocation(sku_id=3, amount=0),
    )
    assert cart.item_payable_amount(3) == 0
    assert sum(cart.item_payable_amount(i) for i in (1, 2, 3)) == cart.payable_amount == 1


def test_promotion_and_coupon_do_not_double_discount_the_same_money(
    pricing: PricingService, make_line
) -> None:
    """600 of promotion plus 600 of coupon on a 1000 cart cannot both land."""
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.FULL_REDUCTION,
            threshold_amount=0,
            reduction_amount=600,
        ),
        coupon=CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=600),
    )

    assert cart.promotion_discount_amount == 600
    assert cart.coupon_discount_amount == 400
    assert cart.payable_amount == 0
    assert cart.item_payable_amount(1) == 0


def test_discount_never_makes_money_even_when_rules_are_maximal(pricing: PricingService, make_line) -> None:
    """The worst pair of rules against the smallest cart still lands on zero."""
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1), make_line(sku_id=2, unit_price=0)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.PERCENT_DISCOUNT,
            discount_bps=10000,
        ),
        coupon=CouponRule(coupon_id=5, coupon_type=CouponType.PERCENT_DISCOUNT, discount_bps=10000),
    )

    assert cart.payable_amount == 0
    assert all(cart.item_payable_amount(item.line.sku_id) == 0 for item in cart.items)
    assert sum(a.amount for a in cart.coupon_allocations) == 0
