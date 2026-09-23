"""The frozen shapes: wire vocabulary, immutability, and the module's borders.

Three things are checked here, all of them interfaces rather than behaviour:

1. **The wire vocabulary.** ``promotion_type`` and ``coupon_type`` are strings
   the client switches on (contract §13.1/§13.3). If this module ever renamed a
   member, the API would change without a single price being wrong - which is
   exactly the kind of breakage that a money test suite never catches.
2. **The value objects are value objects.** Frozen, slotted, hashable, equal by
   value. A priced cart that can be mutated in place is a cart whose total can
   stop matching its lines.
3. **The module's import borders.** §37 makes this context the single price
   authority and §5 forbids it from importing another module's models. That is
   asserted by reading the package's own ASTs, so the claim fails the build
   rather than drifting.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

import app.modules.pricing as pricing_package
from app.core.errors import AppError
from app.modules.pricing import (
    CartPrice,
    CouponRule,
    CouponType,
    DiscountAllocation,
    ItemPrice,
    PricedLine,
    PriceSnapshot,
    PricingInvariantError,
    PromotionRule,
    PromotionType,
)

PRICING_ROOT = pathlib.Path(pricing_package.__file__).parent


# ---------------------------------------------------------------------------
# 1. Wire vocabulary
# ---------------------------------------------------------------------------
def test_promotion_types_are_the_frozen_wire_values() -> None:
    """API_CONTRACT §13.1: DIRECT_DISCOUNT | PERCENT_DISCOUNT | FULL_REDUCTION."""
    assert [member.value for member in PromotionType] == [
        "DIRECT_DISCOUNT",
        "PERCENT_DISCOUNT",
        "FULL_REDUCTION",
    ]


def test_coupon_types_are_the_frozen_wire_values() -> None:
    """API_CONTRACT §13.3: FIXED_AMOUNT | PERCENT_DISCOUNT."""
    assert [member.value for member in CouponType] == ["FIXED_AMOUNT", "PERCENT_DISCOUNT"]


def test_warning_codes_are_stable_strings() -> None:
    assert [member.value for member in pricing_package.PricingWarning] == [
        "PROMOTION_SCOPE_NO_MATCH",
        "PROMOTION_THRESHOLD_NOT_MET",
        "COUPON_SCOPE_NO_MATCH",
        "COUPON_THRESHOLD_NOT_MET",
    ]


def test_public_surface_is_the_frozen_set() -> None:
    """A consumer may rely on ``app.modules.pricing`` exporting exactly this."""
    assert set(pricing_package.__all__) == {
        "COUPON_TYPES",
        "DEFAULT_SHIPPING_POLICY",
        "MAX_DISCOUNT_BPS",
        "PRICING_WARNINGS",
        "PROMOTION_TYPES",
        "CartPrice",
        "CouponRule",
        "CouponType",
        "DiscountAllocation",
        "FreeShippingPolicy",
        "ItemPrice",
        "PriceSnapshot",
        "PricedLine",
        "PricingInvariantError",
        "PricingService",
        "PricingWarning",
        "PromotionRule",
        "PromotionType",
        "ShippingPolicy",
        "allocate_pro_rata",
    }
    for name in pricing_package.__all__:
        assert hasattr(pricing_package, name), name


def test_service_is_importable_from_both_paths() -> None:
    """Whatever import order-flow guessed, both spellings are the same class."""
    from app.modules.pricing import service as service_module

    assert service_module.PricingService is pricing_package.PricingService


def test_pricing_error_is_never_a_wire_code() -> None:
    """PHASE4_DESIGN §2: internal, never a business code.

    If this ever became an ``AppError`` someone would have to invent a client
    code for an internal bug, and §95 makes such a code permanent.
    """
    assert not issubclass(PricingInvariantError, AppError)
    assert PricingInvariantError("boom").context == {}
    assert "sku_id=3" in str(PricingInvariantError("boom", context={"sku_id": 3}))


# ---------------------------------------------------------------------------
# 2. Value semantics
# ---------------------------------------------------------------------------
def test_priced_lines_are_frozen(make_line) -> None:
    line = make_line(sku_id=1, unit_price=1000)

    with pytest.raises(dataclasses.FrozenInstanceError):
        line.quantity = 5  # type: ignore[misc]


def test_price_snapshot_is_frozen(pricing, make_line) -> None:
    snapshot = pricing.build_price_snapshot(pricing.calculate_cart_price([make_line()]))

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.payable_amount = 0  # type: ignore[misc]


def test_value_objects_use_slots(make_line) -> None:
    """Slots is not a micro-optimisation here: no ``__dict__`` means no ad-hoc

    attribute can be attached to a priced cart and read later as if it were part
    of the price.
    """
    for obj in (
        make_line(),
        ItemPrice(line=make_line(), original_amount=1),
        DiscountAllocation(sku_id=1, amount=1),
        PromotionRule(promotion_id=1, promotion_type=PromotionType.DIRECT_DISCOUNT),
        CouponRule(coupon_id=5, coupon_type=CouponType.FIXED_AMOUNT),
    ):
        assert not hasattr(obj, "__dict__"), type(obj).__name__


def test_value_objects_compare_by_value_and_hash(make_line) -> None:
    first = make_line(sku_id=1, unit_price=1000)
    second = make_line(sku_id=1, unit_price=1000)

    assert first == second
    assert hash(first) == hash(second)
    assert {first, second} == {first}
    assert first != make_line(sku_id=1, unit_price=1001)


def test_rule_scope_is_normalised_to_a_frozenset() -> None:
    """Phase 6 hands over a JSON array; a ``set`` field would be unhashable."""
    promotion = PromotionRule(
        promotion_id=1,
        promotion_type=PromotionType.DIRECT_DISCOUNT,
        applicable_sku_ids=[3, 7],  # type: ignore[arg-type]
    )
    coupon = CouponRule(
        coupon_id=5,
        coupon_type=CouponType.FIXED_AMOUNT,
        applicable_sku_ids=(1,),  # type: ignore[arg-type]
    )

    assert promotion.applicable_sku_ids == frozenset({3, 7})
    assert isinstance(promotion.applicable_sku_ids, frozenset)
    assert isinstance(coupon.applicable_sku_ids, frozenset)
    assert hash(promotion) == hash(promotion)


def test_null_scope_means_every_line() -> None:
    promotion = PromotionRule(promotion_id=1, promotion_type=PromotionType.DIRECT_DISCOUNT)

    assert promotion.covers(12345)
    assert promotion.covers(1)


def test_scoped_rule_covers_only_its_skus() -> None:
    promotion = PromotionRule(
        promotion_id=1,
        promotion_type=PromotionType.DIRECT_DISCOUNT,
        applicable_sku_ids=frozenset({3}),
    )

    assert promotion.covers(3)
    assert not promotion.covers(4)


def test_scope_of_the_wrong_type_is_refused() -> None:
    with pytest.raises(PricingInvariantError, match="applicable_sku_ids"):
        PromotionRule(
            promotion_id=1,
            promotion_type=PromotionType.DIRECT_DISCOUNT,
            applicable_sku_ids="all",  # type: ignore[arg-type]
        )


def test_cart_price_defaults(pricing, make_line) -> None:
    cart = pricing.calculate_cart_price([make_line(sku_id=1, unit_price=1000, quantity=2)])

    assert isinstance(cart, CartPrice)
    assert cart.warnings == ()
    assert cart.shipping_amount == 0
    assert cart.total_discount_amount == 0


def test_snapshot_shape_is_the_frozen_field_list() -> None:
    """PHASE4_DESIGN §5 - the workflow persists exactly these fields."""
    assert [field.name for field in dataclasses.fields(PriceSnapshot)] == [
        "items",
        "original_amount",
        "promotion_discount_amount",
        "coupon_discount_amount",
        "shipping_amount",
        "payable_amount",
        "promotion_allocations",
        "coupon_allocations",
        "promotion_ids",
        "coupon_ids",
        "shipping_policy",
    ]


def test_cart_price_shape_is_the_frozen_field_list() -> None:
    assert [field.name for field in dataclasses.fields(CartPrice)] == [
        "items",
        "original_amount",
        "promotion_discount_amount",
        "coupon_discount_amount",
        "shipping_amount",
        "payable_amount",
        "promotion_allocations",
        "coupon_allocations",
        "warnings",
    ]


def test_priced_line_shape_is_the_frozen_field_list() -> None:
    assert [field.name for field in dataclasses.fields(PricedLine)] == [
        "sku_id",
        "product_id",
        "product_name",
        "sku_name",
        "image_object_key",
        "image_url",
        "sku_snapshot",
        "unit_price",
        "quantity",
    ]


# ---------------------------------------------------------------------------
# 3. Import borders: pure, DB-free, and no other module's models
# ---------------------------------------------------------------------------
def _imported_modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def test_pricing_imports_nothing_but_itself_and_the_standard_library() -> None:
    """§37 + §5: pure, DB-free, and no other context's models.

    The allowed set is deliberately tiny. Anything else - SQLAlchemy, the
    catalog, marketing, the settings object, a clock - would mean the price
    authority has a second input, and a price that depends on when or where it
    was asked cannot be re-derived during a dispute.
    """
    allowed = {
        "__future__",
        "collections.abc",
        "dataclasses",
        "decimal",
        "enum",
        "typing",
    }
    offenders: dict[str, set[str]] = {}
    for path in sorted(PRICING_ROOT.glob("*.py")):
        for module in _imported_modules(path):
            if module in allowed or module.startswith("app.modules.pricing"):
                continue
            offenders.setdefault(path.name, set()).add(module)

    assert offenders == {}


def test_pricing_module_files_are_the_frozen_list() -> None:
    assert sorted(path.name for path in PRICING_ROOT.glob("*.py")) == [
        "__init__.py",
        "allocation.py",
        "enums.py",
        "errors.py",
        "service.py",
        "shipping.py",
        "value_objects.py",
    ]
