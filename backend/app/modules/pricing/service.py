"""``PricingService`` - the single price authority (spec §37).

There is exactly one place in this system that decides what a cart costs, and
this is it. §37 forbids the alternatives that are always proposed next: a
second price on the order path ("just recompute it in the workflow"), a price
stored on the cart row, a price trusted from the client. If a number about money
appears anywhere else, it came from here.

Nothing in this module touches a database, a session, a clock or a random
number generator. That is not an aesthetic preference: an authority whose answer
depends on when it was asked cannot be re-derived during a dispute, and Phase 6
has to price a *hypothetical* promotion (the preview token of contract §13.2)
without writing anything. Purity is what makes that possible.

The algorithm is the one frozen by PHASE4_DESIGN §6, in this order:

1. ``original_amount = unit_price x quantity`` per line, after merging duplicate
   SKUs (§14.2: an order has at most one line per SKU).
2. apply the promotion to its eligible lines, allocate pro-rata by original
   amount, remainder to the last eligible line;
3. apply the coupon to *its* eligible lines, against what the promotion left;
4. compute shipping (V1: free);
5. ``payable = original - promotion - coupon + shipping``.

Every step's invariants are enforced rather than described: the per-item
allocations are capped by the amount they discount, so no item can end up with a
negative ``payable_amount``, and :meth:`PricingService.build_price_snapshot`
re-adds the items and refuses to emit a snapshot in which INV-006 does not hold.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace
from decimal import Decimal
from typing import Any, TypeVar

from app.modules.pricing.allocation import allocate_pro_rata
from app.modules.pricing.enums import (
    COUPON_WARNINGS,
    MAX_DISCOUNT_BPS,
    PROMOTION_WARNINGS,
    CouponType,
    PricingWarning,
    PromotionType,
)
from app.modules.pricing.errors import PricingInvariantError
from app.modules.pricing.shipping import DEFAULT_SHIPPING_POLICY, ShippingPolicy
from app.modules.pricing.value_objects import (
    CartPrice,
    CouponRule,
    DiscountAllocation,
    ItemPrice,
    PricedLine,
    PriceSnapshot,
    PromotionRule,
)

__all__ = ["PricingService"]

#: The fields of a line that are snapshots rather than arithmetic inputs. Two
#: client lines for the same SKU that disagree on any of these are not the same
#: line, however much the SKU id says they are.
_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "product_id",
    "product_name",
    "sku_name",
    "image_object_key",
    "image_url",
    "sku_snapshot",
    "unit_price",
)

# Warnings are carried as plain ``str`` values on the frozen ``CartPrice``, so
# the per-rule families are compared as values. Comparing member-to-string by
# accident of ``StrEnum`` would work today and become subtle the moment a
# warning's name and value diverge.
_PROMOTION_WARNING_VALUES: frozenset[str] = frozenset(w.value for w in PROMOTION_WARNINGS)
_COUPON_WARNING_VALUES: frozenset[str] = frozenset(w.value for w in COUPON_WARNINGS)

_EnumT = TypeVar("_EnumT", bound=PromotionType | CouponType)


# ---------------------------------------------------------------------------
# Input coercion / validation
#
# Every one of these rejects rather than repairs. A quietly repaired input is a
# wrong price that nobody will find, and the database would then reject it as a
# CHECK violation - at the end of a write transaction, which is the worst place
# to discover a programming error.
# ---------------------------------------------------------------------------
def _coerce_minor(value: Any, field_name: str) -> int:
    """Coerce money to integer minor units (mirrors ``MoneyMinor``'s contract).

    ``float`` is refused outright: spec §19 forbids it for transactional money
    because a total computed from floats is a total that fails
    ``sum(items) == order``. ``Decimal`` is accepted only when it is an exact
    whole number of minor units, so the caller has to be explicit about the unit.
    """
    if isinstance(value, bool):
        raise PricingInvariantError(f"{field_name} must be minor units, got a bool")
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise PricingInvariantError(
                f"{field_name} must be a whole number of minor units",
                context={"field": field_name, "value": str(value)},
            )
        return int(value)
    raise PricingInvariantError(
        f"{field_name} must be integer minor units, got {type(value).__name__}",
        context={"field": field_name},
    )


def _coerce_bps(value: Any, field_name: str) -> int:
    """Coerce a rate to integer basis points, 0..10000 (spec §19)."""
    amount = _coerce_minor(value, field_name)
    if not 0 <= amount <= MAX_DISCOUNT_BPS:
        raise PricingInvariantError(
            f"{field_name} must be between 0 and {MAX_DISCOUNT_BPS} basis points",
            context={"field": field_name, "value": amount},
        )
    return amount


def _require_int(value: Any, field_name: str) -> int:
    """Coerce an identifier/count to ``int``; bools are not integers here."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise PricingInvariantError(
            f"{field_name} must be an integer, got {type(value).__name__}",
            context={"field": field_name},
        )
    return value


def _coerce_amount(value: Any, field_name: str) -> int:
    """Coerce a rule's money field: integer minor units, never negative.

    A negative discount is not a discount, it is a surcharge wearing the same
    field name. The algorithm would eventually reject it - ``allocate_pro_rata``
    refuses a negative total - but catching it here names the offending rule
    field instead of reporting a failed allocation several frames later.
    """
    amount = _coerce_minor(value, field_name)
    if amount < 0:
        raise PricingInvariantError(
            f"{field_name} must not be negative", context={"field": field_name, "value": amount}
        )
    return amount


def _coerce_enum(value: Any, enum_type: type[_EnumT], field_name: str) -> _EnumT:
    """Accept the enum or its wire string; reject anything else loudly.

    Phase 6 builds these rules from a JSON ``rule_config``, where the
    discriminator arrives as a string. Accepting it here is one conversion in
    one place; accepting an *unknown* string would be a rule that silently never
    applies.
    """
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as exc:
            raise PricingInvariantError(
                f"{field_name} is not a known value", context={"field": field_name, "value": value}
            ) from exc
    raise PricingInvariantError(
        f"{field_name} must be a {enum_type.__name__}, got {type(value).__name__}",
        context={"field": field_name},
    )


def _normalise_line(line: PricedLine) -> PricedLine:
    """Validate one cart line and put its money on integer minor units."""
    if not isinstance(line, PricedLine):
        raise PricingInvariantError(
            "cart lines must be PricedLine instances", context={"got": type(line).__name__}
        )
    sku_id = _require_int(line.sku_id, "sku_id")
    product_id = _require_int(line.product_id, "product_id")
    quantity = _require_int(line.quantity, "quantity")
    unit_price = _coerce_minor(line.unit_price, "unit_price")

    if quantity <= 0:
        raise PricingInvariantError(
            "cart line quantity must be positive (contract §14.2)", context={"sku_id": sku_id}
        )
    if unit_price < 0:
        raise PricingInvariantError("cart line unit price must not be negative", context={"sku_id": sku_id})

    already_exact = (
        isinstance(line.unit_price, int)
        and not isinstance(line.unit_price, bool)
        and quantity == line.quantity
        and sku_id == line.sku_id
        and product_id == line.product_id
    )
    if already_exact:
        return line
    return replace(line, sku_id=sku_id, product_id=product_id, unit_price=unit_price, quantity=quantity)


def _merge_lines(lines: Iterable[PricedLine]) -> tuple[PricedLine, ...]:
    """Merge duplicate SKUs by summing quantities, keeping first-appearance order.

    Contract §14.2: "Duplicate ``sku_id`` lines are merged (quantities summed)
    before pricing, so one order has at most one line per SKU" - which is also
    what ``order_items`` UNIQUE ``(order_id, sku_id)`` requires. Two lines for
    the same SKU that disagree about the product *are* a disagreement worth
    raising, not a merge order to pick arbitrarily: silently keeping either
    snapshot would put the wrong name in a historical order (INV-014).
    """
    merged: dict[int, PricedLine] = {}
    for raw in lines:
        line = _normalise_line(raw)
        existing = merged.get(line.sku_id)
        if existing is None:
            merged[line.sku_id] = line
            continue
        for field_name in _SNAPSHOT_FIELDS:
            if getattr(existing, field_name) != getattr(line, field_name):
                raise PricingInvariantError(
                    "duplicate sku lines carry different snapshots",
                    context={"sku_id": line.sku_id, "field": field_name},
                )
        merged[line.sku_id] = replace(existing, quantity=existing.quantity + line.quantity)
    return tuple(merged.values())


def _allocation_map(
    allocations: Sequence[DiscountAllocation], items: Sequence[ItemPrice], label: str
) -> dict[int, int]:
    """Turn an allocation tuple into ``{sku_id: amount}``, checking coverage.

    Coverage is checked in *both* directions, because the two failures are the
    same failure seen from either end. A missing entry means an item contributes
    nothing to a total that includes it; an extra entry means an amount
    contributes to the order total while contributing to no line. Both are
    INV-006 failing quietly, and both are cheaper to refuse here than to explain
    after a reconciliation has already disagreed.
    """
    mapping: dict[int, int] = {}
    item_skus = {item.line.sku_id for item in items}
    for allocation in allocations:
        sku_id = _require_int(allocation.sku_id, f"{label} allocation sku_id")
        amount = _coerce_minor(allocation.amount, f"{label} allocation amount")
        if amount < 0:
            raise PricingInvariantError(
                f"{label} allocation must not be negative", context={"sku_id": sku_id}
            )
        if sku_id not in item_skus:
            raise PricingInvariantError(
                f"{label} allocation refers to a line that is not in the cart",
                context={"sku_id": sku_id},
            )
        if sku_id in mapping:
            raise PricingInvariantError(f"{label} allocation is recorded twice", context={"sku_id": sku_id})
        mapping[sku_id] = amount
    for item in items:
        if item.line.sku_id not in mapping:
            raise PricingInvariantError(
                f"no {label} allocation recorded for a cart line",
                context={"sku_id": item.line.sku_id},
            )
    return mapping


def _zero_allocations(items: Sequence[ItemPrice]) -> tuple[DiscountAllocation, ...]:
    return tuple(DiscountAllocation(sku_id=item.line.sku_id, amount=0) for item in items)


def _normalise_promotion(rule: PromotionRule) -> PromotionRule:
    """Validate a resolved promotion rule before it prices anything."""
    if not isinstance(rule, PromotionRule):
        raise PricingInvariantError("promotion must be a PromotionRule", context={"got": type(rule).__name__})
    return replace(
        rule,
        promotion_id=_require_int(rule.promotion_id, "promotion_id"),
        promotion_type=_coerce_enum(rule.promotion_type, PromotionType, "promotion_type"),
        threshold_amount=_coerce_amount(rule.threshold_amount, "threshold_amount"),
        discount_amount=_coerce_amount(rule.discount_amount, "discount_amount"),
        discount_bps=_coerce_bps(rule.discount_bps, "discount_bps"),
        reduction_amount=_coerce_amount(rule.reduction_amount, "reduction_amount"),
        max_discount_amount=(
            None
            if rule.max_discount_amount is None
            else _coerce_amount(rule.max_discount_amount, "max_discount_amount")
        ),
    )


def _normalise_coupon(rule: CouponRule) -> CouponRule:
    """Validate a resolved coupon rule before it prices anything."""
    if not isinstance(rule, CouponRule):
        raise PricingInvariantError("coupon must be a CouponRule", context={"got": type(rule).__name__})
    return replace(
        rule,
        coupon_id=_require_int(rule.coupon_id, "coupon_id"),
        coupon_type=_coerce_enum(rule.coupon_type, CouponType, "coupon_type"),
        threshold_amount=_coerce_amount(rule.threshold_amount, "threshold_amount"),
        face_value_amount=_coerce_amount(rule.face_value_amount, "face_value_amount"),
        discount_bps=_coerce_bps(rule.discount_bps, "discount_bps"),
        max_discount_amount=(
            None
            if rule.max_discount_amount is None
            else _coerce_amount(rule.max_discount_amount, "max_discount_amount")
        ),
    )


class PricingService:
    """The single price authority. Stateless; every method is pure.

    Stateless means it can be constructed per request, shared between
    requests, or injected into the workflow - all equivalent, which is why the
    workflow's ``pricing: PricingService | None`` parameter can default to a
    fresh instance without a lifetime question.
    """

    __slots__ = ()

    # -- the six frozen methods (§6) ----------------------------------------
    def calculate_item_price(self, line: PricedLine) -> ItemPrice:
        """Price one line: ``unit_price x quantity``, nothing else.

        Deliberately not "the price of this SKU" - the caller already resolved
        the SKU and its price, and this authority's job is the arithmetic and
        the snapshot, not a second trip to the catalog.
        """
        normalised = _normalise_line(line)
        return ItemPrice(line=normalised, original_amount=normalised.unit_price * normalised.quantity)

    def calculate_cart_price(
        self,
        lines: Iterable[PricedLine],
        *,
        promotion: PromotionRule | None = None,
        coupon: CouponRule | None = None,
        shipping_policy: ShippingPolicy | None = None,
    ) -> CartPrice:
        """Price a whole cart - the entry point both preview and create use.

        The create path calls this again rather than trusting the preview
        response (contract §14.2: "one pricing authority, so the create path
        recomputes the same numbers"). Trusting the preview would make the price
        a client input, which §38 forbids.

        An empty cart prices to zero rather than raising: "the cart is empty" is
        a business condition with its own code (``CART_EMPTY 50001``) owned by the
        order layer, and a pricing authority that raised a business error would
        be reaching outside its contract.
        """
        items = tuple(self.calculate_item_price(line) for line in _merge_lines(lines))
        original_amount = sum(item.original_amount for item in items)
        cart = CartPrice(
            items=items,
            original_amount=original_amount,
            promotion_discount_amount=0,
            coupon_discount_amount=0,
            shipping_amount=0,
            payable_amount=original_amount,
            promotion_allocations=_zero_allocations(items),
            coupon_allocations=_zero_allocations(items),
            warnings=(),
        )

        if promotion is not None:
            cart = self.apply_promotion(cart, promotion)
        if coupon is not None:
            cart = self.apply_coupon(cart, coupon)

        shipping_amount = self.calculate_shipping(cart, shipping_policy)
        if shipping_amount:
            cart = self._repriced(cart, shipping_amount=shipping_amount)
        return cart

    def apply_promotion(self, cart: CartPrice, promotion: PromotionRule) -> CartPrice:
        """Apply one promotion, replacing any promotion already on the cart.

        Replacing rather than stacking is the Phase 4 contract: §13.1 gives a
        promotion a ``stackable`` flag and stacking is Phase 6's decision. A
        caller that wants two promotions has to combine the *rules* (one
        resolved rule), because two order-level discounts allocated
        independently would each be capped by the full original amount and the
        cart could be discounted twice for the same money.

        A coupon already on the cart is carried forward, because the coupon's
        own allocation was computed against the previous promotion and there is
        no coupon rule here to recompute it against. If the new promotion leaves
        less room than that coupon has already taken, this raises rather than
        producing a negative item payable; the correct sequence is
        promotion-then-coupon, which is what :meth:`calculate_cart_price` does.
        """
        rule = _normalise_promotion(promotion)
        eligible = [rule.covers(item.line.sku_id) for item in cart.items]
        eligible_original = sum(
            item.original_amount
            for item, is_eligible in zip(cart.items, eligible, strict=True)
            if is_eligible
        )

        kept = tuple(w for w in cart.warnings if w not in _PROMOTION_WARNING_VALUES)
        warnings: tuple[str, ...] = kept
        if not any(eligible) or eligible_original == 0:
            discount = 0
            warnings = (*kept, PricingWarning.PROMOTION_SCOPE_NO_MATCH.value)
        elif (
            rule.promotion_type is PromotionType.FULL_REDUCTION and eligible_original < rule.threshold_amount
        ):
            discount = 0
            warnings = (*kept, PricingWarning.PROMOTION_THRESHOLD_NOT_MET.value)
        else:
            discount = min(self._promotion_amount(rule, cart, eligible, eligible_original), eligible_original)

        weights = [
            item.original_amount if is_eligible else 0
            for item, is_eligible in zip(cart.items, eligible, strict=True)
        ]
        shares = allocate_pro_rata(discount, weights)
        allocations = tuple(
            DiscountAllocation(sku_id=item.line.sku_id, amount=amount)
            for item, amount in zip(cart.items, shares, strict=True)
        )
        return self._repriced(
            cart,
            promotion_discount_amount=discount,
            promotion_allocations=allocations,
            warnings=warnings,
        )

    def apply_coupon(self, cart: CartPrice, coupon: CouponRule) -> CartPrice:
        """Apply one coupon, replacing any coupon already on the cart.

        The coupon is computed against what the promotion left on its eligible
        lines (``eligible original - promotion allocated to eligible``), which is
        what makes a 50%-off coupon plus a fixed promotion add up instead of
        overdrafting the line. Its allocation basis is each line's *remaining*
        amount by the same token, so no line can be discounted below zero.
        """
        rule = _normalise_coupon(coupon)
        eligible = [rule.covers(item.line.sku_id) for item in cart.items]
        promotion_map = _allocation_map(cart.promotion_allocations, cart.items, "promotion")

        eligible_original = 0
        remaining_total = 0
        weights: list[int] = []
        for item, is_eligible in zip(cart.items, eligible, strict=True):
            if not is_eligible:
                weights.append(0)
                continue
            remaining = item.original_amount - promotion_map[item.line.sku_id]
            if remaining < 0:
                raise PricingInvariantError(
                    "promotion allocation exceeds the line it was allocated to",
                    context={"sku_id": item.line.sku_id},
                )
            eligible_original += item.original_amount
            remaining_total += remaining
            weights.append(remaining)

        kept = tuple(w for w in cart.warnings if w not in _COUPON_WARNING_VALUES)
        warnings: tuple[str, ...] = kept
        if not any(eligible) or remaining_total == 0:
            # `remaining_total == 0` means the promotion already took the whole
            # eligible amount. `COUPON_SCOPE_NO_MATCH` reads as "nothing on this
            # cart for this coupon to apply to", which is true in both branches.
            discount = 0
            warnings = (*kept, PricingWarning.COUPON_SCOPE_NO_MATCH.value)
        elif eligible_original < rule.threshold_amount:
            # §6: the threshold is measured on the eligible *original* amount, so
            # a promotion cannot push an order back below a coupon's threshold.
            discount = 0
            warnings = (*kept, PricingWarning.COUPON_THRESHOLD_NOT_MET.value)
        else:
            discount = min(self._coupon_amount(rule, remaining_total), remaining_total)

        shares = allocate_pro_rata(discount, weights)
        allocations = tuple(
            DiscountAllocation(sku_id=item.line.sku_id, amount=amount)
            for item, amount in zip(cart.items, shares, strict=True)
        )
        return self._repriced(
            cart,
            coupon_discount_amount=discount,
            coupon_allocations=allocations,
            warnings=warnings,
        )

    def calculate_shipping(self, cart: CartPrice, policy: ShippingPolicy | None = None) -> int:
        """Ask the policy what shipping costs; ``None`` means the V1 free policy.

        A negative answer is an invariant violation here; a positive answer is
        legitimate here and refused in :meth:`build_price_snapshot`, because it
        is the *snapshot* that must satisfy INV-006 and the shipping method is
        allowed to tell the truth about a policy that the order path cannot yet
        absorb (contract §14.4).
        """
        resolved = DEFAULT_SHIPPING_POLICY if policy is None else policy
        amount = _coerce_minor(resolved.calculate(cart), "shipping amount")
        if amount < 0:
            raise PricingInvariantError(
                "shipping policy returned a negative amount", context={"policy": resolved.name}
            )
        return amount

    def build_price_snapshot(
        self,
        cart: CartPrice,
        *,
        promotion_ids: Sequence[int] = (),
        coupon_ids: Sequence[int] = (),
        shipping_policy: str = "FREE",
    ) -> PriceSnapshot:
        """Freeze a priced cart into the snapshot the order persists.

        This is where INV-006 stops being a claim and becomes a check: the item
        payables are re-added and compared with the order payable, and the call
        refuses to return a snapshot in which they differ. It is also where a
        non-zero shipping charge is refused, because shipping has no per-item
        home in ``order_items`` and including it would break that equality *by
        construction* (contract §14.4) - so paid shipping needs a decision about
        allocation, not a config change.
        """
        if cart.shipping_amount != 0:
            raise PricingInvariantError(
                "a non-zero shipping_amount has no per-item home and would break INV-006",
                context={"shipping_amount": cart.shipping_amount},
            )

        item_original = sum(item.original_amount for item in cart.items)
        if item_original != cart.original_amount:
            raise PricingInvariantError(
                "cart.original_amount is not the sum of its item amounts",
                context={"original_amount": cart.original_amount, "item_total": item_original},
            )

        promotion_map = _allocation_map(cart.promotion_allocations, cart.items, "promotion")
        coupon_map = _allocation_map(cart.coupon_allocations, cart.items, "coupon")

        if sum(promotion_map.values()) != cart.promotion_discount_amount:
            raise PricingInvariantError(
                "promotion allocations do not sum to the order promotion discount",
                context={
                    "allocated": sum(promotion_map.values()),
                    "order": cart.promotion_discount_amount,
                },
            )
        if sum(coupon_map.values()) != cart.coupon_discount_amount:
            raise PricingInvariantError(
                "coupon allocations do not sum to the order coupon discount",
                context={"allocated": sum(coupon_map.values()), "order": cart.coupon_discount_amount},
            )

        item_payable_total = 0
        for item in cart.items:
            sku_id = item.line.sku_id
            payable = item.original_amount - promotion_map[sku_id] - coupon_map[sku_id]
            if payable < 0:
                raise PricingInvariantError(
                    "item payable_amount would be negative (PHASE4_DESIGN §11)",
                    context={"sku_id": sku_id, "payable": payable},
                )
            item_payable_total += payable

        if item_payable_total != cart.payable_amount:
            raise PricingInvariantError(
                "INV-006 violated: sum(item.payable_amount) != order.payable_amount",
                context={"item_total": item_payable_total, "order_total": cart.payable_amount},
            )

        return PriceSnapshot(
            items=cart.items,
            original_amount=cart.original_amount,
            promotion_discount_amount=cart.promotion_discount_amount,
            coupon_discount_amount=cart.coupon_discount_amount,
            shipping_amount=cart.shipping_amount,
            payable_amount=cart.payable_amount,
            promotion_allocations=cart.promotion_allocations,
            coupon_allocations=cart.coupon_allocations,
            promotion_ids=tuple(_require_int(promotion_id, "promotion_id") for promotion_id in promotion_ids),
            coupon_ids=tuple(_require_int(coupon_id, "coupon_id") for coupon_id in coupon_ids),
            shipping_policy=shipping_policy,
        )

    # -- internals -----------------------------------------------------------
    def _promotion_amount(
        self,
        rule: PromotionRule,
        cart: CartPrice,
        eligible: Sequence[bool],
        eligible_original: int,
    ) -> int:
        """The promotion's discount *before* the eligible-original cap.

        Only the fields the rule's own type defines are read (contract §13.1
        discriminates ``rule_config`` by ``promotion_type``), so
        ``max_discount_amount`` does not cap a ``DIRECT_DISCOUNT``: that variant
        has no such field, and honouring an undefined one would be this module
        inventing contract.
        """
        if rule.promotion_type is PromotionType.DIRECT_DISCOUNT:
            eligible_units = sum(
                item.line.quantity
                for item, is_eligible in zip(cart.items, eligible, strict=True)
                if is_eligible
            )
            return rule.discount_amount * eligible_units

        if rule.promotion_type is PromotionType.PERCENT_DISCOUNT:
            amount = eligible_original * rule.discount_bps // MAX_DISCOUNT_BPS
            if rule.max_discount_amount is not None:
                amount = min(amount, rule.max_discount_amount)
            return amount

        # FULL_REDUCTION. The threshold check already happened in the caller, so
        # reaching here means the reduction applies.
        amount = rule.reduction_amount
        if rule.max_discount_amount is not None:
            amount = min(amount, rule.max_discount_amount)
        return amount

    def _coupon_amount(self, rule: CouponRule, remaining_total: int) -> int:
        """The coupon's discount before the remaining-amount cap."""
        if rule.coupon_type is CouponType.FIXED_AMOUNT:
            return rule.face_value_amount
        amount = remaining_total * rule.discount_bps // MAX_DISCOUNT_BPS
        if rule.max_discount_amount is not None:
            amount = min(amount, rule.max_discount_amount)
        return amount

    def _repriced(
        self,
        cart: CartPrice,
        *,
        promotion_discount_amount: int | None = None,
        promotion_allocations: tuple[DiscountAllocation, ...] | None = None,
        coupon_discount_amount: int | None = None,
        coupon_allocations: tuple[DiscountAllocation, ...] | None = None,
        shipping_amount: int | None = None,
        warnings: tuple[str, ...] | None = None,
    ) -> CartPrice:
        """Rebuild a cart with new components and a recomputed payable.

        ``payable_amount`` is *always* derived here and never passed in, so the
        order-level figure cannot drift from the formula in §14.4 - and the
        per-item check below means it cannot drift into a negative item either.
        """
        promo_total = (
            cart.promotion_discount_amount if promotion_discount_amount is None else promotion_discount_amount
        )
        promo_allocations = (
            cart.promotion_allocations if promotion_allocations is None else promotion_allocations
        )
        coupon_total = (
            cart.coupon_discount_amount if coupon_discount_amount is None else coupon_discount_amount
        )
        coupon_allocations_ = cart.coupon_allocations if coupon_allocations is None else coupon_allocations
        shipping = cart.shipping_amount if shipping_amount is None else shipping_amount

        promotion_map = _allocation_map(promo_allocations, cart.items, "promotion")
        coupon_map = _allocation_map(coupon_allocations_, cart.items, "coupon")
        for item in cart.items:
            sku_id = item.line.sku_id
            payable = item.original_amount - promotion_map[sku_id] - coupon_map[sku_id]
            if payable < 0:
                raise PricingInvariantError(
                    "item payable_amount would be negative (PHASE4_DESIGN §11)",
                    context={"sku_id": sku_id, "payable": payable},
                )

        return replace(
            cart,
            promotion_discount_amount=promo_total,
            promotion_allocations=promo_allocations,
            coupon_discount_amount=coupon_total,
            coupon_allocations=coupon_allocations_,
            shipping_amount=shipping,
            payable_amount=cart.original_amount - promo_total - coupon_total + shipping,
            warnings=cart.warnings if warnings is None else warnings,
        )
