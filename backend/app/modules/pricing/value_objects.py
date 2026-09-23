"""Pricing value objects (PHASE4_DESIGN §5, frozen).

These are the *code* interface several people implement against in parallel, so
the field lists are copied from the freeze rather than invented here. They are
frozen dataclasses with ``slots``: a priced cart that anything can mutate is a
cart whose total can stop matching its lines, and that mismatch is INV-006.

Two rules that the shapes make structural rather than advisory:

* **Money is integer minor units and rates are integer basis points.** There is
  no ``float`` anywhere in this module, for the reason spec §19 gives: binary
  floating point cannot represent 0.1, and a total computed from floats is a
  total that fails ``sum(items) == order``.
* **``PromotionRule``/``CouponRule`` are the resolved view** of API_CONTRACT
  §13.1/§13.3 ``rule_config``/``scope``/``applicable_scope`` - not the wire
  shapes themselves. Phase 6's marketing module builds them; Phase 4 never
  imports marketing models (§5).

The two aggregate types (:class:`CartPrice`, :class:`PriceSnapshot`) also carry
small *derived* lookups (:meth:`promotion_allocation`, :meth:`coupon_allocation`,
:meth:`allocated_discount`, :meth:`item_payable_amount`). They add no fields and
change no contract; they exist so that the consumer (order preview, order_items
persistence) reads the per-item money from the same place that produced it,
instead of re-deriving an allocation scan next to an involution that must stay
exact.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.pricing.enums import CouponType, PromotionType
from app.modules.pricing.errors import PricingInvariantError

__all__ = [
    "CartPrice",
    "CouponRule",
    "DiscountAllocation",
    "ItemPrice",
    "PriceSnapshot",
    "PricedLine",
    "PromotionRule",
]


@dataclass(frozen=True, slots=True)
class PricedLine:
    """One cart line as the pricing authority receives it.

    Everything except ``quantity`` is a **snapshot of the SKU at this moment**,
    which is what INV-014 needs: the order must be re-readable years later
    without asking the catalog what the product is called today. The pricing
    service copies these fields onto the order rather than re-reading them.
    """

    sku_id: int
    product_id: int
    product_name: str
    sku_name: str
    image_object_key: str | None
    image_url: str | None
    sku_snapshot: dict | None
    unit_price: int
    quantity: int


@dataclass(frozen=True, slots=True)
class ItemPrice:
    """A line plus the only arithmetic that is unambiguous: price x quantity."""

    line: PricedLine
    original_amount: int


@dataclass(frozen=True, slots=True)
class DiscountAllocation:
    """How much of an order-level discount landed on one SKU.

    Keyed by ``sku_id`` rather than by index because an order has at most one
    line per SKU (``order_items`` UNIQUE ``(order_id, sku_id)`` and the
    duplicate merge in §14.2), so the SKU *is* the line identity on the wire and
    in the database.
    """

    sku_id: int
    amount: int


class _PricedItemsView:
    """Shared, derived per-item money lookups for the two priced aggregates.

    A mixin rather than duplicated methods on both dataclasses: the two
    aggregates hold identical allocation fields, and a lookup implemented twice
    is a lookup that will eventually disagree with itself. It declares no
    fields, so the dataclasses below stay exactly as frozen.
    """

    __slots__ = ()

    items: tuple[ItemPrice, ...]
    promotion_allocations: tuple[DiscountAllocation, ...]
    coupon_allocations: tuple[DiscountAllocation, ...]
    payable_amount: int

    def _amount_for(self, allocations: tuple[DiscountAllocation, ...], sku_id: int) -> int:
        for allocation in allocations:
            if allocation.sku_id == sku_id:
                return allocation.amount
        raise PricingInvariantError("no allocation recorded for this sku", context={"sku_id": sku_id})

    def _item_for(self, sku_id: int) -> ItemPrice:
        for item in self.items:
            if item.line.sku_id == sku_id:
                return item
        raise PricingInvariantError("sku is not part of this priced cart", context={"sku_id": sku_id})

    def promotion_allocation(self, sku_id: int) -> int:
        """Promotion discount allocated to ``sku_id`` (0 when out of scope)."""
        return self._amount_for(self.promotion_allocations, sku_id)

    def coupon_allocation(self, sku_id: int) -> int:
        """Coupon discount allocated to ``sku_id`` (0 when out of scope)."""
        return self._amount_for(self.coupon_allocations, sku_id)

    def allocated_discount(self, sku_id: int) -> int:
        """``order_items.allocated_discount_amount`` for one SKU."""
        return self.promotion_allocation(sku_id) + self.coupon_allocation(sku_id)

    def item_payable_amount(self, sku_id: int) -> int:
        """``order_items.payable_amount`` for one SKU.

        ``original - allocated``, never a second opinion about the price: the
        database CHECK ``ck_order_items_payable_consistent`` restates it, and a
        consumer that computed it a third way would be the place the two
        definitions diverge.
        """
        item = self._item_for(sku_id)
        return item.original_amount - self.allocated_discount(sku_id)


@dataclass(frozen=True, slots=True)
class CartPrice(_PricedItemsView):
    """A fully priced cart - the single authority's answer (§37).

    ``promotion_allocations`` and ``coupon_allocations`` carry **one entry per
    item, in cart order**, zero for lines outside a rule's scope. Sparse
    allocations would make "sum of allocations equals the order discount"
    unverifiable by inspection and force every consumer to write a lookup that
    treats missing as zero.
    """

    items: tuple[ItemPrice, ...]
    original_amount: int
    promotion_discount_amount: int
    coupon_discount_amount: int
    shipping_amount: int
    payable_amount: int
    promotion_allocations: tuple[DiscountAllocation, ...]
    coupon_allocations: tuple[DiscountAllocation, ...]
    warnings: tuple[str, ...] = ()

    @property
    def item_count(self) -> int:
        """Total units across lines - ``orders.item_count`` (§11 addendum)."""
        return sum(item.line.quantity for item in self.items)

    @property
    def total_discount_amount(self) -> int:
        return self.promotion_discount_amount + self.coupon_discount_amount


@dataclass(frozen=True, slots=True)
class PromotionRule:
    """The *resolved* view of API_CONTRACT §13.1 ``rule_config`` + scope.

    Only the fields the rule's own :class:`PromotionType` defines are read
    (§13.1 discriminates ``rule_config`` by type):

    * ``DIRECT_DISCOUNT``  reads ``discount_amount`` - minor units **per unit**,
      so a quantity of 3 applies it three times;
    * ``PERCENT_DISCOUNT`` reads ``discount_bps`` and ``max_discount_amount``;
    * ``FULL_REDUCTION``   reads ``threshold_amount``, ``reduction_amount`` and
      ``max_discount_amount``.

    ``max_discount_amount`` is therefore *ignored* for ``DIRECT_DISCOUNT``: §13.1
    does not define it for that variant, and honouring an undefined field would
    be this module inventing contract. The discount is still capped by the
    eligible original total, so it can never exceed what it discounts.
    """

    promotion_id: int
    promotion_type: PromotionType
    threshold_amount: int = 0
    discount_amount: int = 0
    discount_bps: int = 0
    reduction_amount: int = 0
    max_discount_amount: int | None = None
    applicable_sku_ids: frozenset[int] | None = None

    def __post_init__(self) -> None:
        self._normalise_scope("applicable_sku_ids")

    def _normalise_scope(self, attribute: str) -> None:
        """Coerce a plain set/list to ``frozenset``.

        A frozen dataclass hashes its fields, so a ``set`` here would make the
        rule unhashable and its equality order-insensitive-but-fragile. Phase 6
        builds these from JSON ``scope`` arrays, which arrive as lists; doing the
        conversion once, at construction, keeps that from being a runtime
        surprise in the allocation path.
        """
        value = getattr(self, attribute)
        if value is None or isinstance(value, frozenset):
            return
        if isinstance(value, (set, list, tuple)):
            object.__setattr__(self, attribute, frozenset(value))
            return
        raise PricingInvariantError(
            f"{attribute} must be a frozenset of sku ids or None",
            context={"promotion_id": self.promotion_id},
        )

    def covers(self, sku_id: int) -> bool:
        """Whether this rule's scope includes ``sku_id`` (``None`` means all)."""
        return self.applicable_sku_ids is None or sku_id in self.applicable_sku_ids


@dataclass(frozen=True, slots=True)
class CouponRule:
    """The *resolved* view of API_CONTRACT §13.3 ``applicable_scope``.

    ``FIXED_AMOUNT`` reads ``face_value_amount``; ``PERCENT_DISCOUNT`` reads
    ``discount_bps`` and ``max_discount_amount``. ``threshold_amount`` is a
    property of the coupon itself in §13.3 and applies to **both** variants: a
    coupon whose threshold is not met is not applicable, whatever its type.
    """

    coupon_id: int
    coupon_type: CouponType
    threshold_amount: int = 0
    face_value_amount: int = 0
    discount_bps: int = 0
    max_discount_amount: int | None = None
    applicable_sku_ids: frozenset[int] | None = None

    def __post_init__(self) -> None:
        value = self.applicable_sku_ids
        if value is None or isinstance(value, frozenset):
            return
        if isinstance(value, (set, list, tuple)):
            object.__setattr__(self, "applicable_sku_ids", frozenset(value))
            return
        raise PricingInvariantError(
            "applicable_sku_ids must be a frozenset of sku ids or None",
            context={"coupon_id": self.coupon_id},
        )

    def covers(self, sku_id: int) -> bool:
        """Whether this coupon's scope includes ``sku_id`` (``None`` means all)."""
        return self.applicable_sku_ids is None or sku_id in self.applicable_sku_ids


@dataclass(frozen=True, slots=True)
class PriceSnapshot(_PricedItemsView):
    """What ``build_price_snapshot`` returns and the workflow persists.

    This is the object INV-006 is proven against and INV-014 is built on: the
    ``items`` here are snapshots, so what gets written into ``order_items`` is
    the price that was agreed, not a live read of the catalog.

    It carries the rule ids that produced it (``promotion_ids``/``coupon_ids``)
    and the shipping policy label, because an order that cannot say which rules
    priced it cannot be re-derived during a dispute.
    """

    items: tuple[ItemPrice, ...]
    original_amount: int
    promotion_discount_amount: int
    coupon_discount_amount: int
    shipping_amount: int
    payable_amount: int
    promotion_allocations: tuple[DiscountAllocation, ...]
    coupon_allocations: tuple[DiscountAllocation, ...]
    promotion_ids: tuple[int, ...]
    coupon_ids: tuple[int, ...]
    shipping_policy: str

    @property
    def item_count(self) -> int:
        return sum(item.line.quantity for item in self.items)
