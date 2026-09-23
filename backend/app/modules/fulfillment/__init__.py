"""Fulfillment domain - packages, shipping, and the order's goods-out axis.

PHASE5_DESIGN sections 3, 4.2, 5.4, 6.3. Import surface only; every use case -
including the ship path and the cumulative 70001 guard - lives in
:mod:`~app.modules.fulfillment.service`. Section 3's layout named a
``fulfillment/workflow.py``, and it was deliberately **not** created: the shipping
logic is one cohesive method that owns its own lock and commit, so a separate
workflow module would have been an empty layer created only to satisfy a list.

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
