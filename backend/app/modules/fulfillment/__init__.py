"""Fulfillment domain - packages, shipping, and the order's goods-out axis.

PHASE5_DESIGN sections 3, 4.2, 5.4, 6.3. Import surface only; the use cases live in
:mod:`~app.modules.fulfillment.service`, the transaction body in
:mod:`~app.modules.fulfillment.workflow`.

Two vocabulary decisions are frozen here and are worth restating, because both are
the kind of thing a later reader wants to "tidy":

* ``FulfillmentStatus`` is **re-exported** from :mod:`app.modules.order.enums`, not
  re-declared - Phase 4 owns that vocabulary and it is already stored in a
  ``CHECK`` on ``orders``.
* ``carrier`` is a **code** from a small allowlist, not free text.
"""

from app.modules.fulfillment.enums import (
    CARRIER_CODES,
    CARRIER_NAMES,
    Carrier,
    FulfillmentStatus,
    is_valid_carrier,
)
from app.modules.fulfillment.models import Fulfillment, FulfillmentItem

__all__ = [
    "CARRIER_CODES",
    "CARRIER_NAMES",
    "Carrier",
    "Fulfillment",
    "FulfillmentItem",
    "FulfillmentStatus",
    "is_valid_carrier",
]
