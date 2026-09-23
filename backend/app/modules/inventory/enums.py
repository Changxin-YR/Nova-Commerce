"""Inventory domain vocabularies.

Spec references:
    §26  warehouses, inventories, inventory_movements
    §28  the seven movement types
    §19  statuses are ``VARCHAR`` + Python Enum
"""

from __future__ import annotations

from enum import StrEnum


class MovementType(StrEnum):
    """Why stock changed. Frozen by spec §28.

    Every mutation of ``inventories`` must append exactly one movement, and the
    movement's ``before_*``/``after_*`` columns must reconcile with the delta.
    That is INV-007: a movement ledger that does not fully explain the balance is
    worse than no ledger, because it creates false confidence during an incident.
    """

    PURCHASE_IN = "PURCHASE_IN"
    ORDER_LOCK = "ORDER_LOCK"
    ORDER_RELEASE = "ORDER_RELEASE"
    ORDER_DEDUCT = "ORDER_DEDUCT"
    RETURN_IN = "RETURN_IN"
    MANUAL_ADJUST = "MANUAL_ADJUST"
    DAMAGED_OUT = "DAMAGED_OUT"

    @property
    def affects_available(self) -> bool:
        return self in {
            MovementType.PURCHASE_IN,
            MovementType.ORDER_LOCK,
            MovementType.ORDER_RELEASE,
            MovementType.ORDER_DEDUCT,
            MovementType.RETURN_IN,
            MovementType.MANUAL_ADJUST,
            MovementType.DAMAGED_OUT,
        }

    @property
    def is_operator_initiated(self) -> bool:
        """True when a human (or an approved agent action) caused the change.

        Used by the audit layer to distinguish "the system did this as part of an
        order" from "somebody deliberately changed the number" - the first
        question asked in any stock-discrepancy investigation.
        """
        return self in {MovementType.MANUAL_ADJUST, MovementType.DAMAGED_OUT}


class OperatorType(StrEnum):
    """Who caused a movement (spec §28 ``operator_type``)."""

    SYSTEM = "SYSTEM"
    CUSTOMER = "CUSTOMER"
    STAFF = "STAFF"
    AGENT = "AGENT"
    MCP = "MCP"
    WORKER = "WORKER"


class ReferenceType(StrEnum):
    """What business object a movement refers to (spec §28 ``reference_type``)."""

    ORDER = "ORDER"
    ORDER_ITEM = "ORDER_ITEM"
    AFTER_SALE = "AFTER_SALE"
    PURCHASE_ORDER = "PURCHASE_ORDER"
    MANUAL = "MANUAL"
    RECONCILIATION = "RECONCILIATION"


class InventoryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    DISABLED = "DISABLED"


class WarehouseStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


#: V1 runs exactly one warehouse (spec §26). Named here rather than sprinkled as
#: a literal so that the eventual multi-warehouse work has one place to start.
DEFAULT_WAREHOUSE_CODE = "MAIN"


__all__ = [
    "DEFAULT_WAREHOUSE_CODE",
    "InventoryStatus",
    "MovementType",
    "OperatorType",
    "ReferenceType",
    "WarehouseStatus",
]
