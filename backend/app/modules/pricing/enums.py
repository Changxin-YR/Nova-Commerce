"""Pricing domain vocabularies.

Spec references:
    §37   the pricing module is the single price authority
    §39   the three promotion types
    §41   one discount-allocation algorithm with an exact remainder rule
    §13.1 promotion ``rule_config`` is *discriminated* by ``promotion_type``
    §13.3 coupon ``coupon_type`` is ``FIXED_AMOUNT`` | ``PERCENT_DISCOUNT``
    PHASE4_DESIGN §5/§6

Why the marketing vocabularies live in the pricing module rather than in a
marketing module: Phase 4 must implement the discount *arithmetic* against the
resolved rule shapes of API_CONTRACT §13.1/§13.3, and PHASE4_DESIGN §5 forbids
importing marketing models to do it. The vocabulary is the frozen part; the
persistence is Phase 6's.
"""

from __future__ import annotations

from enum import StrEnum


class PromotionType(StrEnum):
    """How a promotion computes its discount (spec §39, contract §13.1).

    The value is the wire string: the client switches on it, and
    ``rule_config`` is *discriminated* by it, so the field set that is legal
    differs per variant:

    * ``DIRECT_DISCOUNT``  -> ``discount_amount`` (minor units, **per unit**)
    * ``PERCENT_DISCOUNT`` -> ``discount_bps``, ``max_discount_amount``
    * ``FULL_REDUCTION``   -> ``threshold_amount``, ``reduction_amount``,
                              ``max_discount_amount``
    """

    DIRECT_DISCOUNT = "DIRECT_DISCOUNT"
    PERCENT_DISCOUNT = "PERCENT_DISCOUNT"
    FULL_REDUCTION = "FULL_REDUCTION"


class CouponType(StrEnum):
    """How a coupon computes its discount (contract §13.3).

    Like the promotion types, the pair is a discriminator rather than a
    nullable pair of fields the client has to guess about.
    """

    FIXED_AMOUNT = "FIXED_AMOUNT"
    PERCENT_DISCOUNT = "PERCENT_DISCOUNT"


class PricingWarning(StrEnum):
    """Non-fatal reasons a supplied rule produced less than it promised.

    These travel to the client in the preview response's ``warnings`` array
    (contract §14.2). They are **codes, not sentences**: the console renders
    the localised text, which is why the frozen wire field is a list of
    strings rather than a list of objects.

    A rule that is simply absent produces no warning - absence is not an
    event. A rule that was supplied and did nothing is, because the customer
    selected it and deserves to be told why it did not apply.
    """

    PROMOTION_SCOPE_NO_MATCH = "PROMOTION_SCOPE_NO_MATCH"
    PROMOTION_THRESHOLD_NOT_MET = "PROMOTION_THRESHOLD_NOT_MET"
    COUPON_SCOPE_NO_MATCH = "COUPON_SCOPE_NO_MATCH"
    COUPON_THRESHOLD_NOT_MET = "COUPON_THRESHOLD_NOT_MET"


#: Frozen vocabularies, as tuples, for CHECK constraints and API validation.
#: A tuple rather than a set so the order is stable in generated documentation
#: and in error messages.
PROMOTION_TYPES: tuple[PromotionType, ...] = (
    PromotionType.DIRECT_DISCOUNT,
    PromotionType.PERCENT_DISCOUNT,
    PromotionType.FULL_REDUCTION,
)

COUPON_TYPES: tuple[CouponType, ...] = (
    CouponType.FIXED_AMOUNT,
    CouponType.PERCENT_DISCOUNT,
)

#: Warning codes the pricing service can emit in Phase 4.
PRICING_WARNINGS: tuple[PricingWarning, ...] = (
    PricingWarning.PROMOTION_SCOPE_NO_MATCH,
    PricingWarning.PROMOTION_THRESHOLD_NOT_MET,
    PricingWarning.COUPON_SCOPE_NO_MATCH,
    PricingWarning.COUPON_THRESHOLD_NOT_MET,
)

#: Promotions and coupons never emit warnings about a rule that is not there.
PROMOTION_WARNINGS: frozenset[PricingWarning] = frozenset(
    {PricingWarning.PROMOTION_SCOPE_NO_MATCH, PricingWarning.PROMOTION_THRESHOLD_NOT_MET}
)
COUPON_WARNINGS: frozenset[PricingWarning] = frozenset(
    {PricingWarning.COUPON_SCOPE_NO_MATCH, PricingWarning.COUPON_THRESHOLD_NOT_MET}
)

#: 100% expressed in basis points. A rate is never a float (spec §19): 12.5% is
#: exactly 1250 bps, while ``0.125`` is not exactly representable, and a discount
#: computed from a float is a ledger that does not add up.
MAX_DISCOUNT_BPS = 10_000

__all__ = [
    "COUPON_TYPES",
    "COUPON_WARNINGS",
    "MAX_DISCOUNT_BPS",
    "PRICING_WARNINGS",
    "PROMOTION_TYPES",
    "PROMOTION_WARNINGS",
    "CouponType",
    "PricingWarning",
    "PromotionType",
]
