"""FG-10 (2/4) - INV-006 through the real transaction, with discounts applied.

Spec §37/§41, §112 INV-006, API_CONTRACT §14.4, PHASE4_DESIGN §6/§7.

The internal ``pricing_rules`` seam is the only reason Phase 4 can satisfy this part
of FG-10 at all: marketing (the resolver that turns a promotion row into a
:class:`PromotionRule`) is Phase 6, but the *arithmetic* is Phase 4, and an
allocation that has only ever been unit-tested has never been tested against the
database's own restatement of it.

## What makes this test worth running

Every ``order_items`` row carries two CHECK constraints -
``allocated = promotion + coupon`` and ``payable = original - allocated`` - and
``orders`` carries ``payable = original - promotion - coupon + shipping``. So a
discount split that did not add up would be **rejected by MySQL**, not merely
asserted here. This test drives the allocation through the transaction and then reads
the committed rows back, so it proves the two mechanisms agree rather than that one of
them exists.

## Why the weights do not divide evenly

The prices are 1999 / 2999 / 999 and the quantities are coprime, so every pro-rata
split leaves a genuine remainder. Testing the remainder rule with round numbers is
how a suite goes green while ``SUM(items.payable_amount)`` quietly differs from
``orders.payable_amount`` by one unit - which is exactly the class of defect INV-006
exists to catch.
"""

from __future__ import annotations

import pytest

from app.modules.order.service import OrderService
from app.modules.order.workflow import OrderLineInput, PricingRules
from app.modules.pricing.value_objects import CouponRule, PromotionRule
from app.shared.db.session import get_session_factory

from .conftest import Shop, items_of

pytestmark = [pytest.mark.integration]

#: Quantities chosen so the three line totals share no common factor with most
#: percentage discounts.
WEIGHTS = (2, 3, 5)


def _floor_share(total: int, weight: int, weight_sum: int) -> int:
    return total * weight // weight_sum


def _create_with_rules(shop: Shop, *, rules: PricingRules, suffix: str = "priced"):
    session = get_session_factory()()
    try:
        return OrderService(session).create_order(
            principal=shop.consumer,
            items=[
                OrderLineInput(shop.sku_ids[index], quantity)
                for index, quantity in enumerate(WEIGHTS)
            ],
            address_id=shop.address_id,
            client_request_id=shop.client_request_id(suffix),
            idempotency_key=shop.key(suffix),
            coupon_id=rules.coupon.coupon_id if rules.coupon is not None else None,
            pricing_rules=rules,
        )
    finally:
        session.close()


def _committed(shop: Shop, order_id: int):
    """Read the committed order and its lines back through a **fresh** session.

    A fresh session on purpose: it re-reads from the database instead of returning
    objects the workflow still has in its identity map, so the assertions below are
    about what was actually persisted rather than about what the code intended to
    persist.
    """
    from app.modules.order.models import Order

    session = get_session_factory()()
    try:
        order = session.get(Order, order_id)
        assert order is not None, "the order was not committed"
        return order, items_of(session, order_id=order_id)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# A promotion only
# ---------------------------------------------------------------------------
def test_a_percent_promotion_allocates_exactly_and_the_remainder_lands_last(shop: Shop) -> None:
    """§41: pro-rata by original amount, remainder absorbed by the **last** eligible
    line. The remainder rule is what makes INV-006 hold exactly rather than to within a
    cent - integer division always leaves something over and somebody has to take it.

    §6's refined statement is "the remainder is handed out one minor unit at a time,
    walking backwards from the last line and skipping zero-weight lines, and every
    allocation is bounded by its own line's amount". For an ordinary cart - every line
    positive, and the last line able to absorb a remainder of at most n-1 units - that is
    exactly "the last line takes it", which is what is asserted below. The two rules only
    diverge when the last line cannot absorb the remainder without exceeding its own
    weight, and that cannot happen through the order path: a weight is
    ``unit_price x quantity`` with both factors required to be positive, so no line here
    is ever zero-weight.

    The expected shares are recomputed from the SKU prices and the basis points, and the
    remainder is *placed by the rule* rather than read back from the result.
    """
    rules = PricingRules(
        promotion=PromotionRule(
            promotion_id=101,
            promotion_type="PERCENT_DISCOUNT",
            discount_bps=1250,  # 12.5%, chosen so the split does not divide evenly
        )
    )
    result = _create_with_rules(shop, rules=rules)

    order, items = _committed(shop, result.order.id)
    originals = [item.original_amount for item in items]
    weight_sum = sum(originals)

    # Reproduce the order-level discount independently, from the SKU prices.
    expected_original = sum(
        shop.sku_prices[index] * quantity for index, quantity in enumerate(WEIGHTS)
    )
    assert order.original_amount == expected_original
    assert weight_sum == expected_original

    expected_discount = weight_sum * 1250 // 10_000
    assert order.promotion_discount_amount == expected_discount

    # Each line's share is the floor of its proportion, except the last, which takes
    # the remainder - so the sum is exact.
    shares = [_floor_share(expected_discount, weight, weight_sum) for weight in originals]
    remainder = expected_discount - sum(shares)
    assert items[-1].promotion_discount_amount == shares[-1] + remainder
    for index in range(len(items) - 1):
        assert items[index].promotion_discount_amount == shares[index]

    # §6's two bounds: never negative, and never more than the line is worth. The second
    # is what stops an allocation from driving `payable_amount` below zero - which
    # `ck_order_items_amounts_non_negative` would reject at flush time, turning a pricing
    # bug into a 500 rather than a wrong number.
    for item in items:
        assert 0 <= item.promotion_discount_amount <= item.original_amount
        assert item.payable_amount >= 0

    # And the invariant itself, from the committed rows.
    assert sum(item.payable_amount for item in items) == order.payable_amount
    assert order.payable_amount == order.original_amount - order.promotion_discount_amount


# ---------------------------------------------------------------------------
# A promotion and a coupon
# ---------------------------------------------------------------------------
def test_a_coupon_is_applied_after_the_promotion_on_the_discounted_total(shop: Shop) -> None:
    """§6 step 3: the coupon works against ``eligible_original - promotion_allocated``.

    Ordering matters commercially: applying the coupon to the undiscounted total would
    let a customer stack discounts into a deeper cut than either rule promises.
    """
    rules = PricingRules(
        promotion=PromotionRule(
            promotion_id=101,
            promotion_type="PERCENT_DISCOUNT",
            discount_bps=1250,
        ),
        coupon=CouponRule(
            coupon_id=202,
            coupon_type="FIXED_AMOUNT",
            face_value_amount=500,
            threshold_amount=1000,
        ),
    )
    result = _create_with_rules(shop, rules=rules, suffix="promo-coupon")

    order, items = _committed(shop, result.order.id)
    assert order.coupon_id == 202

    weight_sum = order.original_amount
    promotion = weight_sum * 1250 // 10_000
    assert order.promotion_discount_amount == promotion

    # The coupon sees the post-promotion total, not the original.
    remaining_total = weight_sum - promotion
    expected_coupon = min(500, remaining_total)
    assert order.coupon_discount_amount == expected_coupon

    # Allocation of the coupon is pro-rata over each line's *remaining* amount, with
    # the remainder on the last line.
    remaining = [item.original_amount - item.promotion_discount_amount for item in items]
    remaining_sum = sum(remaining)
    coupon_shares = [_floor_share(expected_coupon, value, remaining_sum) for value in remaining]
    coupon_remainder = expected_coupon - sum(coupon_shares)
    assert items[-1].coupon_discount_amount == coupon_shares[-1] + coupon_remainder
    for index in range(len(items) - 1):
        assert items[index].coupon_discount_amount == coupon_shares[index]

    # INV-006, and the per-row restatements MySQL enforces.
    assert sum(item.payable_amount for item in items) == order.payable_amount
    for item in items:
        assert item.allocated_discount_amount == (
            item.promotion_discount_amount + item.coupon_discount_amount
        )
        assert item.payable_amount == item.original_amount - item.allocated_discount_amount
        assert item.payable_amount >= 0

    # The frozen order-level identity (§14.4).
    assert order.payable_amount == (
        order.original_amount
        - order.promotion_discount_amount
        - order.coupon_discount_amount
        + order.shipping_amount
    )


def test_a_full_reduction_promotion_below_its_threshold_does_nothing(shop: Shop) -> None:
    """A supplied rule that does nothing is not an error - but the total must still be
    right, and the customer's payable must equal the sum of the lines."""
    rules = PricingRules(
        promotion=PromotionRule(
            promotion_id=101,
            promotion_type="FULL_REDUCTION",
            threshold_amount=10_000_000,
            reduction_amount=50_000,
        )
    )
    result = _create_with_rules(shop, rules=rules, suffix="threshold")

    order, items = _committed(shop, result.order.id)
    assert order.promotion_discount_amount == 0
    assert order.payable_amount == order.original_amount
    assert sum(item.payable_amount for item in items) == order.payable_amount


def test_a_direct_discount_can_never_make_a_line_negative(shop: Shop) -> None:
    """The caps are what keep ``payable >= 0`` - and ``ck_order_items_payable_consistent``
    plus the non-negative CHECK would reject the alternative at flush time."""
    rules = PricingRules(
        promotion=PromotionRule(
            promotion_id=101,
            promotion_type="DIRECT_DISCOUNT",
            # Far more per unit than any line is worth.
            discount_amount=100_000,
        )
    )
    result = _create_with_rules(shop, rules=rules, suffix="capped")

    order, items = _committed(shop, result.order.id)
    assert order.payable_amount == 0
    assert all(item.payable_amount == 0 for item in items)
    assert sum(item.payable_amount for item in items) == order.payable_amount
    assert order.promotion_discount_amount == order.original_amount


def test_scoped_promotion_allocates_only_to_eligible_lines(shop: Shop) -> None:
    """An ineligible line must receive exactly zero, and the sum must still be exact -
    the case where a "spread it across everything" implementation looks fine until
    someone reads the line detail."""
    rules = PricingRules(
        promotion=PromotionRule(
            promotion_id=101,
            promotion_type="PERCENT_DISCOUNT",
            discount_bps=2500,
            applicable_sku_ids=frozenset({shop.sku_ids[0], shop.sku_ids[2]}),
        )
    )
    result = _create_with_rules(shop, rules=rules, suffix="scoped")

    order, items = _committed(shop, result.order.id)
    by_sku = {item.sku_id: item for item in items}

    assert by_sku[shop.sku_ids[1]].promotion_discount_amount == 0
    assert by_sku[shop.sku_ids[1]].payable_amount == by_sku[shop.sku_ids[1]].original_amount

    eligible_total = (
        by_sku[shop.sku_ids[0]].original_amount + by_sku[shop.sku_ids[2]].original_amount
    )
    assert order.promotion_discount_amount == eligible_total * 2500 // 10_000
    assert (
        by_sku[shop.sku_ids[0]].promotion_discount_amount
        + by_sku[shop.sku_ids[2]].promotion_discount_amount
        == order.promotion_discount_amount
    )
    assert sum(item.payable_amount for item in items) == order.payable_amount


def test_the_internal_seam_is_not_reachable_from_http() -> None:
    """The seam exists for tests and Phase 6. If it ever became a request field, a
    client could name its own discount - which is §38 inverted and far worse.

    Asserted against the request model rather than by reading the handler, because the
    request model is what decides what can arrive.
    """
    from app.modules.order.schemas import CreateOrderRequest

    assert "pricing_rules" not in CreateOrderRequest.model_fields
    assert "promotion_id" not in CreateOrderRequest.model_fields
    assert "coupon_rule" not in CreateOrderRequest.model_fields


def test_the_movement_key_survives_a_discount(shop: Shop) -> None:
    """Stock is reserved by SKU and quantity; a discount must not change what is
    locked, or a promotion would become a way to reserve an unlimited number of units."""
    from .conftest import read_position

    rules = PricingRules(
        promotion=PromotionRule(
            promotion_id=101, promotion_type="PERCENT_DISCOUNT", discount_bps=5000
        )
    )
    _create_with_rules(shop, rules=rules, suffix="stock")

    session = get_session_factory()()
    try:
        for index, quantity in enumerate(WEIGHTS):
            available, locked, _version = read_position(
                session, sku_id=shop.sku_ids[index], warehouse_id=shop.warehouse_id
            )
            assert locked == quantity
            assert available == 1000 - quantity
    finally:
        session.close()
