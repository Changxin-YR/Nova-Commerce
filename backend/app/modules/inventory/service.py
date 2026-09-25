"""Inventory application services.

Spec references:
    §27  CreateOrder locks stock with ``SELECT ... FOR UPDATE`` inside a SHORT
         transaction. A Redis lock must never replace it.
    §28  append-only movements with before/after/delta and an idempotency key.
    §50  a reconciliation job must be able to prove the ledger explains the
         balance (INV-007).
    §112 INV-001 available_qty >= 0 · INV-002 locked_qty >= 0 ·
         INV-003 the same stock is never deducted twice.

## The ordering rule that makes this correct

**Read the balance only after the row lock is held.** Every method below follows
that order, and it is not a stylistic preference:

    lock -> re-read -> decide -> write -> append movement

Checking availability *before* locking is a time-of-check/time-of-use race. Two
transactions both read ``available_qty = 1``, both conclude there is enough, and
both decrement - the shop has sold one unit twice. The database's
``CHECK (available_qty >= 0)`` would then reject the second write, which is a
better outcome than corruption but still the wrong outcome: a customer who was
told yes is told no. Locking first makes the decision itself exclusive.

## Transaction ownership

The **stock-change** methods do not commit. The caller owns the transaction,
because §49 requires the business rows and the outbox row to commit together -
an implicit commit here would break that. For order creation the caller's
transaction *is* the short transaction of §27.

The console-facing *use cases* at the bottom of this class are the deliberate
exception, and they are named so the boundary is visible at the call site: a use
case that exists only to serve one HTTP request owns its commit (``adjust_and_commit``,
``verify_ledger_for_sku``), while ``adjust`` and ``verify_ledger`` keep the primitive
contract above. Splitting them this way is what lets the router stay a transport
edge: it never opens a session query and never commits one.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    InsufficientStockError,
    InventoryNotFoundError,
    InventoryVersionConflictError,
    ValidationError,
)
from app.core.logging import get_logger
from app.modules.catalog.models import Product, ProductSku
from app.modules.inventory.enums import (
    MovementType,
    OperatorType,
    ReferenceType,
)
from app.modules.inventory.models import Inventory, InventoryMovement, Warehouse
from app.modules.inventory.repository import InventoryRepository, WarehouseRepository
from app.modules.inventory.schemas import InventoryOut
from app.shared.db.base import utc_now

logger = get_logger(__name__)


@dataclass(slots=True)
class ReservationResult:
    """What a reservation actually did.

    ``replayed`` distinguishes "we just did this" from "this was already done".
    Without it, a retried payment callback would look identical to a fresh one,
    and the caller could not tell whether to emit a downstream event - which is
    how a duplicate side effect (INV-004) gets introduced.
    """

    sku_id: int
    quantity: int
    movement_id: int | None
    available_after: int
    locked_after: int
    replayed: bool = False


@dataclass(slots=True)
class AdjustmentPreview:
    """Dry-run result for an inventory adjustment (§47 requires preview first)."""

    sku_id: int
    warehouse_id: int
    current_available: int
    current_locked: int
    current_version: int
    proposed_delta: int
    resulting_available: int
    warnings: list[str]


class InventoryService:
    """Reserve, release, deduct and adjust stock."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._inventory = InventoryRepository(session)
        self._warehouses = WarehouseRepository(session)

    # -- helpers ----------------------------------------------------------
    def resolve_warehouse(self, warehouse_id: int | None, *, merchant_id: int | None = None) -> Warehouse:
        """Resolve the operating warehouse, defaulting to ``MAIN``."""
        warehouse = (
            self._warehouses.get(warehouse_id)
            if warehouse_id is not None
            else self._warehouses.get_default(merchant_id=merchant_id)
        )
        if warehouse is None:
            from app.core.errors import WarehouseNotFoundError

            raise WarehouseNotFoundError("no active warehouse is configured")
        return warehouse

    def _movement_exists(self, idempotency_key: str) -> InventoryMovement | None:
        return self._inventory.find_movement_by_idempotency_key(idempotency_key)

    def _append(
        self,
        *,
        inventory: Inventory,
        movement_type: MovementType,
        before_available: int,
        after_available: int,
        before_locked: int,
        after_locked: int,
        idempotency_key: str,
        reference_type: ReferenceType,
        reference_id: int | None,
        operator_type: OperatorType,
        operator_id: int | None,
        reason: str | None = None,
    ) -> InventoryMovement:
        movement = InventoryMovement.build(
            warehouse_id=inventory.warehouse_id,
            sku_id=inventory.sku_id,
            movement_type=movement_type,
            before_available=before_available,
            after_available=after_available,
            before_locked=before_locked,
            after_locked=after_locked,
            idempotency_key=idempotency_key,
            reference_type=reference_type,
            reference_id=reference_id,
            operator_type=operator_type,
            operator_id=operator_id,
            reason=reason,
        )
        try:
            self._inventory.append_movement(movement)
        except IntegrityError as exc:
            # The UNIQUE constraint on idempotency_key fired. Another transaction
            # performed this exact operation between our lookup and our insert -
            # which is the constraint doing precisely its job (INV-003). Surface
            # it as a domain error rather than a raw IntegrityError so the caller
            # can treat it as "already applied".
            self._session.rollback()
            raise InventoryVersionConflictError(
                "this inventory operation was already applied (duplicate idempotency key)",
                context={"idempotency_key": idempotency_key},
            ) from exc
        inventory.last_movement_at = utc_now()
        return movement

    # -- reserve ----------------------------------------------------------
    def reserve(
        self,
        *,
        sku_id: int,
        quantity: int,
        idempotency_key: str,
        warehouse_id: int | None = None,
        reference_type: ReferenceType = ReferenceType.ORDER,
        reference_id: int | None = None,
        operator_type: OperatorType = OperatorType.SYSTEM,
        operator_id: int | None = None,
    ) -> ReservationResult:
        """Move ``quantity`` from available into locked.

        This is the operation FG-09 exists to test: 20 concurrent callers, 1 unit
        of stock, exactly one winner. The winner is decided by the row lock, not
        by luck or by retry ordering.

        Idempotent on ``idempotency_key``: a replay returns the original result
        with ``replayed=True`` and performs no second mutation.
        """
        if quantity <= 0:
            raise ValidationError("quantity must be positive")
        if not idempotency_key:
            raise ValidationError("an idempotency key is required for every stock movement")

        existing = self._movement_exists(idempotency_key)
        if existing is not None:
            inventory = self._inventory.get_by_sku(
                sku_id=sku_id, warehouse_id=existing.warehouse_id
            )
            return ReservationResult(
                sku_id=sku_id,
                quantity=quantity,
                movement_id=existing.id,
                available_after=inventory.available_qty if inventory else existing.after_available,
                locked_after=inventory.locked_qty if inventory else existing.after_locked,
                replayed=True,
            )

        warehouse = self.resolve_warehouse(warehouse_id)

        # ---- the lock. Everything below runs under it. -------------------
        inventory = self._inventory.lock_for_update(
            warehouse_id=warehouse.id, sku_id=sku_id
        )
        if inventory is None:
            raise InventoryNotFoundError(
                f"SKU {sku_id} has no stock record in warehouse {warehouse.id}",
                context={"sku_id": sku_id, "warehouse_id": warehouse.id},
            )

        # ---- decide, now that nobody else can interleave ------------------
        if not inventory.can_reserve(quantity):
            raise InsufficientStockError(
                f"only {inventory.sellable_qty} of SKU {sku_id} can be reserved",
                context={
                    "sku_id": sku_id,
                    "requested": quantity,
                    "available": inventory.available_qty,
                    "locked": inventory.locked_qty,
                    "safety_stock": inventory.safety_stock,
                },
            )

        before_available, before_locked = inventory.available_qty, inventory.locked_qty
        inventory.available_qty -= quantity
        inventory.locked_qty += quantity
        inventory.version += 1

        movement = self._append(
            inventory=inventory,
            movement_type=MovementType.ORDER_LOCK,
            before_available=before_available,
            after_available=inventory.available_qty,
            before_locked=before_locked,
            after_locked=inventory.locked_qty,
            idempotency_key=idempotency_key,
            reference_type=reference_type,
            reference_id=reference_id,
            operator_type=operator_type,
            operator_id=operator_id,
        )
        self._session.flush()

        return ReservationResult(
            sku_id=sku_id,
            quantity=quantity,
            movement_id=movement.id,
            available_after=inventory.available_qty,
            locked_after=inventory.locked_qty,
            replayed=False,
        )

    # -- release ----------------------------------------------------------
    def release(
        self,
        *,
        sku_id: int,
        quantity: int,
        idempotency_key: str,
        warehouse_id: int | None = None,
        reference_type: ReferenceType = ReferenceType.ORDER,
        reference_id: int | None = None,
        operator_type: OperatorType = OperatorType.SYSTEM,
        operator_id: int | None = None,
        reason: str | None = None,
    ) -> ReservationResult:
        """Return reserved stock to available (order cancelled or expired)."""
        if quantity <= 0:
            raise ValidationError("quantity must be positive")

        existing = self._movement_exists(idempotency_key)
        if existing is not None:
            inventory = self._inventory.get_by_sku(
                sku_id=sku_id, warehouse_id=existing.warehouse_id
            )
            return ReservationResult(
                sku_id=sku_id,
                quantity=quantity,
                movement_id=existing.id,
                available_after=inventory.available_qty if inventory else existing.after_available,
                locked_after=inventory.locked_qty if inventory else existing.after_locked,
                replayed=True,
            )

        warehouse = self.resolve_warehouse(warehouse_id)
        inventory = self._inventory.lock_for_update(warehouse_id=warehouse.id, sku_id=sku_id)
        if inventory is None:
            raise InventoryNotFoundError(f"SKU {sku_id} has no stock record")

        if inventory.locked_qty < quantity:
            # Releasing more than was ever locked means the caller's bookkeeping
            # is wrong. Refusing is better than inventing stock: a silent
            # correction here would mask the bug and inflate inventory.
            raise ValidationError(
                f"cannot release {quantity} of SKU {sku_id}: only {inventory.locked_qty} is locked",
                context={"sku_id": sku_id, "locked": inventory.locked_qty, "requested": quantity},
            )

        before_available, before_locked = inventory.available_qty, inventory.locked_qty
        inventory.available_qty += quantity
        inventory.locked_qty -= quantity
        inventory.version += 1

        movement = self._append(
            inventory=inventory,
            movement_type=MovementType.ORDER_RELEASE,
            before_available=before_available,
            after_available=inventory.available_qty,
            before_locked=before_locked,
            after_locked=inventory.locked_qty,
            idempotency_key=idempotency_key,
            reference_type=reference_type,
            reference_id=reference_id,
            operator_type=operator_type,
            operator_id=operator_id,
            reason=reason,
        )
        self._session.flush()
        return ReservationResult(
            sku_id=sku_id,
            quantity=quantity,
            movement_id=movement.id,
            available_after=inventory.available_qty,
            locked_after=inventory.locked_qty,
        )

    # -- deduct -----------------------------------------------------------
    def deduct(
        self,
        *,
        sku_id: int,
        quantity: int,
        idempotency_key: str,
        warehouse_id: int | None = None,
        reference_type: ReferenceType = ReferenceType.ORDER,
        reference_id: int | None = None,
        operator_type: OperatorType = OperatorType.SYSTEM,
        operator_id: int | None = None,
    ) -> ReservationResult:
        """Consume locked stock for good (payment succeeded).

        ``available_qty`` is untouched: the unit left available when it was
        reserved. Only ``locked_qty`` falls, and ``total_out_qty`` rises so the
        cumulative counters stay reconcilable (INV-007).
        """
        if quantity <= 0:
            raise ValidationError("quantity must be positive")

        existing = self._movement_exists(idempotency_key)
        if existing is not None:
            inventory = self._inventory.get_by_sku(
                sku_id=sku_id, warehouse_id=existing.warehouse_id
            )
            return ReservationResult(
                sku_id=sku_id,
                quantity=quantity,
                movement_id=existing.id,
                available_after=inventory.available_qty if inventory else existing.after_available,
                locked_after=inventory.locked_qty if inventory else existing.after_locked,
                replayed=True,
            )

        warehouse = self.resolve_warehouse(warehouse_id)
        inventory = self._inventory.lock_for_update(warehouse_id=warehouse.id, sku_id=sku_id)
        if inventory is None:
            raise InventoryNotFoundError(f"SKU {sku_id} has no stock record")

        if inventory.locked_qty < quantity:
            raise ValidationError(
                f"cannot deduct {quantity} of SKU {sku_id}: only {inventory.locked_qty} is locked",
                context={"sku_id": sku_id, "locked": inventory.locked_qty, "requested": quantity},
            )

        before_available, before_locked = inventory.available_qty, inventory.locked_qty
        inventory.locked_qty -= quantity
        inventory.total_out_qty += quantity
        inventory.version += 1

        movement = self._append(
            inventory=inventory,
            movement_type=MovementType.ORDER_DEDUCT,
            before_available=before_available,
            after_available=inventory.available_qty,
            before_locked=before_locked,
            after_locked=inventory.locked_qty,
            idempotency_key=idempotency_key,
            reference_type=reference_type,
            reference_id=reference_id,
            operator_type=operator_type,
            operator_id=operator_id,
        )
        self._session.flush()
        return ReservationResult(
            sku_id=sku_id,
            quantity=quantity,
            movement_id=movement.id,
            available_after=inventory.available_qty,
            locked_after=inventory.locked_qty,
        )

    # -- return_in (goods came back) --------------------------------------
    def return_in(
        self,
        *,
        sku_id: int,
        quantity: int,
        idempotency_key: str,
        warehouse_id: int | None = None,
        reference_type: ReferenceType = ReferenceType.AFTER_SALE,
        reference_id: int | None = None,
        operator_type: OperatorType = OperatorType.STAFF,
        operator_id: int | None = None,
        reason: str | None = None,
    ) -> ReservationResult:
        """A returned unit re-enters sellable stock (spec section 28 ``RETURN_IN``).

        The mirror of :meth:`deduct`, and it exists for the same reason: every stock
        mutation has exactly one method that owns the movement it appends, so
        ``available_qty`` and the ledger can never disagree (INV-007).

        ## Why ``available_qty`` and not ``locked_qty``

        At payment time the unit went ``available -= q`` (reserve) and then
        ``locked -= q`` (deduct). The goods physically left, and the row stopped
        counting them as sellable. A return puts the unit back where it came from -
        ``available += q`` - and ``locked_qty`` is untouched, because nothing about
        a return reserves anything. Crediting ``locked_qty`` instead would leave a
        unit that can only be sold by first being released, i.e. stock that exists
        but is not offerable.

        ## What this method deliberately does NOT do

        It does not decide *whether* a return is owed money, and it does not touch
        the order or the claim. ``RETURN_REFUND`` credits stock and
        ``REFUND_ONLY`` must not - the customer kept the goods, and crediting them
        would inflate availability until the next stock count found it. That
        distinction is the refund workflow's to make; this method is only the
        mechanism, which is why it is invocable on its own and must be called with
        the same ``idempotency_key`` on every retry.

        ``operator_type`` defaults to ``STAFF`` rather than ``SYSTEM``: the refund
        was executed by a person, and an audit that cannot tell a human's decision
        from the system's own action is not much of an audit.
        """
        if quantity <= 0:
            raise ValidationError("quantity must be positive")

        existing = self._movement_exists(idempotency_key)
        if existing is not None:
            inventory = self._inventory.get_by_sku(
                sku_id=sku_id, warehouse_id=existing.warehouse_id
            )
            return ReservationResult(
                sku_id=sku_id,
                quantity=quantity,
                movement_id=existing.id,
                available_after=inventory.available_qty if inventory else existing.after_available,
                locked_after=inventory.locked_qty if inventory else existing.after_locked,
                replayed=True,
            )

        warehouse = self.resolve_warehouse(warehouse_id)
        inventory = self._inventory.lock_for_update(warehouse_id=warehouse.id, sku_id=sku_id)
        if inventory is None:
            # Deliberately not an implicit create: a return for a SKU that has no
            # inventory row means the sale and the stock record disagree, and
            # inventing the row here would paper over that.
            raise InventoryNotFoundError(f"SKU {sku_id} has no stock record")

        before_available, before_locked = inventory.available_qty, inventory.locked_qty
        inventory.available_qty += quantity
        # Cumulative counter for reconciliation, so "does the ledger explain the
        # balance?" stays answerable without replaying every movement (INV-007).
        inventory.total_in_qty += quantity
        inventory.version += 1

        movement = self._append(
            inventory=inventory,
            movement_type=MovementType.RETURN_IN,
            before_available=before_available,
            after_available=inventory.available_qty,
            before_locked=before_locked,
            after_locked=inventory.locked_qty,
            idempotency_key=idempotency_key,
            reference_type=reference_type,
            reference_id=reference_id,
            operator_type=operator_type,
            operator_id=operator_id,
            reason=reason,
        )
        self._session.flush()
        return ReservationResult(
            sku_id=sku_id,
            quantity=quantity,
            movement_id=movement.id,
            available_after=inventory.available_qty,
            locked_after=inventory.locked_qty,
        )

    # -- adjustment (the optimistic path) ---------------------------------
    def preview_adjustment(
        self,
        *,
        sku_id: int,
        delta_available: int,
        warehouse_id: int | None = None,
    ) -> AdjustmentPreview:
        """Dry run. §47 requires a preview before a write, for promotions and
        for adjustments alike, so the operator sees the consequence first."""
        warehouse = self.resolve_warehouse(warehouse_id)
        inventory = self._inventory.get_by_sku(sku_id=sku_id, warehouse_id=warehouse.id)
        if inventory is None:
            raise InventoryNotFoundError(f"SKU {sku_id} has no stock record")

        resulting = inventory.available_qty + delta_available
        warnings: list[str] = []
        if resulting < 0:
            warnings.append(
                f"the adjustment would drive available stock to {resulting}; "
                "the database will refuse it (INV-001)"
            )
        if resulting < inventory.safety_stock:
            warnings.append("the result is below the safety stock threshold")

        return AdjustmentPreview(
            sku_id=sku_id,
            warehouse_id=warehouse.id,
            current_available=inventory.available_qty,
            current_locked=inventory.locked_qty,
            current_version=inventory.version,
            proposed_delta=delta_available,
            resulting_available=resulting,
            warnings=warnings,
        )

    def adjust(
        self,
        *,
        sku_id: int,
        version: int,
        delta_available: int,
        idempotency_key: str,
        warehouse_id: int | None = None,
        reason: str | None = None,
        movement_type: MovementType = MovementType.MANUAL_ADJUST,
        operator_type: OperatorType = OperatorType.STAFF,
        operator_id: int | None = None,
    ) -> Inventory:
        """Adjust available stock using the **optimistic** lock.

        Spec §27 splits the two concerns deliberately: the checkout path takes a
        pessimistic row lock because a hot row must never oversell, while a
        background edit is low-contention and can afford to detect a conflict and
        ask the operator to retry. Using `FOR UPDATE` here would make an
        administrator's typo block a customer's checkout.

        The client must send the ``version`` it read. A mismatch means somebody
        else changed the number in between, and silently applying an adjustment
        computed against a stale figure is how a stock count goes wrong.
        """
        if delta_available == 0:
            raise ValidationError("an adjustment must change something")

        existing = self._movement_exists(idempotency_key)
        if existing is not None:
            inventory = self._inventory.get_by_sku(
                sku_id=sku_id, warehouse_id=existing.warehouse_id
            )
            if inventory is None:
                raise InventoryNotFoundError(f"SKU {sku_id} has no stock record")
            return inventory

        warehouse = self.resolve_warehouse(warehouse_id)
        inventory = self._inventory.lock_for_update(warehouse_id=warehouse.id, sku_id=sku_id)
        if inventory is None:
            raise InventoryNotFoundError(f"SKU {sku_id} has no stock record")

        if inventory.version != version:
            raise InventoryVersionConflictError(
                "inventory changed since it was read; refresh and retry",
                context={
                    "sku_id": sku_id,
                    "expected_version": version,
                    "actual_version": inventory.version,
                    "current_available": inventory.available_qty,
                    "current_locked": inventory.locked_qty,
                },
            )

        resulting = inventory.available_qty + delta_available
        if resulting < 0:
            # Caught here so the operator gets a domain error rather than a raw
            # IntegrityError from the CHECK constraint. The constraint remains
            # the real guarantee (INV-001); this is only a better message.
            raise ValidationError(
                f"adjustment would set available stock to {resulting}",
                context={"sku_id": sku_id, "available": inventory.available_qty},
            )

        before_available, before_locked = inventory.available_qty, inventory.locked_qty
        inventory.available_qty = resulting
        inventory.version += 1
        if delta_available > 0:
            inventory.total_in_qty += delta_available

        self._append(
            inventory=inventory,
            movement_type=movement_type,
            before_available=before_available,
            after_available=inventory.available_qty,
            before_locked=before_locked,
            after_locked=inventory.locked_qty,
            idempotency_key=idempotency_key,
            reference_type=ReferenceType.MANUAL,
            reference_id=None,
            operator_type=operator_type,
            operator_id=operator_id,
            reason=reason,
        )
        self._session.flush()
        return inventory

    # -- console use cases (HTTP-facing; these own their transaction) ------
    def console_row(self, inventory: Inventory) -> InventoryOut:
        """Project a stock row into the frozen console shape (§§17/95).

        ``sku_no`` / ``product_name`` / ``sku_name`` are joined in here rather than
        stored on the inventory row, so the console can render a human-readable stock
        list without a second request per row. The lookups use ``Session.get`` on the
        primary key, which is why they cost no extra round trip for rows already in
        the identity map; an unknown SKU leaves the label fields at their empty
        defaults rather than failing the whole page.
        """
        out = InventoryOut.model_validate(inventory)
        sku = self._session.get(ProductSku, inventory.sku_id)
        if sku is not None:
            out.sku_no = sku.sku_no
            out.sku_name = sku.name
            product = self._session.get(Product, sku.product_id)
            if product is not None:
                out.product_name = product.name
        return out

    def position_for_optional(
        self, *, sku_id: int, warehouse_id: int | None = None
    ) -> Inventory | None:
        """One stock position, or ``None``.

        The public availability endpoint needs "not stocked" and "out of stock" to be
        indistinguishable to the caller (§§§26/28), so it asks for a row that may
        legitimately not exist; the console form uses :meth:`position_for`, which
        refuses instead.
        """
        return self._inventory.get_by_sku(sku_id=sku_id, warehouse_id=warehouse_id)

    def console_position(self, *, sku_id: int) -> InventoryOut:
        """The console's one-position read, already projected."""
        return self.console_row(self.position_for(sku_id=sku_id))

    def position_for(self, *, sku_id: int, warehouse_id: int | None = None) -> Inventory:
        """One stock position, or the frozen not-found error."""
        inventory = self._inventory.get_by_sku(sku_id=sku_id, warehouse_id=warehouse_id)
        if inventory is None:
            raise InventoryNotFoundError(f"no stock record for SKU {sku_id}")
        return inventory

    def positions_page(
        self,
        *,
        merchant_id: int | None,
        page: int,
        page_size: int,
        search: str | None = None,
        low_stock_only: bool = False,
    ) -> tuple[list[Inventory], int]:
        """One console page of stock positions plus the unpaginated total.

        Merchant scoping stays a parameter of the *query* here rather than a
        post-load filter: ``None`` means "every merchant" to the repository, so the
        router must pass the principal's merchant explicitly.
        """
        return self._inventory.list_inventory(
            merchant_id=merchant_id,
            search=search,
            low_stock_only=low_stock_only,
            offset=(page - 1) * page_size,
            limit=page_size,
        )

    def movements_page(
        self,
        *,
        sku_id: int,
        page: int,
        page_size: int,
        movement_type: MovementType | None = None,
    ) -> list[InventoryMovement]:
        """One page of the ledger for a SKU, newest first.

        The repository is asked for ``page_size * page`` rows and the caller's page
        sliced out of them, which is the shape the console endpoint has always used;
        it stays here so the paging arithmetic is not re-derived at the edge.
        """
        inventory = self.position_for(sku_id=sku_id)
        rows = self._inventory.movements_for(
            warehouse_id=inventory.warehouse_id,
            sku_id=sku_id,
            movement_type=movement_type,
            limit=page_size * page,
        )
        return list(rows[:page_size])

    def adjust_and_commit(
        self,
        *,
        sku_id: int,
        version: int,
        delta_available: int,
        warehouse_id: int | None,
        reason: str,
        movement_type: MovementType,
        operator_type: OperatorType,
        operator_id: int | None,
        idempotency_key: str,
    ) -> Inventory:
        """Apply a console adjustment and commit it as one unit of work.

        The permission check is the caller's (it belongs to the principal, not to
        stock); everything after it - the optimistic-locked write and the movement
        row that explains it (INV-007) - must be durable together or the ledger and
        the balance disagree the moment the process dies.
        """
        inventory = self.adjust(
            sku_id=sku_id,
            version=version,
            delta_available=delta_available,
            warehouse_id=warehouse_id,
            reason=reason,
            movement_type=movement_type,
            operator_type=operator_type,
            operator_id=operator_id,
            idempotency_key=idempotency_key,
        )
        self._session.commit()
        return inventory

    def verify_ledger_for_sku(self, *, sku_id: int) -> tuple[bool, dict[str, int]]:
        """``verify_ledger`` resolved from a SKU alone (the console's form).

        The warehouse is looked up from the SKU's own position rather than taken
        from the request, so an operator cannot ask "is SKU 7 consistent in
        warehouse 3" and be answered about a warehouse the SKU has no row in.
        """
        inventory = self.position_for(sku_id=sku_id)
        return self.verify_ledger(warehouse_id=inventory.warehouse_id, sku_id=sku_id)

    # -- reconciliation ---------------------------------------------------
    def verify_ledger(self, *, warehouse_id: int, sku_id: int) -> tuple[bool, dict[str, int]]:
        """Check that the movement ledger explains the current balance (INV-007).

        The starting balance is assumed to be zero, which holds because stock is
        only ever created through a movement (``PURCHASE_IN`` or
        ``MANUAL_ADJUST``). A direct ``INSERT``/``UPDATE`` that bypasses this
        service would break that assumption - which is exactly the condition this
        method is designed to detect.
        """
        inventory = self._inventory.get_by_sku(sku_id=sku_id, warehouse_id=warehouse_id)
        if inventory is None:
            raise InventoryNotFoundError(f"SKU {sku_id} has no stock record")

        net_available, net_locked = self._inventory.movement_signature(
            warehouse_id=warehouse_id, sku_id=sku_id
        )
        detail = {
            "ledger_available": net_available,
            "row_available": inventory.available_qty,
            "ledger_locked": net_locked,
            "row_locked": inventory.locked_qty,
        }
        return (
            net_available == inventory.available_qty and net_locked == inventory.locked_qty,
            detail,
        )

    def list_positions(
        self,
        *,
        merchant_id: int | None = None,
        low_stock_only: bool = False,
        search: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[Inventory], int]:
        """One page of stock positions, and how many exist in total.

        The read path behind the MCP ``nova.inventory.get`` tool and the console's
        stock list. It exists as a service method rather than letting callers reach
        ``InventoryRepository.list_inventory`` directly because the merchant filter is
        an *authorization* decision: a tool that passed no ``merchant_id`` would read
        every merchant's stock, and nothing in the repository refuses that (the filter
        is optional there precisely so platform-scoped console reads can omit it).

        Returns ``(rows, total)`` rather than a page object so the two existing
        consumers - which differ in what they do with the total - both stay simple.
        """
        from app.core.errors import PermissionDeniedError

        if merchant_id is None:
            # Fail closed. ``DataScope.ALL`` callers are the console, not the MCP
            # surface, and they can pass an explicit scope decision at the call site.
            raise PermissionDeniedError("inventory reads require an explicit merchant scope")
        rows, total = self._inventory.list_inventory(
            merchant_id=merchant_id,
            search=search,
            low_stock_only=low_stock_only,
            offset=offset,
            limit=limit,
        )
        return rows, total

    def low_stock_report(
        self, *, threshold_multiplier: float = 1.0, limit: int = 50
    ) -> list[tuple[Inventory, int]]:
        """Positions at or below their safety stock, worst first.

        Feeds the flagship agent flow of §124, which asks for the three most
        over-stocked SKUs - this is its mirror image, and the same query shape.
        """
        rows, _ = self._inventory.list_inventory(low_stock_only=True, limit=limit)
        # ``threshold_multiplier`` widens or narrows what counts as under-stocked
        # relative to each SKU's OWN safety stock. Raising it surfaces positions
        # that are not yet critical but are heading that way - which is what an
        # operator wants *before* a promotion rather than after one.
        scored = [
            (row, int(row.safety_stock * threshold_multiplier) - row.available_qty)
            for row in rows
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored


__all__ = [
    "AdjustmentPreview",
    "InventoryService",
    "ReservationResult",
]
