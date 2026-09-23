"""Fulfillment domain vocabularies.

    FulfillmentStatus (re-exported), Carrier

PHASE5_DESIGN section 4.2 freezes both decisions made here:

1. **``fulfillment_status`` is not re-declared.** The membership
   (UNFULFILLED/PARTIAL_SHIPPED/SHIPPED/DELIVERED) already lives in
   :class:`app.modules.order.enums.FulfillmentStatus` and Phase 4 froze it, so this
   module **imports and re-exports** it rather than declaring a second vocabulary.
   Widening or renaming it is a migration on ``orders`` as well as on
   ``fulfillments`` - a genuine schema change, not a local edit - so a second
   declaration here would be a promise to keep two ``CHECK`` constraints in sync by
   hand, which is exactly the drift the enum-derived constraints exist to prevent.
2. **``carrier`` is a CODE, not free text.** ``"SF"``, ``"YTO"``, ``"JD"``, not
   ``"SF Express"`` or ``"顺丰"``. The console renders a *select*, so an
   unrecognised value is a client bug rather than user input; accepting it would
   put an unqueryable string in a column that the logistics integration of a later
   phase has to map back to a provider.
"""

from __future__ import annotations

from enum import StrEnum

from app.modules.order.enums import FulfillmentStatus

__all__ = [
    "CARRIER_CODES",
    "CARRIER_NAMES",
    "Carrier",
    "FulfillmentStatus",
    "is_valid_carrier",
]


class Carrier(StrEnum):
    """The supported carrier codes (PHASE5_DESIGN section 4.2).

    A small, frozen allowlist on purpose. ``carrier`` is nullable on
    ``fulfillments`` - a package exists in ``UNFULFILLED`` before anybody ships it,
    and it has no carrier until then - but when it *is* set it must be one of these,
    validated at the schema edge (``ValidationError``) rather than stored as an
    arbitrary string.

    Adding a carrier is a one-line change here **plus** a migration for the
    ``CHECK (carrier IN (...))`` on ``fulfillments``: Alembic does not
    autogenerate CHECK changes on MySQL (``HANDOFF.md`` section 6), so the
    constraint has to be hand-written, and a new member without that migration
    would fail at write time in production rather than at review time.
    """

    #: 顺丰速运 / SF Express.
    SF = "SF"
    #: 圆通速递 / YTO Express.
    YTO = "YTO"
    #: 京东物流 / JD Logistics.
    JD = "JD"
    #: 中通快递 / ZTO Express.
    ZTO = "ZTO"
    #: 申通快递 / STO Express.
    STO = "STO"
    #: 韵达速递 / Yunda Express.
    YD = "YD"
    #: 邮政EMS / China Post EMS.
    EMS = "EMS"
    #: An escape hatch for a carrier nobody has added yet. Present on purpose: the
    #: alternative is that an operator ships with a small regional carrier and the
    #: console has no value to pick, so somebody edits the enum during an incident.
    #: A deliberately-named "OTHER" is reviewable; a free-text column is not.
    OTHER = "OTHER"


#: Display names for the console's select. Kept next to the codes so a new code
#: cannot be added without a label, which is the small omission that leaves a
#: dropdown rendering an empty option.
CARRIER_NAMES: dict[str, str] = {
    Carrier.SF.value: "顺丰速运",
    Carrier.YTO.value: "圆通速递",
    Carrier.JD.value: "京东物流",
    Carrier.ZTO.value: "中通快递",
    Carrier.STO.value: "申通快递",
    Carrier.YD.value: "韵达速递",
    Carrier.EMS.value: "邮政EMS",
    Carrier.OTHER.value: "其他",
}

#: Vocabulary tuple, in the exact order the migration's ``CHECK`` constraint lists
#: it. Derived from the enum rather than re-typed, for the same reason as every
#: other vocabulary in the project.
CARRIER_CODES: tuple[str, ...] = tuple(member.value for member in Carrier)


def is_valid_carrier(value: str | None) -> bool:
    """Whether ``value`` is an allowed carrier code.

    ``None`` is valid: an unshipped package has no carrier yet, and rejecting that
    would force the fulfillment shell to invent a placeholder carrier - a stored
    lie in a column the customer's tracking page renders.
    """
    if value is None:
        return True
    return value in CARRIER_CODES
