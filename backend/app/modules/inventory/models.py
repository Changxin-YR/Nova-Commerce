"""Inventory domain models - spec §26 and §28.

    warehouses, inventories, inventory_movements

## Why ``available_qty`` and ``locked_qty`` are separate columns

Collapsing them into one number loses the distinction between *stock that is
sold* and *stock that is reserved for an order that has not been paid yet*. Those
behave differently: a locked unit must be released if the order is cancelled or
expires, and must never be sold to somebody else in the meantime. One column
cannot express "currently promised but not yet earned", so it cannot answer the
only question that matters during a flash sale - *is this unit actually mine?*

## The invariant that makes FG-09 meaningful

``CHECK (available_qty >= 0)`` is not decoration. It is the last line of defence:
if the application's locking is ever wrong, the database refuses the write and
turns a silent oversell into a loud error. Application logic can have a bug; a
constraint cannot be raced past.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.modules.inventory.enums import (
    DEFAULT_WAREHOUSE_CODE,
    MovementType,
    OperatorType,
    ReferenceType,
)
from app.shared.db.base import (
    Base,
    MerchantScopedMixin,
    PkMixin,
    SoftDeleteMixin,
    TimestampMixin,
    short_str,
    status_column,
)
from app.shared.db.types import BigIntUnsigned, DateTimeMS

MOVEMENT_TYPES = tuple(m.value for m in MovementType)
OPERATOR_TYPES = tuple(o.value for o in OperatorType)
REFERENCE_TYPES = tuple(r.value for r in ReferenceType)


class Warehouse(Base, PkMixin, TimestampMixin, MerchantScopedMixin, SoftDeleteMixin):
    """A physical stocking location.

    V1 has exactly one, ``MAIN`` (spec §26), but the table exists from the first
    migration so that adding a second location is configuration rather than a
    schema change - and so that ``inventories`` can be keyed by
    ``(warehouse_id, sku_id)`` now, which is the shape that is hard to retrofit.
    """

    __tablename__ = "warehouses"
    __table_args__ = (
        UniqueConstraint("merchant_id", "code", name="uq_warehouses_merchant_code"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE')", name="status_valid"),
    )

    code: Mapped[str] = mapped_column(
        short_str(32), nullable=False, default=DEFAULT_WAREHOUSE_CODE
    )
    name: Mapped[str] = mapped_column(short_str(128), nullable=False)
    status: Mapped[str] = mapped_column(
        status_column(), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    contact_name: Mapped[str | None] = mapped_column(short_str(64), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    inventories: Mapped[list[Inventory]] = relationship(back_populates="warehouse", lazy="noload")


class Inventory(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    """The stock position for one SKU at one warehouse.

    This is the row the whole §27 story is about. It is the one hot row in the
    system, and the one place where a lost update means real money.
    """

    __tablename__ = "inventories"
    __table_args__ = (
        UniqueConstraint("warehouse_id", "sku_id", name="uq_inventories_warehouse_sku"),
        # INV-001 / INV-002. These two lines are why FG-09 cannot pass by
        # accident: even if the pessimistic lock were removed entirely, an
        # oversell would raise an IntegrityError instead of corrupting the number.
        CheckConstraint("available_qty >= 0", name="available_non_negative"),
        CheckConstraint("locked_qty >= 0", name="locked_non_negative"),
        CheckConstraint("safety_stock >= 0", name="safety_stock_non_negative"),
        Index("ix_inventories_sku", "sku_id"),
        Index("ix_inventories_low_stock", "sku_id", "available_qty"),
    )

    warehouse_id: Mapped[int] = mapped_column(
        # RESTRICT, not SET NULL: these columns participate in CHECK constraints,
        # and MySQL rejects (errno 3823) a column that both carries a CHECK and
        # sits in a foreign key with a mutating referential action. RESTRICT is
        # also correct - deleting a warehouse that still holds stock should fail.
        BigIntUnsigned,
        ForeignKey("warehouses.id", ondelete="RESTRICT"),
        nullable=False,
    )
    sku_id: Mapped[int] = mapped_column(
        BigIntUnsigned,
        ForeignKey("product_skus.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: Sellable right now. Decremented when an order reserves stock.
    available_qty: Mapped[int] = mapped_column(
        BigIntUnsigned, nullable=False, default=0, server_default="0"
    )
    #: Promised to an unpaid order. Moved out of ``available_qty`` but not yet
    #: deducted, because the sale is not final until payment succeeds.
    locked_qty: Mapped[int] = mapped_column(
        BigIntUnsigned, nullable=False, default=0, server_default="0"
    )
    #: Never sell below this. 0 means "sell everything".
    safety_stock: Mapped[int] = mapped_column(
        BigIntUnsigned, nullable=False, default=0, server_default="0"
    )
    #: Cumulative counters for reconciliation, so "does the ledger explain the
    #: balance?" is answerable without replaying every movement (INV-007).
    total_in_qty: Mapped[int] = mapped_column(
        BigIntUnsigned, nullable=False, default=0, server_default="0"
    )
    total_out_qty: Mapped[int] = mapped_column(
        BigIntUnsigned, nullable=False, default=0, server_default="0"
    )

    #: Optimistic-locking counter for background edits (spec §27): a low-contention
    #: admin adjustment can retry, whereas the checkout path must not (§27/ADR-004).
    version: Mapped[int] = mapped_column(
        BigIntUnsigned, nullable=False, default=1, server_default="1"
    )

    last_movement_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    last_counted_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    warehouse: Mapped[Warehouse] = relationship(back_populates="inventories", lazy="joined")

    # -- derived ---------------------------------------------------------
    @property
    def sellable_qty(self) -> int:
        """What a new order may actually take."""
        return max(self.available_qty - self.safety_stock, 0)

    @property
    def on_hand_qty(self) -> int:
        return self.available_qty + self.locked_qty

    def can_reserve(self, quantity: int) -> bool:
        return quantity > 0 and self.sellable_qty >= quantity

    def __repr__(self) -> str:
        return (
            f"<Inventory sku={self.sku_id} wh={self.warehouse_id} "
            f"available={self.available_qty} locked={self.locked_qty}>"
        )


class InventoryMovement(Base, PkMixin, TimestampMixin):
    """Append-only stock ledger. Frozen column set by spec §28.

    **Never updated, never deleted.** Corrections are made by appending a
    compensating movement, exactly as in double-entry bookkeeping. That is what
    makes INV-007 checkable: at any moment the balance can be recomputed from the
    ledger and compared with the row.

    ``idempotency_key`` carries a UNIQUE constraint, which is what makes INV-003
    ("the same stock cannot be deducted twice") a database guarantee rather than
    an application promise. A retried payment callback that tries to deduct twice
    hits the unique index, not a code path somebody may have missed.
    """

    __tablename__ = "inventory_movements"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_inventory_movements_idempotency_key"),
        CheckConstraint(
            "movement_type IN ('PURCHASE_IN','ORDER_LOCK','ORDER_RELEASE',"
            "'ORDER_DEDUCT','RETURN_IN','MANUAL_ADJUST','DAMAGED_OUT')",
            name="movement_type_valid",
        ),
        CheckConstraint(
            "operator_type IN ('SYSTEM','CUSTOMER','STAFF','AGENT','MCP','WORKER')",
            name="operator_type_valid",
        ),
        CheckConstraint("after_available >= 0", name="after_available_non_negative"),
        CheckConstraint("after_locked >= 0", name="after_locked_non_negative"),
        CheckConstraint("before_available >= 0", name="before_available_non_negative"),
        CheckConstraint("before_locked >= 0", name="before_locked_non_negative"),
        # INV-007 stated as a constraint: the recorded delta must equal the
        # difference between the recorded before/after values. A ledger whose
        # arithmetic does not add up is worse than no ledger.
        CheckConstraint(
            "delta_available = after_available - before_available",
            name="delta_available_consistent",
        ),
        CheckConstraint(
            "delta_locked = after_locked - before_locked",
            name="delta_locked_consistent",
        ),
        Index("ix_inventory_movements_sku_created", "sku_id", "created_at"),
        Index("ix_inventory_movements_reference", "reference_type", "reference_id"),
        Index("ix_inventory_movements_type_created", "movement_type", "created_at"),
    )

    warehouse_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    sku_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("product_skus.id", ondelete="RESTRICT"), nullable=False
    )

    movement_type: Mapped[str] = mapped_column(status_column(32), nullable=False)

    delta_available: Mapped[int] = mapped_column(Integer, nullable=False)
    delta_locked: Mapped[int] = mapped_column(Integer, nullable=False)
    before_available: Mapped[int] = mapped_column(BigIntUnsigned, nullable=False)
    after_available: Mapped[int] = mapped_column(BigIntUnsigned, nullable=False)
    before_locked: Mapped[int] = mapped_column(BigIntUnsigned, nullable=False)
    after_locked: Mapped[int] = mapped_column(BigIntUnsigned, nullable=False)

    reference_type: Mapped[str] = mapped_column(
        status_column(32), nullable=False, default=ReferenceType.MANUAL.value
    )
    reference_id: Mapped[int | None] = mapped_column(BigIntUnsigned, nullable=True)

    operator_type: Mapped[str] = mapped_column(
        status_column(32), nullable=False, default=OperatorType.SYSTEM.value
    )
    operator_id: Mapped[int | None] = mapped_column(BigIntUnsigned, nullable=True)

    #: The dedupe key. Non-null by contract: an idempotent ledger requires every
    #: row to be identifiable, so callers must always supply one.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Correlation with the request that caused the movement (spec §131).
    trace_id: Mapped[str | None] = mapped_column(short_str(64), nullable=True)
    #: Free-form context (order_no, provider event id). Snapshot only, and
    #: redacted before write - spec §19 permits JSON for exactly this.
    metadata_json: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )

    @classmethod
    def build(
        cls,
        *,
        warehouse_id: int,
        sku_id: int,
        movement_type: MovementType,
        before_available: int,
        after_available: int,
        before_locked: int,
        after_locked: int,
        idempotency_key: str,
        reference_type: ReferenceType = ReferenceType.MANUAL,
        reference_id: int | None = None,
        operator_type: OperatorType = OperatorType.SYSTEM,
        operator_id: int | None = None,
        reason: str | None = None,
        trace_id: str | None = None,
        metadata_json: dict | None = None,
    ) -> InventoryMovement:
        """Construct a movement with deltas derived, never passed in.

        Deriving the deltas here rather than accepting them as arguments removes
        the whole class of bug where a caller supplies a delta that disagrees
        with its own before/after pair - which the CHECK constraint would reject
        at the database anyway, but only after wasting a round trip and muddying
        the error.
        """
        return cls(
            warehouse_id=warehouse_id,
            sku_id=sku_id,
            movement_type=movement_type.value,
            delta_available=after_available - before_available,
            delta_locked=after_locked - before_locked,
            before_available=before_available,
            after_available=after_available,
            before_locked=before_locked,
            after_locked=after_locked,
            reference_type=reference_type.value,
            reference_id=reference_id,
            operator_type=operator_type.value,
            operator_id=operator_id,
            idempotency_key=idempotency_key,
            reason=reason,
            trace_id=trace_id,
            metadata_json=metadata_json,
        )

    @property
    def net_stock_change(self) -> int:
        """Positive movement of sellable inventory (``available + locked``).

        This is the number a reconciliation job sums to compare against the
        balance row (INV-007).
        """
        return self.delta_available + self.delta_locked


__all__ = [
    "MOVEMENT_TYPES",
    "OPERATOR_TYPES",
    "REFERENCE_TYPES",
    "Inventory",
    "InventoryMovement",
    "Warehouse",
]
