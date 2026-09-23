"""Order data access (spec section 18: a repository does data access and nothing else).

    OrderRepository, OrderStatusLogRepository, IdempotencyRepository

No pricing, no policy, no state-machine decisions live here. Whether a
``PENDING_PAYMENT`` order *may* be cancelled is decided by ``state_machine.py``;
whether a coupon is resolvable is decided in Phase 6's marketing module; whether
a cart has enough stock is decided by ``InventoryService``. This module answers
"what is in the database" and "write this row".

## Two methods are not ordinary queries, and are documented as such

:meth:`OrderRepository.get_by_order_no_for_update` is the pessimistic lock the
cancel and confirm-receipt paths need. Following the section 27 rule generalised
in ``HANDOFF.md`` section 7 - **decide inside the lock, not before it** - the
transition guard and the status write must happen while the row is locked, or two
concurrent cancels can both read ``PENDING_PAYMENT``, both decide "allowed", and
both release stock.

:meth:`IdempotencyRepository.insert_in_progress` is the serialisation point for
concurrent creates. It is the only method here that catches a database
exception, and the reason is a real race rather than defensive coding - see its
docstring.

## Append-only means append-only

``OrderStatusLogRepository`` has no ``update`` and no ``delete``. That is not an
oversight: spec section 36 makes the log the audit trail, and the cheapest way to
guarantee nobody mutates it is for the mutation to not exist. Corrections are
appended as a further transition, exactly as the stock ledger appends a
compensating movement.

## Locking clause

``with_for_update()`` is used rather than hand-written SQL, for the same reason as
in the inventory repository: the statement still goes through the ORM's mapper
configuration, and the architecture test that forbids raw SQL in the persistence
layer stays satisfiable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.modules.fulfillment.models import Fulfillment
from app.modules.order.enums import OrderStatus
from app.modules.order.models import Order, OrderItem, OrderStatusLog
from app.shared.db.base import utc_now
from app.shared.db.models.idempotency import IdempotencyRecord, IdempotencyStatus

__all__ = [
    "IdempotencyRepository",
    "OrderRepository",
    "OrderStatusLogRepository",
]

#: Scope namespace for order creation. Frozen by spec section 48 so that Phase 5
#: can use ``payment:callback`` without a collision when a client reuses one UUID.
ORDER_CREATE_SCOPE = "order:create"


class OrderRepository:
    """Queries over ``orders`` and ``order_items``, plus the insert path."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- writes ----------------------------------------------------------
    def add(self, order: Order) -> Order:
        """Persist a new order and flush so its id is available.

        The flush is not cosmetic: ``order_no`` is stamped from the
        auto-increment id afterwards, so the workflow needs the id before it can
        build the identifier. It happens inside the caller's transaction, which
        is also why the order-no UNIQUE constraint can be relied on - nothing is
        committed until the whole create succeeds.
        """
        self._session.add(order)
        self._session.flush()
        return order

    def add_item(self, item: OrderItem) -> OrderItem:
        self._session.add(item)
        self._session.flush()
        return item

    def add_status_log(self, log: OrderStatusLog) -> OrderStatusLog:
        """Persist an already-built status log row.

        A convenience for callers holding a constructed ``OrderStatusLog``. The
        preferred entry point remains :meth:`OrderStatusLogRepository.append`,
        which derives ``order_no`` from the order so a log cannot disagree with the
        order it describes; this method exists so a workflow that has already built
        the row (or is replaying one) does not have to reach for a second
        repository mid-transaction.
        """
        self._session.add(log)
        self._session.flush()
        return log

    def items_for(self, order_id: int) -> list[OrderItem]:
        """The persisted snapshot lines for one order, ordered by id.

        Returns rows from ``order_items`` only - it never joins the catalogue, so
        a caller that renders these values cannot accidentally read a live product
        name (INV-014). ``Order.items`` returns the same collection; this method
        exists for callers that hold only an id rather than an instance.
        """
        stmt = (
            select(OrderItem)
            .where(OrderItem.order_id == order_id)
            .order_by(OrderItem.id.asc())
        )
        return list(self._session.execute(stmt).scalars().all())


    def shipments_for(self, order_id: int) -> list[Fulfillment]:
        """The order's packages, oldest first, items eagerly loaded (section 5.1).

        The join table belongs to the fulfillment module, and this method is the
        order module's **only** reference to it. That direction is deliberate: the
        order owns the response field ``shipments[]`` (section 6), so the order
        read path is where the join belongs. The reverse edge - fulfillment
        reaching into orders - would make the dependency circular and is why the
        fulfillment module never imports this repository.

        Phase 4 needed no such query because no fulfillment table existed; the
        field was frozen and always ``[]``. Phase 5 fills it without a wire change,
        which is the whole reason the field was frozen early.

        No ``order_status`` filtering here: hiding a package because the order was
        cancelled would hide the one record that says a parcel left the warehouse.
        """
        stmt = (
            select(Fulfillment)
            .where(Fulfillment.order_id == order_id)
            .order_by(Fulfillment.id.asc())
            .options(selectinload(Fulfillment.items))
        )
        return list(self._session.execute(stmt).scalars().all())

    def touch_version(self, order: Order) -> None:
        """Increment the optimistic-locking counter (spec section 26).

        Used by paths that mutate an order they did not lock pessimistically. The
        status-machine paths lock instead, so this is for the Phase 5 writers that
        follow the counter-based scheme.
        """
        order.version = (order.version or 0) + 1
        self._session.flush()

    # -- reads -----------------------------------------------------------
    def get(self, order_id: int, *, for_update: bool = False) -> Order | None:
        """Load one order by primary key.

        ``for_update`` takes the row lock; it is a keyword rather than a second
        method so a caller cannot accidentally use the unlocked variant on a
        write path without the parameter being visible in the diff.
        """
        if not for_update:
            return self._session.get(Order, order_id)
        stmt = select(Order).where(Order.id == order_id).with_for_update()
        return self._session.execute(stmt).scalars().first()

    def get_by_order_no(
        self,
        order_no: str,
        *,
        merchant_id: int | None = None,
        user_id: int | None = None,
        for_update: bool = False,
    ) -> Order | None:
        """Resolve an order by its public identifier.

        ``user_id``/``merchant_id`` are applied **in the query**, not checked
        after loading. That ordering is the IDOR defence (spec section 14.6): a
        consumer asking for somebody else's order must get ``ORDER_NOT_FOUND
        (50003)``, and the only way to be certain that happens is for the foreign
        row never to be fetched. A post-load ownership check is one early
        ``return`` away from leaking existence.
        """
        stmt = select(Order).where(Order.order_no == order_no)
        if merchant_id is not None:
            stmt = stmt.where(Order.merchant_id == merchant_id)
        if user_id is not None:
            stmt = stmt.where(Order.user_id == user_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self._session.execute(stmt).scalars().first()

    def get_by_order_no_for_update(
        self, order_no: str, *, user_id: int | None = None
    ) -> Order | None:
        """Lock an order by its public identifier (section 27 rule).

        Named as its own method, rather than left to the ``for_update`` flag, so
        that a reviewer can see at the call site that a write path takes a lock.
        """
        return self.get_by_order_no(order_no, user_id=user_id, for_update=True)

    def get_by_client_request_id(
        self, *, user_id: int, client_request_id: str, for_update: bool = False
    ) -> Order | None:
        """The second idempotency guard: find the order a retry already created.

        Returns the order regardless of ``request_hash``; the caller compares the
        hash and raises ``IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD (10011)``
        when it differs. Keeping the comparison out of the query is deliberate -
        a hash mismatch and a missing row are different failures with different
        business codes, and collapsing them into "no row" would turn a client bug
        into a confusing duplicate order.
        """
        stmt = select(Order).where(
            Order.user_id == user_id,
            Order.client_request_id == client_request_id,
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self._session.execute(stmt).scalars().first()

    def list_customer_orders(
        self,
        *,
        user_id: int,
        order_status: OrderStatus | str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Order], int]:
        """One page of a customer's own orders, newest first (section 14.6).

        Returns ``(rows, total)`` so the list envelope's ``total`` does not need a
        second round trip in the API layer. ``created_at DESC`` is the frozen
        default order.
        """
        stmt = select(Order).where(Order.user_id == user_id)
        return self._paginate(stmt, order_status=order_status, page=page, page_size=page_size)

    def list_admin_orders(
        self,
        *,
        merchant_id: int | None = None,
        order_status: OrderStatus | str | None = None,
        payment_status: str | None = None,
        fulfillment_status: str | None = None,
        order_no: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Order], int]:
        """One page of the console list.

        ``merchant_id`` is mandatory in practice for a merchant-scoped console
        principal and is applied in the query (INV-004): the filter is what keeps
        a DataScope of ``MERCHANT`` from silently meaning "all merchants". It is
        optional only so a platform-scoped administrator can pass ``None``
        explicitly.
        """
        stmt = select(Order)
        if merchant_id is not None:
            stmt = stmt.where(Order.merchant_id == merchant_id)
        if payment_status is not None:
            stmt = stmt.where(Order.payment_status == _as_value(payment_status))
        if fulfillment_status is not None:
            stmt = stmt.where(Order.fulfillment_status == _as_value(fulfillment_status))
        if order_no is not None:
            stmt = stmt.where(Order.order_no == order_no)
        return self._paginate(stmt, order_status=order_status, page=page, page_size=page_size)

    def count_for_user(self, user_id: int) -> int:
        stmt = select(func.count()).select_from(Order).where(Order.user_id == user_id)
        return int(self._session.execute(stmt).scalar_one())

    # -- internals -------------------------------------------------------
    def _paginate(
        self,
        stmt: Select[Any],
        *,
        order_status: OrderStatus | str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[Order], int]:
        if order_status is not None:
            stmt = stmt.where(Order.order_status == _as_value(order_status))

        # Count from the filter chain, not from a fetched page: a page-size cap is
        # not a bound on how many orders a customer has.
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int(self._session.execute(count_stmt).scalar_one())

        offset = max(page - 1, 0) * page_size
        rows = (
            self._session.execute(
                stmt.order_by(Order.created_at.desc(), Order.id.desc())
                .limit(page_size)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return list(rows), total


class OrderStatusLogRepository:
    """Append-only access to ``order_status_logs`` (spec section 36).

    Deliberately has no ``update`` and no ``delete``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        *,
        order: Order,
        to_status: OrderStatus | str,
        from_status: OrderStatus | str | None = None,
        operator_type: str,
        operator_id: int | None = None,
        reason: str | None = None,
        trace_id: str | None = None,
    ) -> OrderStatusLog:
        """Append one transition row.

        ``order_no`` is copied from the order rather than passed in, so a log can
        never disagree with the order it describes. ``created_at`` is stamped here
        from :func:`utc_now` because the rows are read in insertion order within a
        transaction (all three share a millisecond otherwise).
        """
        log = OrderStatusLog(
            order_id=order.id,
            order_no=order.order_no,
            from_status=None if from_status is None else _as_value(from_status),
            to_status=_as_value(to_status),
            reason=reason,
            operator_type=operator_type,
            operator_id=operator_id,
            trace_id=trace_id,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self._session.add(log)
        self._session.flush()
        return log

    def list_for_order(self, order_id: int) -> list[OrderStatusLog]:
        """Full history, oldest first - the order a support agent reads it in."""
        stmt = (
            select(OrderStatusLog)
            .where(OrderStatusLog.order_id == order_id)
            .order_by(OrderStatusLog.created_at.asc(), OrderStatusLog.id.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def count_for_order(self, order_id: int) -> int:
        """Used by the integration tests to prove no extra transition was logged."""
        stmt = (
            select(func.count())
            .select_from(OrderStatusLog)
            .where(OrderStatusLog.order_id == order_id)
        )
        return int(self._session.execute(stmt).scalar_one())


class IdempotencyRepository:
    """Read/write access to the shared ``idempotency_records`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- reads -----------------------------------------------------------
    def get(self, *, scope: str, idempotency_key: str) -> IdempotencyRecord | None:
        stmt = select(IdempotencyRecord).where(
            IdempotencyRecord.scope == scope,
            IdempotencyRecord.idempotency_key == idempotency_key,
        )
        return self._session.execute(stmt).scalars().first()

    def get_for_update(self, *, scope: str, idempotency_key: str) -> IdempotencyRecord | None:
        """Read the claim under a row lock.

        Not needed for correctness on the *insert* path - the unique index
        already serialises that - but used by tests and by Phase 5 to wait for an
        in-flight claim rather than racing it.
        """
        stmt = (
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )
        return self._session.execute(stmt).scalars().first()

    # -- writes ----------------------------------------------------------
    def insert_in_progress(
        self,
        *,
        scope: str,
        idempotency_key: str,
        request_hash: str,
        resource_type: str | None = None,
        expires_at: datetime | None = None,
    ) -> IdempotencyRecord | None:
        """Claim ``(scope, key)``; return ``None`` if somebody else already holds it.

        ## Why this catches an exception instead of checking first

        The obvious implementation is ``get_if_exists() or insert()``, and it is
        wrong: two concurrent requests can both find nothing, both proceed to
        insert, and (because one of them loses at the unique index) only *one*
        create has to be correct for the test to pass while the other is a
        duplicate order in production. The database is the only place the
        decision can be made atomically, so this method *attempts the insert* and
        treats the unique-violation as the answer "you lost the race".

        ## Why a SAVEPOINT

        On MySQL, a failed statement does not abort the transaction, but
        SQLAlchemy does not know that and marks the session as needing a
        rollback - so catching the ``IntegrityError`` and carrying on with the
        same session would raise ``PendingRollbackError`` on the next statement.
        Wrapping the insert in ``begin_nested()`` confines the failure to a
        ``SAVEPOINT`` that can be rolled back on its own: the caller's transaction
        stays healthy and the caller can immediately re-read the winning record
        and replay it.

        (``HANDOFF.md`` section 6 records why this is worth spelling out rather
        than discovering: MySQL/Alembic behaviours here are non-obvious and were
        paid for once already.)

        The record is deliberately **not** committed here - spec section 49 puts
        the claim in the order's transaction, so a rollback leaves no key behind
        and a genuine retry after a real failure is allowed.
        """
        record = IdempotencyRecord(
            scope=scope,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            status=IdempotencyStatus.IN_PROGRESS.value,
            resource_type=resource_type,
            expires_at=expires_at,
        )
        try:
            with self._session.begin_nested():
                self._session.add(record)
                self._session.flush()
        except IntegrityError:
            # The unique index answered for us: this key is already claimed.
            return None
        return record

    def mark_completed(
        self,
        record: IdempotencyRecord,
        *,
        resource_type: str,
        resource_id: int,
        response_code: int,
        response_snapshot: dict | None = None,
    ) -> IdempotencyRecord:
        """Finalise a claim after the order has been written.

        ``response_snapshot`` must be a **non-sensitive** summary (spec section
        48) - this JSON is outside the order's own redaction path.
        """
        record.status = IdempotencyStatus.COMPLETED.value
        record.resource_type = resource_type
        record.resource_id = resource_id
        record.response_code = response_code
        record.response_snapshot = response_snapshot
        self._session.flush()
        return record

    def mark_failed(
        self,
        record: IdempotencyRecord,
        *,
        response_code: int | None = None,
        response_snapshot: dict | None = None,
    ) -> IdempotencyRecord:
        """Record a failure against a claim that is being kept.

        Rarely reached in the create path - an exception rolls the claim back with
        the order - but needed by callers that deliberately keep a failed claim
        visible (Phase 5 payment callbacks) so a retry can be told *why* the first
        attempt failed rather than being silently replayed as a success.
        """
        record.status = IdempotencyStatus.FAILED.value
        record.response_code = response_code
        record.response_snapshot = response_snapshot
        self._session.flush()
        return record


def _as_value(status: OrderStatus | str) -> str:
    """Normalise an enum member or a raw string to the stored ``VARCHAR`` value.

    Accepts both because the API layer works with enums while a row read from the
    database yields a plain string, and a comparison that silently fails on one
    of them is a filter that quietly returns everything.
    """
    return status.value if isinstance(status, OrderStatus) else str(status)
