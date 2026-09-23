"""Shipping policy for the pricing module.

Spec references:
    §37   ``PricingService.calculate_shipping`` is frozen as a method, so it
          must exist even though V1 has no shipping charge
    §14.4 ``shipping_amount`` is 0 in V1, and a non-zero charge has no per-item
          home, so including it in ``payable_amount`` would break INV-006 by
          construction

A policy is an object rather than a config flag because "what does shipping
cost" is a question the pricing authority asks and does not answer: the answer
may later depend on address, weight, order value or a carrier contract. The
protocol is the seam, and it takes the whole priced cart because any of those
inputs may matter.

What a policy may **not** do is quietly break the invariant: a non-zero result
is returned truthfully by :meth:`ShippingPolicy.calculate` and then refused by
:meth:`PricingService.build_price_snapshot`, loudly, with INV-006 named in the
message. Switching on paid shipping therefore requires a decision about
allocation (or about the invariant) rather than a settings edit - which is
exactly what API_CONTRACT §14.4 demands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.modules.pricing.value_objects import CartPrice

__all__ = ["DEFAULT_SHIPPING_POLICY", "FreeShippingPolicy", "ShippingPolicy"]


@runtime_checkable
class ShippingPolicy(Protocol):
    """Something that can price shipping for a priced cart.

    ``name`` is the label persisted on the order (``PriceSnapshot
    .shipping_policy``). It is part of the protocol because an order that
    cannot say *which* policy priced it is an order nobody can re-derive.

    It is declared as a read-only property rather than as a plain attribute on
    purpose: a policy is a *value*, so an immutable one (a frozen dataclass, a
    module-level constant) has to satisfy the protocol. A mutable attribute
    declaration would have excluded exactly the implementations this module
    ships.
    """

    @property
    def name(self) -> str:
        """The label written to ``PriceSnapshot.shipping_policy``."""
        ...

    def calculate(self, cart: CartPrice) -> int:
        """Return the shipping charge in minor units; must be non-negative."""
        ...


@dataclass(frozen=True, slots=True)
class FreeShippingPolicy:
    """The V1 policy: shipping is free (spec §14.4).

    Frozen and stateless, so it can be shared by every request without any
    lifetime question. The cart argument is accepted and deliberately unread -
    the signature is the seam, and a free policy that also specialised on cart
    contents would be a paid policy wearing the wrong name.
    """

    name: str = "FREE"

    def calculate(self, cart: CartPrice) -> int:  # noqa: ARG002 - the seam, not a used input
        return 0


#: The single instance every caller may share.
DEFAULT_SHIPPING_POLICY = FreeShippingPolicy()
