"""Inventory data access.

Spec §18: a repository does data access and **nothing else**. No stock policy,
no risk, no workflow. Everything that decides *whether* a reservation is allowed
lives in ``service.py``. If a rule ever feels like it wants to live here, it
belongs one layer up.

## The one method that matters

:meth:`InventoryRepository.lock_for_update` is the foundation of FG-09. Spec §27
requires the CreateOrder path to take `SELECT ... FOR UPDATE` on the inventory
row inside a short transaction, and forbids substituting a Redis lock for it.

The reason is that the *check* and the *write* must be atomic with respect to
other transactions. A read followed by a write - even a read of the freshest
committed value - lets two callers both observe `available_qty = 1`, both decide
there is enough, and both decrement. That is the classic TOCTOU race, and it is
exactly how a shop oversells. ``FOR UPDATE`` closes it by serialising the
competing transactions at the row.

``with_for_update()`` is used rather than raw SQL so the statement still goes
through the ORM's mapper configuration and so the architecture test's "no SQL in
services" rule stays satisfiable.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session, joinedload

from app.modules.inventory.enums import MovementType
from app.modules.inventory.models import Inventory, InventoryMovement, Warehouse
from app.shared.db.base import utc_now


class WarehouseRepository:
    """Queries over ``warehouses``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, warehouse_id: int) -> Warehouse | None:
        return self._session.get(Warehouse, warehouse_id)

    def get_by_code(self, code: str, *, merchant_id: int | None = None) -> Warehouse | None:
        stmt = select(Warehouse).where(Warehouse.code == code)
        if merchant_id is not None:
            stmt = stmt.where(Warehouse.merchant_id == merchant_id)
        return self._session.execute(stmt).scalars().first()

    def get_default(self, *, merchant_id: int | None = None) -> Warehouse | None:
        """The warehouse V1 operates out of (spec §26: exactly one, ``MAIN``).

        Prefers the row explicitly flagged default and falls back to the lowest
        id, so a database seeded before the flag existed still resolves.
        """
        stmt = select(Warehouse).where(Warehouse.status == "ACTIVE")
        if merchant_id is not None:
            stmt = stmt.where(Warehouse.merchant_id == merchant_id)
        explicit = self._session.execute(
            stmt.where(Warehouse.is_default.is_(True)).order_by(Warehouse.id)
        ).scalars().first()
        if explicit is not None:
            return explicit
        return self._session.execute(stmt.order_by(Warehouse.id)).scalars().first()

    def add(self, warehouse: Warehouse) -> Warehouse:
        self._session.add(warehouse)
        self._session.flush()
        return warehouse


class InventoryRepository:
    """Queries over ``inventories`` and ``inventory_movements``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- reads -----------------------------------------------------------
    def get(self, inventory_id: int) -> Inventory | None:
        return self._session.get(Inventory, inventory_id)

    def get_by_sku(
        self,
        *,
        sku_id: int,
        warehouse_id: int | None = None,
    ) -> Inventory | None:
        stmt = select(Inventory).options(joinedload(Inventory.warehouse)).where(
            Inventory.sku_id == sku_id
        )
        if warehouse_id is not None:
            stmt = stmt.where(Inventory.warehouse_id == warehouse_id)
        return self._session.execute(stmt.order_by(Inventory.warehouse_id)).scalars().first()

    def get_many_by_skus(
        self,
        sku_ids: Sequence[int],
        *,
        warehouse_id: int | None = None,
    ) -> dict[int, Inventory]:
        """Bulk fetch for an order preview.

        Deliberately a plain read, **not** a lock: a preview must not hold row
        locks while a human reads the page. The lock is taken later, in the
        create-order transaction, where the numbers are actually committed.
        """
        if not sku_ids:
            return {}
        stmt = select(Inventory).where(Inventory.sku_id.in_(list(sku_ids)))
        if warehouse_id is not None:
            stmt = stmt.where(Inventory.warehouse_id == warehouse_id)
        rows = self._session.execute(stmt).scalars().all()
        return {row.sku_id: row for row in rows}

    def list_inventory(
        self,
        *,
        merchant_id: int | None = None,
        search: str | None = None,
        low_stock_only: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[Inventory], int]:
        from app.modules.catalog.models import Product, ProductSku

        stmt: Select[tuple[Inventory]] = select(Inventory).options(joinedload(Inventory.warehouse))
        count_stmt = select(func.count()).select_from(Inventory)

        conditions = []
        if merchant_id is not None:
            conditions.append(Inventory.merchant_id == merchant_id)
        if low_stock_only:
            # ``safety_stock`` is part of the comparison, so a SKU that is low
            # relative to its own threshold is found - not one that is low
            # relative to somebody else's.
            conditions.append(Inventory.available_qty <= Inventory.safety_stock)
        if search:
            # Searching stock by SKU code or product name requires a join; there
            # is nothing human-searchable on the inventory row itself.
            pattern = f"%{search.strip()}%"
            from sqlalchemy import or_

            conditions.append(
                Inventory.sku_id.in_(
                    select(ProductSku.id)
                    .join(Product, Product.id == ProductSku.product_id)
                    .where(or_(ProductSku.sku_no.like(pattern), Product.name.like(pattern)))
                )
            )

        for condition in conditions:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = int(self._session.execute(count_stmt).scalar_one())
        rows = (
            self._session.execute(
                stmt.order_by(Inventory.available_qty.asc(), Inventory.id.desc())
                .offset(offset)
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return list(rows), total

    # -- the lock ---------------------------------------------------------
    def lock_for_update(
        self,
        *,
        warehouse_id: int,
        sku_id: int,
        nowait: bool = False,
    ) -> Inventory | None:
        """Acquire a row lock on one inventory position.

        Must be called **inside** an open transaction; the lock is held until
        that transaction commits or rolls back, which is why spec §27 insists the
        transaction be short:

            session.begin()
            inv = repo.lock_for_update(warehouse_id=..., sku_id=...)
            ... check available_qty, mutate, append movement ...
            session.commit()          # <- lock released here

        ``nowait`` turns lock contention into an immediate error instead of a
        wait. It is off by default: with a 5s ``innodb_lock_wait_timeout`` the
        wait is already bounded, and waiting usually wins the sale. It exists for
        the concurrency test's negative control, where the point is to *observe*
        contention rather than to queue behind it.

        Returns ``None`` when no stock row exists. The caller must decide whether
        that is "not stocked" or an error - this layer does not guess.
        """
        stmt = (
            select(Inventory)
            .where(
                Inventory.warehouse_id == warehouse_id,
                Inventory.sku_id == sku_id,
            )
            .with_for_update(nowait=nowait)
        )
        return self._session.execute(stmt).scalars().first()

    def lock_many_for_update(
        self,
        *,
        warehouse_id: int,
        sku_ids: Sequence[int],
    ) -> dict[int, Inventory]:
        """Lock several positions, **in ascending sku_id order**.

        The ordering is not cosmetic. Two concurrent multi-item orders that lock
        the same pair of rows in opposite orders deadlock: transaction A holds
        sku 1 and waits for sku 2, while B holds sku 2 and waits for sku 1.
        MySQL resolves that by killing one transaction with a deadlock error -
        correct but wasteful, and it turns a legitimate order into a retry.
        Acquiring in a globally consistent order removes the cycle entirely.

        Using ``FOR UPDATE`` on a set of rows in one statement would also avoid
        the cycle, but MySQL does not guarantee the order it locks them in, so
        the explicit ordered loop is the honest version.
        """
        ordered = sorted(set(sku_ids))
        locked: dict[int, Inventory] = {}
        for sku_id in ordered:
            row = self.lock_for_update(warehouse_id=warehouse_id, sku_id=sku_id)
            if row is not None:
                locked[sku_id] = row
        return locked

    # -- writes -----------------------------------------------------------
    def add(self, inventory: Inventory) -> Inventory:
        self._session.add(inventory)
        self._session.flush()
        return inventory

    def append_movement(self, movement: InventoryMovement) -> InventoryMovement:
        """Append one ledger row.

        Insert-only. There is deliberately no ``update_movement`` and no
        ``delete_movement``: spec §28 makes the ledger append-only, and a
        correction is expressed by appending a compensating movement. That is
        what makes INV-007 ("the movements explain the balance") checkable
        rather than merely asserted.
        """
        self._session.add(movement)
        self._session.flush()
        return movement

    def touch_movement_timestamp(self, inventory: Inventory, *, when: datetime | None = None) -> None:
        inventory.last_movement_at = when or utc_now()
        self._session.flush()

    # -- reconciliation ---------------------------------------------------
    def movement_signature(self, *, warehouse_id: int, sku_id: int) -> tuple[int, int]:
        """Sum every movement for one position.

        Used by the reconciliation job (and by FG-09's INV-007 assertion) to
        confirm the ledger explains the balance. Returns
        ``(net_available_delta, net_locked_delta)``.
        """
        row = self._session.execute(
            select(
                func.coalesce(func.sum(InventoryMovement.delta_available), 0),
                func.coalesce(func.sum(InventoryMovement.delta_locked), 0),
            ).where(
                InventoryMovement.warehouse_id == warehouse_id,
                InventoryMovement.sku_id == sku_id,
            )
        ).one()
        return int(row[0]), int(row[1])

    def movements_for(
        self,
        *,
        warehouse_id: int,
        sku_id: int,
        movement_type: MovementType | None = None,
        limit: int = 100,
    ) -> list[InventoryMovement]:
        stmt = select(InventoryMovement).where(
            InventoryMovement.warehouse_id == warehouse_id,
            InventoryMovement.sku_id == sku_id,
        )
        if movement_type is not None:
            stmt = stmt.where(InventoryMovement.movement_type == movement_type.value)
        return list(
            self._session.execute(
                stmt.order_by(InventoryMovement.id.desc()).limit(limit)
            )
            .scalars()
            .all()
        )

    def find_movement_by_idempotency_key(self, key: str) -> InventoryMovement | None:
        """Replay lookup for INV-003.

        The UNIQUE constraint is the real guarantee; this method exists so the
        service can return the *original* outcome on a replay instead of raising
        a constraint violation, which would be a confusing way to say "already
        done".
        """
        return self._session.execute(
            select(InventoryMovement).where(InventoryMovement.idempotency_key == key)
        ).scalars().first()


__all__ = ["InventoryRepository", "WarehouseRepository"]
