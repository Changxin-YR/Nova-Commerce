"""``PricingService`` - the three promotion types, the coupon, and the caps.

Spec §37 (one price authority), §39 (the promotion types), §41 (allocation),
API_CONTRACT §13.1/§13.3 (the rule shapes this consumes), §14.2/§14.4 (the wire
shape and the exact-sum rule), PHASE4_DESIGN §6 (the algorithm) and §11 (what
"done" means).

The tests are written against *money*, not against internals: every assertion is
either a contract field with a hand-computed value or an invariant that a wrong
price would break. A test that only restates the implementation would pass just
as happily against a wrong algorithm.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest

from app.modules.pricing import (
    CartPrice,
    CouponRule,
    CouponType,
    DiscountAllocation,
    FreeShippingPolicy,
    ItemPrice,
    PricedLine,
    PricingInvariantError,
    PricingService,
    PricingWarning,
    PromotionRule,
    PromotionType,
)


# ---------------------------------------------------------------------------
# calculate_item_price - the only unambiguous arithmetic in commerce
# ---------------------------------------------------------------------------
def test_item_price_is_unit_price_times_quantity(pricing: PricingService) -> None:
    line = PricedLine(
        sku_id=3,
        product_id=7,
        product_name="Nova Phone 15 Pro",
        sku_name="原色钛金属 256GB",
        image_object_key=None,
        image_url=None,
        sku_snapshot=None,
        unit_price=299900,
        quantity=1,
    )
    item = pricing.calculate_item_price(line)

    assert item.original_amount == 299900
    assert item.line is line


def test_item_price_multiplies_quantity(pricing: PricingService, make_line) -> None:
    assert pricing.calculate_item_price(make_line(unit_price=1500, quantity=4)).original_amount == 6000


def test_zero_priced_line_is_allowed(pricing: PricingService, make_line) -> None:
    """A free SKU is legal; it is the *allocation* that must not exceed it."""
    assert pricing.calculate_item_price(make_line(unit_price=0)).original_amount == 0


@pytest.mark.parametrize("quantity", [0, -1, -100])
def test_non_positive_quantity_is_refused(pricing: PricingService, make_line, quantity: int) -> None:
    with pytest.raises(PricingInvariantError, match="quantity"):
        pricing.calculate_item_price(make_line(quantity=quantity))


def test_negative_unit_price_is_refused(pricing: PricingService, make_line) -> None:
    with pytest.raises(PricingInvariantError, match="unit price"):
        pricing.calculate_item_price(make_line(unit_price=-1))


@pytest.mark.parametrize("bad_price", [19.99, 0.1, "1000", None, True])
def test_non_integer_money_is_refused(pricing: PricingService, make_line, bad_price: object) -> None:
    with pytest.raises(PricingInvariantError):
        pricing.calculate_item_price(make_line(unit_price=bad_price))  # type: ignore[arg-type]


def test_exact_decimal_money_is_normalised_to_minor_units(pricing: PricingService, make_line) -> None:
    """A DB ``DECIMAL`` that is a whole number of cents is accepted, once."""
    item = pricing.calculate_item_price(make_line(unit_price=Decimal("1000"), quantity=2))

    assert item.original_amount == 2000
    assert isinstance(item.line.unit_price, int)


def test_sub_cent_money_is_refused(pricing: PricingService, make_line) -> None:
    with pytest.raises(PricingInvariantError, match="whole number"):
        pricing.calculate_item_price(make_line(unit_price=Decimal("10.005")))


# ---------------------------------------------------------------------------
# Duplicate SKUs are merged before pricing (§14.2, order_items UNIQUE)
# ---------------------------------------------------------------------------
def test_duplicate_skus_are_merged_and_quantities_summed(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price([make_line(sku_id=3, quantity=1), make_line(sku_id=3, quantity=2)])

    assert len(cart.items) == 1
    assert cart.items[0].line.quantity == 3
    assert cart.original_amount == 3000


def test_merge_keeps_first_appearance_order(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price([make_line(sku_id=5), make_line(sku_id=3), make_line(sku_id=5)])

    assert [item.line.sku_id for item in cart.items] == [5, 3]
    assert cart.original_amount == 3000


def test_duplicate_skus_with_different_prices_are_refused(pricing: PricingService, make_line) -> None:
    with pytest.raises(PricingInvariantError, match="snapshots"):
        pricing.calculate_cart_price([make_line(sku_id=3), make_line(sku_id=3, unit_price=2000)])


def test_duplicate_skus_with_different_names_are_refused(pricing: PricingService, make_line) -> None:
    with pytest.raises(PricingInvariantError, match="snapshots"):
        pricing.calculate_cart_price(
            [make_line(sku_id=3), make_line(sku_id=3, product_name="Something else")]
        )


# ---------------------------------------------------------------------------
# Promotions (§39, §13.1)
# ---------------------------------------------------------------------------
def test_direct_discount_applies_per_unit(pricing: PricingService, make_line) -> None:
    """``discount_amount`` is minor units **per unit**, so quantity multiplies it."""
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000, quantity=1), make_line(sku_id=2, unit_price=1000, quantity=1)],
        promotion=PromotionRule(
            promotion_id=1, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=100
        ),
    )

    assert cart.promotion_discount_amount == 200
    assert cart.payable_amount == 1800
    assert cart.promotion_allocations == (
        DiscountAllocation(sku_id=1, amount=100),
        DiscountAllocation(sku_id=2, amount=100),
    )


def test_direct_discount_per_unit_times_quantity(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000, quantity=3)],
        promotion=PromotionRule(
            promotion_id=1, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=100
        ),
    )

    assert cart.promotion_discount_amount == 300
    assert cart.payable_amount == 2700


def test_direct_discount_is_capped_by_the_eligible_amount(pricing: PricingService, make_line) -> None:
    """A rule that promises more than the line has cannot create negative money."""
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000, quantity=2)],
        promotion=PromotionRule(
            promotion_id=1, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=5000
        ),
    )

    assert cart.promotion_discount_amount == 2000
    assert cart.payable_amount == 0
    assert cart.item_payable_amount(1) == 0


def test_direct_discount_ignores_max_discount_amount(pricing: PricingService, make_line) -> None:
    """§13.1 defines no ``max_discount_amount`` for DIRECT_DISCOUNT.

    Honouring an undefined field would be this module inventing contract; the
    eligible-amount cap still bounds the result.
    """
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000, quantity=1)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.DIRECT_DISCOUNT,
            discount_amount=100,
            max_discount_amount=10,
        ),
    )

    assert cart.promotion_discount_amount == 100


def test_promotion_scope_limits_the_eligible_lines(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000, quantity=1), make_line(sku_id=2, unit_price=1000, quantity=1)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.DIRECT_DISCOUNT,
            discount_amount=100,
            applicable_sku_ids=frozenset({1}),
        ),
    )

    assert cart.promotion_discount_amount == 100
    assert cart.payable_amount == 1900
    # Out-of-scope lines carry an explicit zero, not a missing entry.
    assert cart.promotion_allocations == (
        DiscountAllocation(sku_id=1, amount=100),
        DiscountAllocation(sku_id=2, amount=0),
    )
    assert cart.item_payable_amount(2) == 1000


def test_promotion_scope_with_no_match_warns(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.DIRECT_DISCOUNT,
            discount_amount=100,
            applicable_sku_ids=frozenset({999}),
        ),
    )

    assert cart.promotion_discount_amount == 0
    assert cart.warnings == (PricingWarning.PROMOTION_SCOPE_NO_MATCH,)


def test_percent_discount_floors(pricing: PricingService, make_line) -> None:
    """1250 bps == 12.5%; 299900 x 12.5% is 37487.5, so 37487 - never a float."""
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=299900)],
        promotion=PromotionRule(
            promotion_id=1, promotion_type=PromotionType.PERCENT_DISCOUNT, discount_bps=1250
        ),
    )

    assert cart.promotion_discount_amount == 37487
    assert cart.payable_amount == 262413


def test_percent_discount_max_cap(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=10000)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.PERCENT_DISCOUNT,
            discount_bps=5000,
            max_discount_amount=3000,
        ),
    )

    assert cart.promotion_discount_amount == 3000
    assert cart.payable_amount == 7000


def test_percent_discount_of_100_percent_takes_the_whole_amount(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1234)],
        promotion=PromotionRule(
            promotion_id=1, promotion_type=PromotionType.PERCENT_DISCOUNT, discount_bps=10000
        ),
    )

    assert cart.promotion_discount_amount == 1234
    assert cart.payable_amount == 0


@pytest.mark.parametrize("bad_bps", [10001, -1, 100000])
def test_out_of_range_rate_is_refused(pricing: PricingService, make_line, bad_bps: int) -> None:
    with pytest.raises(PricingInvariantError, match="basis points"):
        pricing.calculate_cart_price(
            [make_line()],
            promotion=PromotionRule(
                promotion_id=1, promotion_type=PromotionType.PERCENT_DISCOUNT, discount_bps=bad_bps
            ),
        )


def test_percent_discount_allocates_without_losing_a_cent(pricing: PricingService, make_line) -> None:
    """Weights 333/333/334 sharing 100: 33 each leaves one unit over."""
    cart = pricing.calculate_cart_price(
        [
            make_line(sku_id=1, unit_price=333),
            make_line(sku_id=2, unit_price=333),
            make_line(sku_id=3, unit_price=334),
        ],
        promotion=PromotionRule(
            promotion_id=1, promotion_type=PromotionType.PERCENT_DISCOUNT, discount_bps=1000
        ),
    )

    assert cart.original_amount == 1000
    assert cart.promotion_discount_amount == 100
    assert [allocation.amount for allocation in cart.promotion_allocations] == [33, 33, 34]
    assert cart.payable_amount == 900


def test_full_reduction_applies_at_the_threshold(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=3000)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.FULL_REDUCTION,
            threshold_amount=3000,
            reduction_amount=300,
        ),
    )

    assert cart.promotion_discount_amount == 300
    assert cart.payable_amount == 2700


def test_full_reduction_below_the_threshold_does_nothing_and_warns(
    pricing: PricingService, make_line
) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=2999)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.FULL_REDUCTION,
            threshold_amount=3000,
            reduction_amount=300,
        ),
    )

    assert cart.promotion_discount_amount == 0
    assert cart.payable_amount == 2999
    assert cart.warnings == (PricingWarning.PROMOTION_THRESHOLD_NOT_MET,)


def test_full_reduction_threshold_uses_the_eligible_scope_only(pricing: PricingService, make_line) -> None:
    """A 3000 threshold on a cart of 4000 whose only eligible line is 1000 is not met."""
    cart = pricing.calculate_cart_price(
        [
            make_line(sku_id=1, unit_price=1000),
            make_line(sku_id=2, unit_price=3000),
        ],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.FULL_REDUCTION,
            threshold_amount=3000,
            reduction_amount=300,
            applicable_sku_ids=frozenset({1}),
        ),
    )

    assert cart.promotion_discount_amount == 0
    assert cart.warnings == (PricingWarning.PROMOTION_THRESHOLD_NOT_MET,)


def test_full_reduction_caps_at_the_eligible_amount(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.FULL_REDUCTION,
            threshold_amount=0,
            reduction_amount=999_999,
        ),
    )

    assert cart.promotion_discount_amount == 1000
    assert cart.payable_amount == 0


def test_full_reduction_respects_its_max_cap(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=10_000)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.FULL_REDUCTION,
            threshold_amount=0,
            reduction_amount=5000,
            max_discount_amount=1000,
        ),
    )

    assert cart.promotion_discount_amount == 1000


def test_promotion_type_accepts_its_wire_string(pricing: PricingService, make_line) -> None:
    """Phase 6 builds these from JSON ``rule_config``, where the discriminator is a string."""
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type="FULL_REDUCTION",  # type: ignore[arg-type]
            threshold_amount=0,
            reduction_amount=100,
        ),
    )

    assert cart.promotion_discount_amount == 100


def test_unknown_promotion_type_is_refused(pricing: PricingService, make_line) -> None:
    with pytest.raises(PricingInvariantError, match="promotion_type"):
        pricing.calculate_cart_price(
            [make_line()],
            promotion=PromotionRule(
                promotion_id=1,
                promotion_type="BUY_ONE_GET_ONE",  # type: ignore[arg-type]
            ),
        )


def test_negative_discount_amount_is_refused(pricing: PricingService, make_line) -> None:
    with pytest.raises(PricingInvariantError, match="discount_amount"):
        pricing.calculate_cart_price(
            [make_line()],
            promotion=PromotionRule(
                promotion_id=1, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=-100
            ),
        )


def test_applying_a_promotion_replaces_the_previous_one(pricing: PricingService, make_line) -> None:
    """Stacking is a Phase 6 decision (§13.1 ``stackable``); this replaces."""
    lines = [make_line(sku_id=1, unit_price=1000)]
    first = pricing.calculate_cart_price(
        lines,
        promotion=PromotionRule(
            promotion_id=1, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=100
        ),
    )
    second = pricing.apply_promotion(
        first,
        PromotionRule(promotion_id=2, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=300),
    )

    assert first.promotion_discount_amount == 100
    assert second.promotion_discount_amount == 300
    assert second.payable_amount == 700


def test_replacing_a_promotion_clears_the_stale_warning(pricing: PricingService, make_line) -> None:
    lines = [make_line(sku_id=1, unit_price=1000)]
    warned = pricing.calculate_cart_price(
        lines,
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.DIRECT_DISCOUNT,
            discount_amount=100,
            applicable_sku_ids=frozenset({999}),
        ),
    )
    assert warned.warnings == (PricingWarning.PROMOTION_SCOPE_NO_MATCH,)

    fixed = pricing.apply_promotion(
        warned,
        PromotionRule(promotion_id=2, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=100),
    )

    assert fixed.warnings == ()


# ---------------------------------------------------------------------------
# Coupons (§13.3), applied against what the promotion left
# ---------------------------------------------------------------------------
def test_fixed_coupon(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)],
        coupon=CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=500),
    )

    assert cart.coupon_discount_amount == 500
    assert cart.payable_amount == 500
    assert cart.coupon_allocations == (DiscountAllocation(sku_id=1, amount=500),)


def test_fixed_coupon_is_capped_by_the_cart(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)],
        coupon=CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=5000),
    )

    assert cart.coupon_discount_amount == 1000
    assert cart.payable_amount == 0


def test_coupon_below_its_threshold_does_nothing_and_warns(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)],
        coupon=CouponRule(
            coupon_id=5,
            coupon_type=CouponType.FIXED_AMOUNT,
            face_value_amount=500,
            threshold_amount=19900,
        ),
    )

    assert cart.coupon_discount_amount == 0
    assert cart.payable_amount == 1000
    assert cart.warnings == (PricingWarning.COUPON_THRESHOLD_NOT_MET,)


def test_coupon_threshold_is_measured_before_the_promotion(pricing: PricingService, make_line) -> None:
    """§6: "threshold on eligible original" - a promotion cannot push an order

    back below a coupon's threshold and silently void the coupon the customer
    selected.
    """
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=2000)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.FULL_REDUCTION,
            threshold_amount=0,
            reduction_amount=1500,
        ),
        coupon=CouponRule(
            coupon_id=5,
            coupon_type=CouponType.FIXED_AMOUNT,
            face_value_amount=100,
            threshold_amount=2000,
        ),
    )

    assert cart.promotion_discount_amount == 1500
    assert cart.coupon_discount_amount == 100
    assert cart.payable_amount == 400


def test_coupon_applies_after_the_promotion(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000), make_line(sku_id=2, unit_price=1000)],
        promotion=PromotionRule(
            promotion_id=1, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=100
        ),
        coupon=CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=500),
    )

    assert cart.promotion_discount_amount == 200
    assert cart.coupon_discount_amount == 500
    assert cart.payable_amount == 1300


def test_percent_coupon_uses_what_the_promotion_left(pricing: PricingService, make_line) -> None:
    """2000 original - 200 promotion = 1800 remaining; 10% of that is 180, not 200."""
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000), make_line(sku_id=2, unit_price=1000)],
        promotion=PromotionRule(
            promotion_id=1, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=100
        ),
        coupon=CouponRule(coupon_id=5, coupon_type=CouponType.PERCENT_DISCOUNT, discount_bps=1000),
    )

    assert cart.coupon_discount_amount == 180
    assert cart.payable_amount == 1620
    assert sum(a.amount for a in cart.coupon_allocations) == 180


def test_coupon_is_capped_by_what_the_promotion_left(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000), make_line(sku_id=2, unit_price=1000)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.FULL_REDUCTION,
            threshold_amount=0,
            reduction_amount=1900,
        ),
        coupon=CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=500),
    )

    assert cart.promotion_discount_amount == 1900
    assert cart.coupon_discount_amount == 100
    assert cart.payable_amount == 0


def test_percent_coupon_respects_its_max_cap(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)],
        coupon=CouponRule(
            coupon_id=5,
            coupon_type=CouponType.PERCENT_DISCOUNT,
            discount_bps=5000,
            max_discount_amount=100,
        ),
    )

    assert cart.coupon_discount_amount == 100


def test_coupon_scope_limits_the_eligible_lines(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000), make_line(sku_id=2, unit_price=1000)],
        coupon=CouponRule(
            coupon_id=5,
            coupon_type=CouponType.FIXED_AMOUNT,
            face_value_amount=500,
            applicable_sku_ids=frozenset({1}),
        ),
    )

    assert cart.coupon_discount_amount == 500
    assert cart.coupon_allocations == (
        DiscountAllocation(sku_id=1, amount=500),
        DiscountAllocation(sku_id=2, amount=0),
    )
    assert cart.item_payable_amount(2) == 1000


def test_coupon_with_no_eligible_line_warns(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)],
        coupon=CouponRule(
            coupon_id=5,
            coupon_type=CouponType.FIXED_AMOUNT,
            face_value_amount=500,
            applicable_sku_ids=frozenset({999}),
        ),
    )

    assert cart.coupon_discount_amount == 0
    assert cart.warnings == (PricingWarning.COUPON_SCOPE_NO_MATCH,)


def test_coupon_against_fully_discounted_lines_warns_and_consumes_nothing(
    pricing: PricingService, make_line
) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.FULL_REDUCTION,
            threshold_amount=0,
            reduction_amount=1000,
        ),
        coupon=CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=500),
    )

    assert cart.payable_amount == 0
    assert cart.coupon_discount_amount == 0
    assert cart.warnings == (PricingWarning.COUPON_SCOPE_NO_MATCH,)


def test_promotion_warning_survives_a_coupon(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.DIRECT_DISCOUNT,
            discount_amount=100,
            applicable_sku_ids=frozenset({999}),
        ),
        coupon=CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=100),
    )

    assert cart.warnings == (PricingWarning.PROMOTION_SCOPE_NO_MATCH,)
    assert cart.coupon_discount_amount == 100


def test_applying_a_promotion_that_overdraws_an_existing_coupon_is_refused(
    pricing: PricingService, make_line
) -> None:
    """The correct order is promotion-then-coupon; this order is a bug, loudly."""
    lines = [make_line(sku_id=1, unit_price=1000)]
    with_coupon = pricing.calculate_cart_price(
        lines,
        coupon=CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=900),
    )

    with pytest.raises(PricingInvariantError, match="negative"):
        pricing.apply_promotion(
            with_coupon,
            PromotionRule(
                promotion_id=1,
                promotion_type=PromotionType.FULL_REDUCTION,
                threshold_amount=0,
                reduction_amount=500,
            ),
        )


def test_warnings_are_plain_strings_for_the_wire(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)],
        promotion=PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.DIRECT_DISCOUNT,
            discount_amount=100,
            applicable_sku_ids=frozenset({999}),
        ),
    )

    assert json.dumps({"warnings": list(cart.warnings)}) == '{"warnings": ["PROMOTION_SCOPE_NO_MATCH"]}'


def test_no_rules_means_no_warnings(pricing: PricingService, make_line) -> None:
    assert pricing.calculate_cart_price([make_line()]).warnings == ()


# ---------------------------------------------------------------------------
# Shipping (§14.4)
# ---------------------------------------------------------------------------
def test_shipping_is_free_by_default(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price([make_line(sku_id=1, unit_price=1000)])

    assert cart.shipping_amount == 0
    assert cart.payable_amount == 1000


def test_explicit_free_policy_is_free(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)], shipping_policy=FreeShippingPolicy()
    )

    assert cart.shipping_amount == 0


def test_non_zero_shipping_is_reported_by_the_cart(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)], shipping_policy=_FlatShippingPolicy(500)
    )

    assert cart.shipping_amount == 500
    assert cart.payable_amount == 1500


def test_non_zero_shipping_cannot_become_a_snapshot(pricing: PricingService, make_line) -> None:
    """A shipping charge has no per-item home, so it would break INV-006 by construction."""
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)], shipping_policy=_FlatShippingPolicy(500)
    )

    with pytest.raises(PricingInvariantError, match="INV-006"):
        pricing.build_price_snapshot(cart)


def test_negative_shipping_is_refused(pricing: PricingService, make_line) -> None:
    with pytest.raises(PricingInvariantError, match="negative"):
        pricing.calculate_shipping(pricing.calculate_cart_price([make_line()]), _FlatShippingPolicy(-1))


def test_non_integer_shipping_is_refused(pricing: PricingService, make_line) -> None:
    with pytest.raises(PricingInvariantError):
        pricing.calculate_shipping(
            pricing.calculate_cart_price([make_line()]),
            _FlatShippingPolicy(9.99),  # type: ignore[arg-type]
        )


class _FlatShippingPolicy:
    """A policy that charges a flat amount - the shape V2 will need."""

    def __init__(self, amount: object) -> None:
        self.name = "FLAT"
        self._amount = amount

    def calculate(self, cart: CartPrice) -> int:
        return self._amount  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# The snapshot (INV-006, INV-014's carrier)
# ---------------------------------------------------------------------------
def test_snapshot_carries_the_frozen_fields(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000)],
        promotion=PromotionRule(
            promotion_id=11, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=100
        ),
        coupon=CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=50),
    )
    snapshot = pricing.build_price_snapshot(cart, promotion_ids=(11,), coupon_ids=(5,))

    assert snapshot.items == cart.items
    assert snapshot.original_amount == 1000
    assert snapshot.promotion_discount_amount == 100
    assert snapshot.coupon_discount_amount == 50
    assert snapshot.shipping_amount == 0
    assert snapshot.payable_amount == 850
    assert snapshot.promotion_ids == (11,)
    assert snapshot.coupon_ids == (5,)
    assert snapshot.shipping_policy == "FREE"


def test_snapshot_labels_the_shipping_policy(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price([make_line()])
    snapshot = pricing.build_price_snapshot(cart, shipping_policy=FreeShippingPolicy().name)

    assert snapshot.shipping_policy == "FREE"


def test_snapshot_per_item_lookups_match_the_cart(pricing: PricingService, make_line) -> None:
    """The consumer's recipe: original - allocated, allocation looked up by SKU."""
    cart = pricing.calculate_cart_price(
        [make_line(sku_id=1, unit_price=1000), make_line(sku_id=2, unit_price=2000)],
        promotion=PromotionRule(
            promotion_id=11, promotion_type=PromotionType.DIRECT_DISCOUNT, discount_amount=100
        ),
        coupon=CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=300),
    )
    snapshot = pricing.build_price_snapshot(cart)

    for sku_id, item in ((1, 1000), (2, 2000)):
        assert snapshot.item_payable_amount(sku_id) == item - snapshot.allocated_discount(sku_id)
        assert snapshot.allocated_discount(sku_id) == (
            snapshot.promotion_allocation(sku_id) + snapshot.coupon_allocation(sku_id)
        )
    assert sum(snapshot.item_payable_amount(s.line.sku_id) for s in snapshot.items) == 3000 - 200 - 300


def test_snapshot_refuses_a_cart_whose_total_drifted(pricing: PricingService, make_line) -> None:
    """This is INV-006's executable check, not a comment."""
    cart = pricing.calculate_cart_price([make_line(sku_id=1, unit_price=1000)])
    broken = replace(cart, payable_amount=cart.payable_amount - 1)

    with pytest.raises(PricingInvariantError, match="INV-006"):
        pricing.build_price_snapshot(broken)


def test_snapshot_refuses_an_allocation_that_does_not_cover_every_line(
    pricing: PricingService, make_line
) -> None:
    cart = pricing.calculate_cart_price([make_line(sku_id=1, unit_price=1000)])
    broken = replace(cart, promotion_allocations=())

    with pytest.raises(PricingInvariantError, match="allocation"):
        pricing.build_price_snapshot(broken)


def test_snapshot_refuses_a_negative_item_payable(pricing: PricingService, make_line) -> None:
    """Allocation larger than the line is rejected even if the order total adds up."""
    cart = pricing.calculate_cart_price([make_line(sku_id=1, unit_price=1000)])
    broken = replace(
        cart,
        promotion_discount_amount=5000,
        promotion_allocations=(DiscountAllocation(sku_id=1, amount=5000),),
        payable_amount=-4000,
    )

    with pytest.raises(PricingInvariantError, match="negative"):
        pricing.build_price_snapshot(broken)


def test_snapshot_refuses_allocations_that_do_not_sum_to_the_order_discount(
    pricing: PricingService, make_line
) -> None:
    cart = pricing.calculate_cart_price([make_line(sku_id=1, unit_price=1000)])
    broken = replace(cart, promotion_discount_amount=100)

    with pytest.raises(PricingInvariantError, match="sum"):
        pricing.build_price_snapshot(broken)


def test_lookup_of_an_unknown_sku_is_refused(pricing: PricingService, make_line) -> None:
    cart = pricing.calculate_cart_price([make_line(sku_id=1, unit_price=1000)])

    with pytest.raises(PricingInvariantError, match="not part of this priced cart"):
        cart.item_payable_amount(999)


# ---------------------------------------------------------------------------
# Empty cart, and the preview wire shape
# ---------------------------------------------------------------------------
def test_empty_cart_prices_to_zero(pricing: PricingService) -> None:
    """ "Empty" is a business condition with its own code (CART_EMPTY 50001)."""
    cart = pricing.calculate_cart_price([])

    assert cart.items == ()
    assert cart.original_amount == 0
    assert cart.payable_amount == 0
    assert cart.item_count == 0
    assert pricing.build_price_snapshot(cart).payable_amount == 0


def test_preview_item_amounts_match_the_contract_field_names(pricing: PricingService, make_line) -> None:
    """The §14.2 preview item, assembled only from the authority's own outputs."""
    cart = pricing.calculate_cart_price(
        [
            PricedLine(
                sku_id=3,
                product_id=7,
                product_name="Nova Phone 15 Pro",
                sku_name="原色钛金属 256GB",
                image_object_key=None,
                image_url=None,
                sku_snapshot=None,
                unit_price=299900,
                quantity=1,
            )
        ]
    )

    preview_items = [
        {
            "sku_id": item.line.sku_id,
            "product_id": item.line.product_id,
            "product_name": item.line.product_name,
            "sku_name": item.line.sku_name,
            "image_url": item.line.image_url,
            "unit_price": item.line.unit_price,
            "quantity": item.line.quantity,
            "original_amount": item.original_amount,
            "promotion_discount_amount": cart.promotion_allocation(item.line.sku_id),
            "coupon_discount_amount": cart.coupon_allocation(item.line.sku_id),
            "allocated_discount_amount": cart.allocated_discount(item.line.sku_id),
            "payable_amount": cart.item_payable_amount(item.line.sku_id),
        }
        for item in cart.items
    ]

    assert preview_items == [
        {
            "sku_id": 3,
            "product_id": 7,
            "product_name": "Nova Phone 15 Pro",
            "sku_name": "原色钛金属 256GB",
            "image_url": None,
            "unit_price": 299900,
            "quantity": 1,
            "original_amount": 299900,
            "promotion_discount_amount": 0,
            "coupon_discount_amount": 0,
            "allocated_discount_amount": 0,
            "payable_amount": 299900,
        }
    ]


def test_cart_item_count_counts_units_not_lines(pricing: PricingService, make_line) -> None:
    """``orders.item_count`` is total units (§11 addendum), not line count."""
    cart = pricing.calculate_cart_price([make_line(sku_id=1, quantity=2), make_line(sku_id=2, quantity=3)])

    assert cart.item_count == 5
    assert len(cart.items) == 2


def test_item_price_dataclass_is_the_documented_shape(pricing: PricingService, make_line) -> None:
    item = pricing.calculate_item_price(make_line(sku_id=4, unit_price=250, quantity=3))

    assert isinstance(item, ItemPrice)
    assert item == ItemPrice(line=make_line(sku_id=4, unit_price=250, quantity=3), original_amount=750)
