"""Pricing's internal error type.

Spec references:
    §95    every ``/api/v1`` response carries a *stable business code*
    §109   security-relevant failures must not leak internals
    PHASE4_DESIGN §2  ``PricingInvariantError`` is internal and never a wire code

Every condition that raises this is a bug or a corrupt rule row, never a user
mistake: the user-facing mistakes (empty cart, bad SKU, coupon not found) are
business errors with codes 40xxx/50xxx/90xxx raised by the layers that own
them. So this class deliberately does **not** derive from
``app.core.errors.AppError``: making it an ``AppError`` would force someone to
invent a business code for an internal bug, and the code would then become part
of the public contract forever (§95: a code may be deprecated but never reused).

It therefore surfaces as ``INTERNAL_ERROR (10000)`` through the generic
handler, which leaks exactly the right amount of information: none.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class PricingInvariantError(Exception):
    """A pricing precondition or invariant was violated.

    Two families of condition raise this:

    1. **Preconditions** - a :class:`~app.modules.pricing.value_objects.PricedLine`
       with ``quantity <= 0`` or a negative price, a non-integer money value, a
       rate above 10000 bps. Garbage in, loud exception out; silently pricing it
       would persist nonsense behind a database CHECK constraint, which turns a
       programming error into a 500 at the worst possible moment.
    2. **Invariants** - INV-006 (``sum(item.payable_amount) ==
       order.payable_amount``) and per-item non-negativity, asserted while
       building the snapshot rather than hoped for. Spec §136 asks for
       executable evidence, not comments.

    ``context`` is for structured logs and audit only; it is never serialised
    to a client.
    """

    def __init__(self, message: str, *, context: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context) if context else {}

    def __str__(self) -> str:
        if not self.context:
            return self.message
        rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return f"{self.message} ({rendered})"


__all__ = ["PricingInvariantError"]
