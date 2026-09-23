"""Pricing context: the single price authority (spec §37).

Spec sections 37/39/41 and PHASE4_DESIGN §5-§6. Owns the discount arithmetic and
the exact allocation that INV-006 rests on. It is deliberately DB-free and
imports no other module's models: Phase 6's marketing module builds the resolved
rule objects this context consumes, and the order context consumes the
snapshot it produces.

The public surface is re-exported here so a consumer can write
``from app.modules.pricing import PricingService, PricedLine`` without caring
which file inside the package holds them.
"""

from __future__ import annotations

from app.modules.pricing.allocation import allocate_pro_rata
from app.modules.pricing.enums import (
    COUPON_TYPES,
    MAX_DISCOUNT_BPS,
    PRICING_WARNINGS,
    PROMOTION_TYPES,
    CouponType,
    PricingWarning,
    PromotionType,
)
from app.modules.pricing.errors import PricingInvariantError
from app.modules.pricing.service import PricingService
from app.modules.pricing.shipping import (
    DEFAULT_SHIPPING_POLICY,
    FreeShippingPolicy,
    ShippingPolicy,
)
from app.modules.pricing.value_objects import (
    CartPrice,
    CouponRule,
    DiscountAllocation,
    ItemPrice,
    PricedLine,
    PriceSnapshot,
    PromotionRule,
)

__all__ = [
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
]
