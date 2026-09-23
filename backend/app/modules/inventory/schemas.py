"""Inventory request/response schemas.

Spec §110 (mass assignment): every request model declares **explicitly** which
fields a client may set. There is no `extra="allow"` and no path that forwards a
whole parsed body into an ORM constructor, so a client cannot reach
`merchant_id`, `total_in_qty` or `version` by adding keys to the payload.

Spec §19: money is integer minor units; quantities are integers. `MoneyMinor`-ish
validation is expressed here as `int` with an explicit description rather than a
float, because a float that reaches a quantity is a silent rounding bug.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Quantities are bounded. The upper bound is not business policy - it is a sanity
#: rail so a fat-fingered `1e9` is rejected before it reaches a CHECK constraint
#: or an admin's eyeballs.
Quantity = Annotated[int, Field(ge=0, le=1_000_000)]
PositiveQuantity = Annotated[int, Field(gt=0, le=1_000_000)]


class InventoryOut(BaseModel):
    """Inventory position as returned to a console user.

    Mirrors the frozen shape in `docs/architecture/API_CONTRACT.md` §7. The
    derived fields (`sellable_qty`, `on_hand_qty`) are sent rather than left for the
    client to compute, because §114 of the contract records that a ratio rendered as
    currency - or a stock figure computed two different ways - is a plausible-looking
    lie. One authority, one answer.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    warehouse_id: int
    sku_id: int

    sku_no: str | None = None
    product_name: str | None = None
    sku_name: str | None = None

    available_qty: int
    locked_qty: int
    safety_stock: int
    sellable_qty: int
    on_hand_qty: int
    version: int

    last_movement_at: datetime | None = None
    updated_at: datetime


class InventoryMovementOut(BaseModel):
    """One ledger row.

    Exposed deliberately: the console's whole point during a stock investigation is
    answering "who changed this, when, and by how much". Hiding the ledger would
    leave an operator with a number and no provenance, which is how a stock
    discrepancy becomes unresolvable.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    movement_type: str
    delta_available: int
    delta_locked: int
    before_available: int
    after_available: int
    before_locked: int
    after_locked: int
    reference_type: str
    reference_id: int | None
    operator_type: str
    operator_id: int | None
    reason: str | None
    trace_id: str | None
    created_at: datetime

    @property
    def net_stock_change(self) -> int:
        return self.delta_available + self.delta_locked


class AdjustmentPreviewRequest(BaseModel):
    """Dry-run request (§47: promotion and adjustment writes need a preview)."""

    model_config = ConfigDict(extra="forbid")

    warehouse_id: int | None = None
    sku_id: int
    delta_available: int = Field(
        description="Signed change in available stock. Negative reduces."
    )


class AdjustmentPreviewOut(BaseModel):
    sku_id: int
    warehouse_id: int
    current_available: int
    current_locked: int
    current_version: int
    proposed_delta: int
    resulting_available: int
    warnings: list[str] = Field(default_factory=list)


class CreateAdjustmentRequest(BaseModel):
    """The write. Note what is NOT accepted.

    `version` is required (§27 optimistic lock): the client must send the version it
    read, so a change that happened in between is detected rather than silently
    overwritten. There is no `idempotency_key` field here - that arrives as the
    `Idempotency-Key` header, because a key in the body invites clients to omit it.
    """

    model_config = ConfigDict(extra="forbid")

    warehouse_id: int | None = None
    sku_id: int
    version: int = Field(ge=1, description="The version the caller last read.")
    delta_available: int = Field(
        description="Signed change in available stock; must be non-zero."
    )
    reason: str | None = Field(default=None, max_length=500)

    @field_validator("delta_available")
    @classmethod
    def _non_zero(cls, value: int) -> int:
        if value == 0:
            msg = "an adjustment must change something"
            raise ValueError(msg)
        return value


class StaleVersionConflict(BaseModel):
    """Body of a 409 from the adjust endpoint (contract §7).

    Carries the current row so the UI can show the operator what actually changed
    instead of an unhelpful "conflict". Returning 409 with no detail forces a blind
    retry, which is how the same mistake gets made twice.
    """

    expected_version: int
    actual_version: int
    current_available: int
    current_locked: int


class LowStockRow(BaseModel):
    inventory: InventoryOut
    shortfall: int


class LedgerVerificationOut(BaseModel):
    """INV-007 check, exposed so an operator can verify a position on demand."""

    consistent: bool
    ledger_available: int
    row_available: int
    ledger_locked: int
    row_locked: int


class WarehouseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    status: str
    is_default: bool


__all__ = [
    "AdjustmentPreviewOut",
    "AdjustmentPreviewRequest",
    "CreateAdjustmentRequest",
    "InventoryMovementOut",
    "InventoryOut",
    "LedgerVerificationOut",
    "LowStockRow",
    "PositiveQuantity",
    "Quantity",
    "StaleVersionConflict",
    "WarehouseOut",
]
