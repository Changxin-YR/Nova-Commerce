"""Every refusal the pricing authority can make, and why each one exists.

These are the branches that fire when a caller breaks a contract: a wrong type,
a rule handed over as the wrong object, or a ``CartPrice`` that was not built by
:class:`PricingService` and therefore does not add up. None of them is a business
error - the user cannot cause one - so each must fail loudly and internally
rather than pricing something plausible.

A test per branch is worth it here because the *cost* of the branch not existing
is a wrong price that passes every test that only exercises the happy path. The
class of bug these guards catch - a discount allocated to a line that does not
have the money - is invisible in the response body and only surfaces as a failed
CHECK constraint at the end of a write transaction.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.modules.pricing import (
    CouponRule,
    CouponType,
    DiscountAllocation,
    PricedLine,
    PricingInvariantError,
    PromotionRule,
)


# ---------------------------------------------------------------------------
# Inputs that are not what they claim to be
# ---------------------------------------------------------------------------
def test_a_line_that_is_not_a_priced_line_is_refused(pricing) -> None:
    with pytest.raises(PricingInvariantError, match="PricedLine"):
        pricing.calculate_cart_price([{"sku_id": 1, "quantity": 1}])


def test_a_non_integer_sku_id_is_refused(pricing, make_line) -> None:
    with pytest.raises(PricingInvariantError, match="sku_id"):
        pricing.calculate_cart_price([make_line(sku_id=1.5)])


def test_a_non_integer_quantity_is_refused(pricing, make_line) -> None:
    with pytest.raises(PricingInvariantError, match="quantity"):
        pricing.calculate_cart_price([make_line(quantity="2")])


def test_a_promotion_that_is_not_a_rule_is_refused(pricing, make_line) -> None:
    """A wire payload must not be able to price anything by accident."""
    with pytest.raises(PricingInvariantError, match="PromotionRule"):
        pricing.calculate_cart_price([make_line()], promotion="FULL_REDUCTION")


def test_a_coupon_that_is_not_a_rule_is_refused(pricing, make_line) -> None:
    with pytest.raises(PricingInvariantError, match="CouponRule"):
        pricing.calculate_cart_price([make_line()], coupon={"face_value_amount": 100})


def test_a_non_string_promotion_type_is_refused(pricing, make_line) -> None:
    with pytest.raises(PricingInvariantError, match="promotion_type"):
        pricing.calculate_cart_price(
            [make_line()],
            promotion=PromotionRule(promotion_id=1, promotion_type=39),  # type: ignore[arg-type]
        )


def test_a_coupon_scope_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(PricingInvariantError, match="applicable_sku_ids"):
        CouponRule(
            coupon_id=5,
            coupon_type=CouponType.FIXED_AMOUNT,
            applicable_sku_ids=5.0,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# A CartPrice that was not built here
# ---------------------------------------------------------------------------
def _hand_built(pricing, make_line, **overrides):
    """A one-line cart with fields overwritten - the "not built here" case."""
    cart = pricing.calculate_cart_price([make_line(sku_id=1, unit_price=1000)])
    return replace(cart, **overrides)


def test_a_negative_promotion_allocation_is_refused(pricing, make_line) -> None:
    broken = _hand_built(
        pricing,
        make_line,
        promotion_discount_amount=-5,
        promotion_allocations=(DiscountAllocation(sku_id=1, amount=-5),),
    )

    with pytest.raises(PricingInvariantError, match="must not be negative"):
        pricing.build_price_snapshot(broken)


def test_a_duplicated_allocation_is_refused(pricing, make_line) -> None:
    """Two entries for one SKU would let one line be discounted twice."""
    broken = _hand_built(
        pricing,
        make_line,
        promotion_allocations=(
            DiscountAllocation(sku_id=1, amount=0),
            DiscountAllocation(sku_id=1, amount=0),
        ),
    )

    with pytest.raises(PricingInvariantError, match="twice"):
        pricing.build_price_snapshot(broken)


def test_a_cart_whose_lines_do_not_add_up_to_its_original_is_refused(pricing, make_line) -> None:
    broken = _hand_built(pricing, make_line, original_amount=1001, payable_amount=1001)

    with pytest.raises(PricingInvariantError, match="sum of its item amounts"):
        pricing.build_price_snapshot(broken)


def test_an_allocation_whose_sum_disagrees_with_the_coupon_total_is_refused(pricing, make_line) -> None:
    broken = _hand_built(
        pricing,
        make_line,
        coupon_allocations=(DiscountAllocation(sku_id=1, amount=5),),
        coupon_discount_amount=0,
    )

    with pytest.raises(PricingInvariantError, match="coupon allocations"):
        pricing.build_price_snapshot(broken)


def test_a_cart_with_an_allocation_for_a_line_not_in_it_is_refused(pricing, make_line) -> None:
    broken = _hand_built(
        pricing,
        make_line,
        promotion_allocations=(
            DiscountAllocation(sku_id=1, amount=0),
            DiscountAllocation(sku_id=999, amount=0),
        ),
    )

    with pytest.raises(PricingInvariantError, match="not in the cart"):
        pricing.build_price_snapshot(broken)


def test_a_coupon_over_an_over_allocated_promotion_is_refused(pricing, make_line) -> None:
    """The promotion took more than the line has: stop, do not price a coupon on it."""
    broken = _hand_built(
        pricing,
        make_line,
        promotion_discount_amount=2000,
        promotion_allocations=(DiscountAllocation(sku_id=1, amount=2000),),
        payable_amount=-1000,
    )

    with pytest.raises(PricingInvariantError, match="exceeds the line"):
        pricing.apply_coupon(
            broken, CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT, face_value_amount=100)
        )


def test_looking_up_an_allocation_for_an_absent_sku_is_refused(pricing, make_line) -> None:
    cart = pricing.calculate_cart_price([make_line(sku_id=1, unit_price=1000)])

    with pytest.raises(PricingInvariantError, match="no allocation recorded"):
        cart.promotion_allocation(999)
    with pytest.raises(PricingInvariantError, match="no allocation recorded"):
        cart.coupon_allocation(999)


def test_a_service_error_renders_its_message_without_context() -> None:
    """The log line has to be readable when there is no context to add."""
    assert str(PricingInvariantError("plain failure")) == "plain failure"


def test_snapshot_counts_units_not_lines(pricing, make_line) -> None:
    snapshot = pricing.build_price_snapshot(
        pricing.calculate_cart_price([make_line(sku_id=1, quantity=2), make_line(sku_id=2, quantity=3)])
    )

    assert snapshot.item_count == 5


def test_item_price_of_a_non_line_is_refused(pricing) -> None:
    with pytest.raises(PricingInvariantError, match="PricedLine"):
        pricing.calculate_item_price(None)  # type: ignore[arg-type]


def test_a_priced_line_is_still_usable_after_normalisation(pricing, make_line) -> None:
    """Normalisation must not silently drop the snapshot fields (INV-014)."""
    line = make_line(sku_id=1, unit_price=1000, sku_snapshot={"color": "black"})
    item = pricing.calculate_item_price(line)

    assert isinstance(item.line, PricedLine)
    assert item.line.sku_snapshot == {"color": "black"}
    assert item.line.product_name == line.product_name
