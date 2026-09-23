"""Fulfillment use cases: the shell write, the ship path, and the reads.

    FulfillmentService

## ``create_shell`` - the one thing ``PaymentSuccessWorkflow`` calls

Design section 6.1 step 8: once payment has settled, the order needs the record that
says *the goods exist and are owed*. That is one ``fulfillments`` row in
``UNFULFILLED`` carrying one line per order line at full quantity, and it is a pure
ORM write - no inventory call, no commit, no transaction of its own. It joins the
caller's transaction, which is what makes the single-commit rule of 6.1 true.

Creating a package is **not** shipping it (6.1 step 9), so this deliberately does
**not** touch the order's ``fulfillment_status``. The axis stays ``UNFULFILLED``
until a parcel actually leaves.

## The ship path - where the cumulative rule lives

``POST /fulfillments/{id}/ship`` is the only place ``fulfillment_status`` moves, and
it is the only writer of the order's axis. Two rules shape this method:

* **Decide inside the lock.** The package is read ``FOR UPDATE`` first and every
  guard runs under that lock (``HANDOFF.md`` section 7). Two concurrent ship
  requests that both read ``UNFULFILLED`` before either decides would both ship, and
  the second would ship the same goods again.
* **The total is order-wide, never package-wide.** A per-package check passes while
  an order over-ships a line: three packages of one unit each, against a line whose
  quantity is two, looks valid package by package and ships three. The cumulative
  total comes from ``FulfillmentRepository.shipped_quantities_for_order``, which
  counts only packages that have actually shipped (an ``UNFULFILLED`` shell holds
  *planned* units, not shipped ones, so a fresh shell is never counted as a shipment)

## ``order_status`` is never written here

Not by accident, not as a convenience. ``order_status`` (``PENDING_PAYMENT``/
``PROCESSING``/``COMPLETED``/...) and ``fulfillment_status`` are independent axes
(spec section 31, REQ-ORD-003/005), so an order can be ``PROCESSING`` and
``SHIPPED`` at once and a ``COMPLETED`` order can still be ``PARTIAL_SHIPPED``. This
module contains no write to ``order_status`` and no import of the transition table;
the only column it moves is the fulfillment axis.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    FulfillmentAlreadyShippedError,
    FulfillmentNotFoundError,
    FulfillmentQuantityError,
    ValidationError,
)
from app.modules.fulfillment.models import Fulfillment, FulfillmentItem
from app.modules.fulfillment.repository import FulfillmentRepository
from app.modules.fulfillment.schemas import ShipFulfillmentRequest
from app.modules.identity.enums import PermissionCode
from app.modules.identity.service import Principal
from app.modules.order.enums import FULFILLMENT_STATUSES, FulfillmentStatus
from app.modules.order.models import Order, OrderItem
from app.shared.db.base import utc_now

__all__ = [
    "FULFILLMENT_READ_PERMISSIONS",
    "FULFILLMENT_SHIP_PERMISSIONS",
    "FulfillmentPage",
    "FulfillmentService",
    "compute_fulfillment_status",
    "compute_residual",
]


#: Ships are performed with either the specific task permission or the legacy
#: ``order:admin`` grant (an operator role that predates the fulfillment split still
#: has to be able to work the queue). ``require_permission`` takes *at least one*
#: of these, which is why it is a tuple.
FULFILLMENT_SHIP_PERMISSIONS: tuple[str, ...] = (
    PermissionCode.FULFILLMENT_SHIP.value,
    PermissionCode.ORDER_ADMIN.value,
)
FULFILLMENT_READ_PERMISSIONS: tuple[str, ...] = (
    PermissionCode.FULFILLMENT_READ.value,
    PermissionCode.ORDER_READ.value,
    PermissionCode.ORDER_ADMIN.value,
)


# ---------------------------------------------------------------------------
# Pure helpers - no session, no I/O, unit-tested directly
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class FulfillmentPage:
    """One page of the console queue plus the pre-slice total."""

    rows: list[Fulfillment]
    total: int


def compute_fulfillment_status(
    *,
    ordered: dict[int, int],
    shipped: dict[int, int],
    current: str | None = None,
) -> str:
    """The order's ``fulfillment_status``, recomputed from **all** its packages.

    The three-way rule of design section 6.3 step 4, expressed once so the ship path
    and any later reader cannot disagree:

    * every order line's cumulative shipped quantity ``>=`` its ordered quantity ->
      ``SHIPPED``;
    * something shipped but not everything -> ``PARTIAL_SHIPPED``;
    * nothing shipped -> ``UNFULFILLED``.

    ``current`` guards the one state the recomputation must not undo: **``DELIVERED``
    is terminal**. A delivered parcel that later gains another package (a reship, a
    returns flow) would otherwise drag the order backwards to ``SHIPPED``, telling
    the customer their delivered order is out for delivery again. Nothing else is
    sticky - a ``SHIPPED`` order that has a partial line added by a reship *should*
    read ``PARTIAL_SHIPPED``, because some of it has not arrived.

    Lines absent from ``shipped`` count as zero, and lines absent from ``ordered``
    are ignored: a package line whose order line no longer exists is a data defect,
    not a shipment of an invented quantity.
    """
    if current == FulfillmentStatus.DELIVERED.value:
        return FulfillmentStatus.DELIVERED.value

    if not ordered:
        # No lines to ship means nothing has shipped. Returning SHIPPED here would
        # make `all()` over an empty sequence true, which is the classic empty-set
        # trap: an order with no lines is unfulfilled, not fulfilled.
        return FulfillmentStatus.UNFULFILLED.value

    shipped_any = False
    for order_item_id, ordered_qty in ordered.items():
        gone = shipped.get(order_item_id, 0)
        if gone > 0:
            shipped_any = True
        if gone < ordered_qty:
            return (
                FulfillmentStatus.PARTIAL_SHIPPED.value
                if shipped_any
                else FulfillmentStatus.UNFULFILLED.value
            )
    return FulfillmentStatus.SHIPPED.value


def compute_residual(
    *,
    package_lines: dict[int, int],
    requested: dict[int, int],
) -> dict[int, int]:
    """What is left in a package after the requested units ship.

    Design 6.3 step 5: a partial shipment must leave the remainder shippable as a
    **new** package rather than dropping it.

    Iterates the **package's** lines, not the request's, and that direction is the
    whole correctness of the function. Iterating the request answers "how much of what
    I asked for did I not send" and therefore **loses every line the operator held
    back entirely** - the line is not in ``requested``, so it is never visited, so it
    appears in no residual package and the units it was owed simply cease to exist.
    Iterating the package asks the right question instead: "for each line this package
    planned to carry, how much is still owed?" A line that ships nothing is then owed
    its full quantity and lands in the residual, which is what a real split parcel
    looks like.

    Lines whose remainder is zero are dropped: a package line of zero units would
    violate ``CHECK (quantity > 0)`` on ``fulfillment_items``, so returning ``0``
    entries would turn a correct shipment into a database error at flush time.

    Raises :class:`~app.core.errors.ValidationError` if the request names a line this
    package does not carry, or asks for more units than the package holds. The
    workflow runs the same checks under the lock before calling this; the duplication
    is deliberate, because silently returning a wrong residual is worse than failing
    loudly, and a pure function cannot assume its caller was careful.
    """
    for order_item_id, quantity in requested.items():
        if order_item_id not in package_lines:
            raise ValidationError(
                f"order line {order_item_id} is not part of this package",
                context={"order_item_id": order_item_id},
            )
        if quantity > package_lines[order_item_id]:
            raise ValidationError(
                f"cannot ship {quantity} of order line {order_item_id}: "
                f"this package carries {package_lines[order_item_id]}",
                context={
                    "order_item_id": order_item_id,
                    "available": package_lines[order_item_id],
                    "requested": quantity,
                },
            )

    residual: dict[int, int] = {}
    for order_item_id, package_quantity in package_lines.items():
        remaining = package_quantity - requested.get(order_item_id, 0)
        if remaining > 0:
            residual[order_item_id] = remaining
    return residual


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------
class FulfillmentService:
    """Create the shell, ship a package, and answer the two frozen reads."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._fulfillments = FulfillmentRepository(session)

    # -- write: the shell (PaymentSuccessWorkflow step 8) ----------------
    def create_shell(
        self,
        *,
        order: Order,
        items: Sequence[OrderItem],
        warehouse_id: int,
    ) -> Fulfillment:
        """One ``UNFULFILLED`` package with one line per order line, at full quantity.

        Idempotent **per order**: an order that already has an unshipped shell returns
        that row instead of gaining a second one. The payment workflow is retried
        after a crash between its deduction step and its commit, and a retry that
        appended a second shell would tell the operator twice as much stock is owed
        - the order would look like it needs two shipments of everything. The
        repository's ``UNIQUE (merchant_id, fulfillment_no)`` cannot catch that (the
        second row would have a different identifier), so the guard is here.

        A retry that *raised* instead of returning would be worse, not stricter: the
        settlement retry would roll back the replayed deduction and leave a paid order
        holding locked stock with no package.

        ## The guard is check-then-act, so this method takes the order lock itself

        "Is there a shell?" and "insert one" are two statements, and no constraint
        covers the gap: a partial unique index (one ``UNFULFILLED`` row per order) is
        not expressible in MySQL 8, and ``uq_fulfillments_merchant_fulfillment_no``
        cannot help because two racing inserts carry *different* identifiers. Without
        serialisation two callers both read "no shell" and both insert - **measured:
        8 concurrent calls produced 8 shells and 8 item rows for one order.**

        It was previously serialised only by the payment workflow's
        ``SELECT ... FOR UPDATE`` on the order row, i.e. by a lock taken in *another
        module*. That holds for the settlement path (FG-11 exercises it under 10-way
        concurrency) but it is an invisible precondition: any future caller - an admin
        repair path, a re-ship tool, a data migration - that did not happen to lock the
        order first would silently create duplicate packages, and nothing would reject
        them.

        So the lock is taken **here**, on the order row, which makes the guard correct
        on its own terms rather than on its caller's discipline. It is a re-entrant
        no-op for the settlement path, which already holds that exact row lock in the
        same transaction (the lock is owned by the transaction, not the statement), so
        the hot path pays nothing and the wrong caller becomes impossible rather than
        merely discouraged. Locking the order first also matches the aggregate's own
        order: the order is the parent, and taking it before any child row keeps
        lock acquisition one-directional.

        ``warehouse_id`` is required and non-nullable on the table: it is the
        reconciliation target for the stock ledger (INV-007), and a package that
        cannot name its warehouse cannot be reconciled against the deduction. The
        caller has it in hand - it is the warehouse the deduction just happened in.

        Raises :class:`ValidationError` for an empty ``items`` sequence: a package
        with no lines ships nothing while claiming to be the record that goods are
        owed, which is a defect that would only surface at the ship endpoint as a
        request that can never be satisfied.
        """
        lines = list(items)
        if not lines:
            raise ValidationError(
                "a fulfillment shell needs at least one order line",
                context={"order_no": order.order_no},
            )

        # Serialise every caller of this method on the order row before deciding
        # anything (HANDOFF section 7: decide inside the lock, not before it). See the
        # docstring for why this belongs here rather than in the caller.
        self._lock_order(order.id)

        existing = self._unshipped_shell_for(order.id)
        if existing is not None:
            return existing

        now = utc_now()
        shell = Fulfillment(
            # Placeholder until the id exists: ``fulfillment_no`` is NOT NULL and
            # NOT NULL means "a value is required", not "the final value is required".
            # A uuid4 keeps two concurrent transactions from colliding on the
            # placeholder, and it is replaced before the commit, so no client ever
            # observes it. Same device as ``CreateOrderWorkflow._insert_order``.
            fulfillment_no=uuid4().hex,
            order_id=order.id,
            order_no=order.order_no,
            merchant_id=order.merchant_id,
            warehouse_id=warehouse_id,
            fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
            package_count=1,
            created_at=now,
            updated_at=now,
        )
        for line in lines:
            shell.items.append(
                FulfillmentItem(
                    order_item_id=line.id,
                    product_name=line.product_name,
                    sku_name=line.sku_name,
                    quantity=line.quantity,
                    created_at=now,
                    updated_at=now,
                )
            )

        self._fulfillments.add(shell)
        shell.fulfillment_no = f"{get_settings().FULFILLMENT_NO_PREFIX}{now:%Y%m%d}{shell.id:06d}"
        self._session.flush()
        return shell

    def ship(
        self,
        *,
        principal: Principal,
        fulfillment_id: int,
        payload: ShipFulfillmentRequest,
    ) -> Fulfillment:
        """Ship (part of) a package: the only writer of ``fulfillment_status``.

        Order of operations is load-bearing (design 6.3):

        1. authorise, then load the package ``FOR UPDATE`` - the lock comes before
           any decision, so two concurrent requests cannot both pass the
           "already shipped?" guard;
        2. refuse an already-shipped package with ``FULFILLMENT_ALREADY_SHIPPED``
           (70 003) and a foreign or missing one with ``FULFILLMENT_NOT_FOUND``
           (70 000) - a foreign row is never fetched, so merchant scope is applied in
           the query rather than checked after loading (INV-004);
        3. validate the request against **the order**: every named line must be in
           this package, and this request's units added to the order-wide cumulative
           total must not exceed the ordered quantity (70 001);
        4. stamp carrier / tracking / ``shipped_at``, shrink the package's lines to
           what actually left;
        5. create the residual package for anything held back - in the same
           transaction, or the remainder would simply be lost;
        6. recompute the order's axis from **all** packages. ``order_status`` is not
           touched here and has no writer in this module.
        """
        self._assert_can_ship(principal)
        fulfillment, order = self._resolve_locked_package(principal=principal, fulfillment_id=fulfillment_id)

        if fulfillment.is_shipped:
            raise FulfillmentAlreadyShippedError(
                "this package has already been shipped",
                context={
                    "fulfillment_id": fulfillment.id,
                    "fulfillment_no": fulfillment.fulfillment_no,
                    "fulfillment_status": fulfillment.fulfillment_status,
                },
            )

        requested = {line.order_item_id: line.quantity for line in payload.item_quantities}
        package_lines = {item.order_item_id: item.quantity for item in fulfillment.items}
        if not package_lines:
            # Defensive: a package with no lines cannot satisfy a request that names
            # any line, and `compute_residual` would report that as a validation error
            # on the first requested line. Failing here names the real cause.
            raise FulfillmentQuantityError(
                "this package has no lines to ship",
                context={"fulfillment_id": fulfillment.id},
            )

        ordered = {item.id: item.quantity for item in order.items}
        order_wide_shipped = self._fulfillments.shipped_quantities_for_order(order.id)
        self._assert_quantities_within_bounds(
            requested=requested,
            package_lines=package_lines,
            ordered=ordered,
            already_shipped=order_wide_shipped,
        )

        residual = compute_residual(package_lines=package_lines, requested=requested)

        # -- step 4: stamp the shipment facts ------------------------------
        now = utc_now()
        self._fulfillments.stamp_shipped(
            fulfillment,
            carrier=payload.carrier,
            tracking_no=payload.tracking_no,
            shipped_at=now,
        )

        # Make the package say what actually left, and nothing else.
        #
        # `fulfillment_items.quantity` is the *package line*, not a shipped counter
        # (models.py): one number, one meaning. So a line that shipped fewer units than
        # planned is reduced to what left, and a line held back entirely is removed -
        # it was never in this parcel, and leaving it here would put goods in a
        # "shipped" record. Whatever is still owed is carried by the residual package,
        # built from `package_lines` below (the plan captured before this mutation).
        #
        # The removal is a `session.delete` on purpose. Removing the child from the
        # relationship alone would not remove the row, and a leftover row would keep
        # claiming units in a package whose line the residual also carries - the
        # `UNIQUE (fulfillment_id, order_item_id)` constraint cannot catch that, since
        # the two rows live in different packages.
        for item in list(fulfillment.items):
            shipped_here = requested.get(item.order_item_id)
            if shipped_here is None:
                fulfillment.items.remove(item)
                self._session.delete(item)
                continue
            item.quantity = shipped_here
            item.updated_at = now
        self._session.flush()

        # A package that shipped nothing would be a lie: its `shipment_data_required`
        # CHECK would be satisfied while the customer sees a "shipped" parcel with no
        # goods in it.
        if not fulfillment.items:
            raise FulfillmentQuantityError(
                "a shipment must contain at least one line of this package",
                context={"fulfillment_id": fulfillment.id},
            )

        # -- step 5: the residual package ---------------------------------
        if residual:
            self._create_residual_package(
                order=order,
                warehouse_id=fulfillment.warehouse_id,
                planned=package_lines,
                residual=residual,
                source_items={item.order_item_id: item for item in fulfillment.items},
                now=now,
            )

        # -- step 6: the axis, from all packages --------------------------
        #
        # Order matters twice here. The package's ``SHIPPED`` write must be flushed
        # BEFORE the axis is recomputed, because the shipped total is now a SQL
        # aggregate filtered on ``fulfillment_status IN ('SHIPPED','DELIVERED')``: it can
        # only see rows the database has, and this write is still pending in the session.
        # Flushing afterwards instead made the aggregate return ``{}`` for a package that
        # had just shipped, so a successful partial shipment left the order reading
        # ``UNFULFILLED``. (The earlier in-memory implementation summed ``package.items``
        # and so was immune - which is exactly why moving the total into the query
        # introduced this, and why the integration tests caught it.)
        fulfillment.fulfillment_status = FulfillmentStatus.SHIPPED.value
        self._session.flush()

        order.fulfillment_status = self._recompute_order_axis(order)
        self._session.flush()
        return fulfillment

    def sku_by_line_for_orders(self, order_ids: Sequence[int]) -> dict[int, int]:
        """``{order_item_id: sku_id}`` for the given orders, in **one** query.

        ``fulfillment_items`` does not store ``sku_id`` (``models.py``: duplicating it
        would create a second place for the same fact to be wrong), but the frozen wire
        shape carries it (``API_CONTRACT`` section 5), so the read paths resolve it from
        ``order_items`` - the authoritative place for it by definition, since the SKU is
        a property of what was *ordered*, not of how it was packed.

        Batched across every order on the page rather than resolved per line. The
        alternative is an N+1 that stays invisible until an order ships in five
        packages of four lines each, at which point one console page issues twenty
        extra queries and nobody can see why.

        An empty sequence returns ``{}`` without touching the database.
        """
        ids = list(order_ids)
        if not ids:
            return {}
        stmt = select(OrderItem.id, OrderItem.sku_id).where(OrderItem.order_id.in_(ids))
        return {int(row[0]): int(row[1]) for row in self._session.execute(stmt).all()}

    def get_for_order_no(self, *, principal: Principal, order_no: str) -> list[Fulfillment]:
        """The consumer's packages for one order.

        Ownership is in the query: a consumer only ever sees their own order, and the
        merchant scope of a staff principal is applied at the same time. A foreign
        order therefore answers ``ORDER_NOT_FOUND`` - the row is never loaded, which
        is what makes this IDOR-safe rather than IDOR-tested.
        """
        from app.core.errors import OrderNotFoundError
        from app.modules.order.repository import OrderRepository

        order = OrderRepository(self._session).get_by_order_no(order_no)
        if order is None:
            raise OrderNotFoundError("order not found")
        if principal.is_staff:
            if principal.merchant_id is not None and order.merchant_id != principal.merchant_id:
                raise OrderNotFoundError("order not found")
        elif order.user_id != principal.user_id:
            raise OrderNotFoundError("order not found")
        return self._fulfillments.list_for_order(order.id)

    def list_admin_fulfillments(
        self,
        *,
        principal: Principal,
        order_no: str | None = None,
        fulfillment_status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> FulfillmentPage:
        """One page of the console fulfillment queue (API_CONTRACT section 5.2).

        The status filter is validated against the frozen vocabulary, so an
        unmeetable filter is a validation error rather than an empty page: an
        operator who sees an empty queue concludes there is nothing to ship and acts
        on it.
        """
        self._assert_can_read(principal)
        if fulfillment_status is not None and fulfillment_status not in FULFILLMENT_STATUSES:
            raise ValidationError(
                f"unknown fulfillment_status {fulfillment_status!r}",
                context={"allowed": list(FULFILLMENT_STATUSES)},
            )
        rows, total = self._fulfillments.list_admin_fulfillments(
            merchant_id=principal.merchant_id,
            fulfillment_status=fulfillment_status,
            order_no=order_no,
            page=page,
            page_size=page_size,
        )
        return FulfillmentPage(rows=rows, total=total)

    # -- internals -------------------------------------------------------
    def _lock_order(self, order_id: int) -> None:
        """Take ``SELECT ... FOR UPDATE`` on the order row for this transaction.

        Called by ``create_shell`` before its check-then-act guard, so concurrent
        callers are serialised whether or not they took the lock themselves. A
        re-entrant no-op when the caller already holds it, because row locks belong to
        the transaction rather than to the statement - which is why this is safe to add
        under the settlement path.

        Deliberately re-reads the row rather than trusting the ``order`` instance the
        caller passed: an uncommitted or stale instance is not evidence that the
        database agrees, and the point of the lock is to make the *database's* view
        authoritative for the duplicate check that follows.
        """
        from app.modules.order.repository import OrderRepository

        OrderRepository(self._session).get(order_id, for_update=True)

    def _unshipped_shell_for(self, order_id: int) -> Fulfillment | None:
        """The order's existing ``UNFULFILLED`` shell, if it has one.

        ``UNFULFILLED`` specifically: a package that has shipped is not a shell, and
        returning it would let the payment retry path hand back a shipped parcel as
        if it were the record of goods still owed.
        """
        for row in self._fulfillments.list_for_order(order_id):
            if row.fulfillment_status == FulfillmentStatus.UNFULFILLED.value:
                return row
        return None

    def _assert_can_ship(self, principal: Principal) -> None:
        from app.core.errors import PermissionDeniedError

        if not principal.is_staff:
            raise PermissionDeniedError("shipping is a merchant staff action")
        if not principal.has_any_permission(*FULFILLMENT_SHIP_PERMISSIONS):
            raise PermissionDeniedError(
                "missing a required permission",
                context={"required_any_of": list(FULFILLMENT_SHIP_PERMISSIONS)},
            )

    def _assert_can_read(self, principal: Principal) -> None:
        from app.core.errors import PermissionDeniedError

        if not principal.is_staff:
            raise PermissionDeniedError("the fulfillment queue is a console surface")
        if not principal.has_any_permission(*FULFILLMENT_READ_PERMISSIONS):
            raise PermissionDeniedError(
                "missing a required permission",
                context={"required_any_of": list(FULFILLMENT_READ_PERMISSIONS)},
            )

    def _resolve_locked_package(
        self, *, principal: Principal, fulfillment_id: int
    ) -> tuple[Fulfillment, Order]:
        """Lock the package and load its order, both scoped to the caller.

        The merchant check happens **after** the lock but before any decision: the
        lock is keyed on the primary key, which the caller supplied, so the scope must
        be asserted while the row is held. A foreign package is reported as
        ``FULFILLMENT_NOT_FOUND`` rather than as a permission error - telling a caller
        that a row exists but is not theirs is itself a disclosure.
        """
        from app.core.errors import OrderNotFoundError
        from app.modules.order.repository import OrderRepository

        fulfillment = self._fulfillments.get_for_update(fulfillment_id)
        if fulfillment is None:
            raise FulfillmentNotFoundError(
                "fulfillment not found", context={"fulfillment_id": fulfillment_id}
            )
        if principal.merchant_id is not None and fulfillment.merchant_id != principal.merchant_id:
            raise FulfillmentNotFoundError(
                "fulfillment not found", context={"fulfillment_id": fulfillment_id}
            )

        orders = OrderRepository(self._session)
        order = orders.get(fulfillment.order_id, for_update=True)
        if order is None:
            # A package whose order is gone is impossible under the RESTRICT FK; if it
            # happens, the row is corrupt rather than missing and saying so beats an
            # AttributeError deep in the rollup.
            raise OrderNotFoundError(
                "the order this package belongs to no longer exists",
                context={"fulfillment_id": fulfillment.id, "order_id": fulfillment.order_id},
            )
        return fulfillment, order

    # ``_shipped_quantities`` stood here until ``104ef26`` committed the repository's
    # status filter, and the comment is kept rather than the method because the history
    # is the guard rail.
    #
    # It existed for one reason: ``shipped_quantities_for_order`` summed every package
    # with **no** status filter, so on a fresh order it returned the ``UNFULFILLED``
    # shell's *planned* units as though they had shipped (measured: one shell of qty=3
    # against a ``quantity=3`` line reported 3 shipped when the truth was 0), and the
    # cumulative guard then refused the order's **first legal shipment** with 70001 -
    # the endpoint was unusable while every service-level test stayed green.
    #
    # The filter now lives in the query where the rule belongs, so this module reads the
    # repository instead of maintaining a second implementation of "how much has gone
    # out"; two of those is exactly how an order reads ``SHIPPED`` while its last
    # shipment was refused. The guard and ``_recompute_order_axis`` share the one query,
    # so they still cannot disagree, and
    # ``tests/integration/fulfillment/test_reads_and_serialisation.py`` fails if the
    # filter is ever removed - which is what makes this retirement safe rather than tidy.
    def _assert_quantities_within_bounds(
        self,
        *,
        requested: dict[int, int],
        package_lines: dict[int, int],
        ordered: dict[int, int],
        already_shipped: dict[int, int],
    ) -> None:
        """The two quantity guards, both under the package lock.

        ``FULFILLMENT_QUANTITY_EXCEEDS_ORDER`` (70 001) covers both failures because
        the operator's remedy is the same either way (ask for fewer units), and the
        ``context`` distinguishes them for the client:

        * a line this package does not carry at all;
        * more units than the package holds;
        * more units, cumulatively across every package, than the order line's
          quantity - the rule that a per-package check would miss.
        """
        for order_item_id, quantity in requested.items():
            package_quantity = package_lines.get(order_item_id)
            if package_quantity is None:
                raise FulfillmentQuantityError(
                    "this package does not carry that order line",
                    context={
                        "order_item_id": order_item_id,
                        "package_lines": sorted(package_lines),
                    },
                )
            if quantity > package_quantity:
                raise FulfillmentQuantityError(
                    f"cannot ship {quantity} of order line {order_item_id}: "
                    f"this package carries {package_quantity}",
                    context={
                        "order_item_id": order_item_id,
                        "package_quantity": package_quantity,
                        "requested": quantity,
                    },
                )
            ordered_quantity = ordered.get(order_item_id)
            if ordered_quantity is None:
                raise FulfillmentQuantityError(
                    "that order line does not belong to this order",
                    context={"order_item_id": order_item_id, "order_id": None},
                )
            cumulative = already_shipped.get(order_item_id, 0) + quantity
            if cumulative > ordered_quantity:
                raise FulfillmentQuantityError(
                    f"shipping {quantity} of order line {order_item_id} would ship "
                    f"{cumulative} in total, but the order line is {ordered_quantity}",
                    context={
                        "order_item_id": order_item_id,
                        "ordered_quantity": ordered_quantity,
                        "already_shipped": already_shipped.get(order_item_id, 0),
                        "requested": quantity,
                        "cumulative": cumulative,
                    },
                )

    def _create_residual_package(
        self,
        *,
        order: Order,
        warehouse_id: int,
        planned: dict[int, int],
        residual: dict[int, int],
        source_items: dict[int, FulfillmentItem],
        now: Any,
    ) -> Fulfillment:
        """The remainder, as a new package in the same transaction (6.3 step 5).

        The name snapshots are resolved in this order:

        1. from the shipped package's own line, when that line was **partially**
           shipped - it is still there, reduced to what left, and carries the same
           snapshot the plan carried;
        2. otherwise from ``order_items``, when the line was **held back entirely** -
           it was removed from the shipped package, so its snapshot lives only on the
           order line (INV-014: the order line's names are frozen at creation, so this
           is the same string, not a fresh catalogue read).

        Building the residual from ``planned`` rather than from what is left in the
        shipped package is what makes a held-back line survive: after the shrink, the
        shipped package no longer knows the quantity that line was *owed*, and a
        residual computed from it would silently lose the units.
        """
        residual_package = Fulfillment(
            fulfillment_no=uuid4().hex,
            order_id=order.id,
            order_no=order.order_no,
            merchant_id=order.merchant_id,
            warehouse_id=warehouse_id,
            fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
            package_count=1,
            remark="residual of a partial shipment",
            created_at=now,
            updated_at=now,
        )
        for order_item_id, quantity in residual.items():
            template = source_items.get(order_item_id)
            if template is not None:
                product_name, sku_name = template.product_name, template.sku_name
            else:
                fallback = next((line for line in order.items if line.id == order_item_id), None)
                if fallback is None:
                    raise ValidationError(
                        "cannot build a residual package for an unknown order line",
                        context={
                            "order_item_id": order_item_id,
                            "planned": planned.get(order_item_id),
                        },
                    )
                product_name, sku_name = fallback.product_name, fallback.sku_name
            residual_package.items.append(
                FulfillmentItem(
                    order_item_id=order_item_id,
                    product_name=product_name,
                    sku_name=sku_name,
                    quantity=quantity,
                    created_at=now,
                    updated_at=now,
                )
            )
        self._fulfillments.add(residual_package)
        residual_package.fulfillment_no = (
            f"{get_settings().FULFILLMENT_NO_PREFIX}{now:%Y%m%d}{residual_package.id:06d}"
        )
        self._session.flush()
        return residual_package

    def _recompute_order_axis(self, order: Order) -> str:
        """The order's axis, from every package it has (6.3 step 4).

        Re-read through the repository rather than derived from the two rows this
        method just touched: the axis is a property of *all* the order's packages, and
        a computation that only knew about the ones in memory would report
        ``PARTIAL_SHIPPED`` for an order whose other packages already shipped
        everything.
        """
        ordered = {item.id: item.quantity for item in order.items}
        return compute_fulfillment_status(
            ordered=ordered,
            shipped=self._fulfillments.shipped_quantities_for_order(order.id),
            current=order.fulfillment_status,
        )
