"""Fulfillment data access (spec section 18: a repository does data access, nothing else).

    FulfillmentRepository

No carrier validation, no cumulative-quantity arithmetic, no ``fulfillment_status``
decision lives here. Whether a requested quantity exceeds what the order line
still has coming (``FULFILLMENT_QUANTITY_EXCEEDS_ORDER``, 70001) is decided by
``ShipWorkflow`` **while the package is locked**; whether a carrier code is known
is decided at the schema edge; whether the order's axis should become
``SHIPPED``/``PARTIAL_SHIPPED`` is decided by the workflow from **all** packages.
This module answers "what is in the database" and "write this row".

## The one lock in this module

:meth:`FulfillmentRepository.get_for_update` is the pessimistic lock the ship path
needs. Following the rule generalised in ``HANDOFF.md`` section 7 - **decide
inside the lock, not before it** - the "is this package already shipped?" guard
(``FULFILLMENT_ALREADY_SHIPPED``, 70003) and the ``SHIPPED`` write must happen
under one lock, or two concurrent ship requests can both read ``UNFULFILLED``,
both decide "ship it", and both deduct stock.

## Two aggregates, and why they are named the way they are

:meth:`FulfillmentRepository.shipped_quantities_for_order` answers the cumulative
rule's question - how many units of each order line have already **left**, across
every package of the order - in one query, so the caller cannot accidentally answer
it per package. The join through ``fulfillments`` is what makes the total order-wide
rather than package-wide; a per-package total would pass every check while
over-shipping the line. It filters on ``SHIPPED_STATUSES``, because a shell in
``UNFULFILLED`` carries the order's *planned* units and counting those as shipped
made the ship endpoint refuse every legal first shipment - see its docstring for the
full defect.

:meth:`FulfillmentRepository.planned_quantities_for_order` is the same aggregate
**without** that filter. Two methods rather than one boolean argument, because a
flag would leave the dangerous reading as the default at some call sites and the
whole point is that the distinction cannot be forgotten.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from app.modules.fulfillment.enums import FulfillmentStatus
from app.modules.fulfillment.models import Fulfillment, FulfillmentItem

__all__ = ["FulfillmentRepository"]

#: The package statuses that mean goods have actually left. ``DELIVERED`` implies
#: shipped (the ``delivered_after_shipped`` CHECK enforces it), so both count.
#:
#: This constant exists because its absence was a real defect - see
#: :meth:`FulfillmentRepository.shipped_quantities_for_order`.
SHIPPED_STATUSES: tuple[str, ...] = (
    FulfillmentStatus.SHIPPED.value,
    FulfillmentStatus.DELIVERED.value,
)


class FulfillmentRepository:
    """Queries over ``fulfillments`` / ``fulfillment_items`` plus the write paths."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- writes ----------------------------------------------------------
    def add(self, fulfillment: Fulfillment) -> Fulfillment:
        """Persist a new package and flush so its id is available.

        The flush is load-bearing: ``fulfillment_no`` is stamped from the
        auto-increment id afterwards, so the caller needs the id before it can
        build the identifier. It happens inside the caller's transaction, which is
        why ``uq_fulfillments_merchant_fulfillment_no`` can be relied on - nothing
        is committed until the whole workflow succeeds.
        """
        self._session.add(fulfillment)
        self._session.flush()
        return fulfillment

    def add_item(self, item: FulfillmentItem) -> FulfillmentItem:
        self._session.add(item)
        self._session.flush()
        return item

    def stamp_shipped(
        self,
        fulfillment: Fulfillment,
        *,
        carrier: str,
        tracking_no: str,
        shipped_at: datetime,
    ) -> Fulfillment:
        """Write the shipment facts onto an already-locked row (section 6.3 step 3).

        Deliberately does **not** set ``fulfillment_status``: the status is derived
        by the workflow from the shipped quantity, and having this method guess it
        would create a second place for the derivation to be wrong. The
        ``shipment_data_required`` CHECK means this and the status write must both
        happen before the flush that ends the transaction, which is exactly the
        order the workflow uses.
        """
        fulfillment.carrier = carrier
        fulfillment.tracking_no = tracking_no
        fulfillment.shipped_at = shipped_at
        self._session.flush()
        return fulfillment

    # -- reads -----------------------------------------------------------
    def get(self, fulfillment_id: int, *, for_update: bool = False) -> Fulfillment | None:
        """Load one package by primary key.

        ``for_update`` is a keyword rather than a second method so a caller cannot
        accidentally use the unlocked variant on a write path without the parameter
        being visible in the diff.
        """
        if not for_update:
            return self._session.get(Fulfillment, fulfillment_id)
        stmt = select(Fulfillment).where(Fulfillment.id == fulfillment_id).with_for_update()
        return self._session.execute(stmt).scalars().first()

    def get_for_update(self, fulfillment_id: int) -> Fulfillment | None:
        """Lock a package by primary key (section 27 rule).

        Named as its own method, rather than left to the ``for_update`` flag, so a
        reviewer can see at the call site that the ship path takes a lock.
        """
        return self.get(fulfillment_id, for_update=True)

    def get_by_fulfillment_no(
        self,
        fulfillment_no: str,
        *,
        merchant_id: int | None = None,
        for_update: bool = False,
    ) -> Fulfillment | None:
        """Resolve a package by its public identifier, merchant-scoped in the query.

        The scope is applied **in the query**, not checked after loading, for the
        same IDOR reason as everywhere else: a foreign row must never be fetched.
        """
        stmt = select(Fulfillment).where(Fulfillment.fulfillment_no == fulfillment_no)
        if merchant_id is not None:
            stmt = stmt.where(Fulfillment.merchant_id == merchant_id)
        if for_update:
            stmt = stmt.with_for_update()
        return self._session.execute(stmt).scalars().first()

    def list_for_order(self, order_id: int) -> list[Fulfillment]:
        """Every package of one order, oldest first.

        This is the query the order's ``fulfillment_status`` recomputation reads
        (section 6.3 step 4): the axis is derived from **all** packages, never from
        the one that was just shipped.
        """
        stmt = (
            select(Fulfillment)
            .where(Fulfillment.order_id == order_id)
            .order_by(Fulfillment.created_at.asc(), Fulfillment.id.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def list_for_order_no(self, order_no: str, *, merchant_id: int | None = None) -> list[Fulfillment]:
        """The customer's and console's view of an order's packages.

        Scoped by merchant when a merchant is given, so a console principal cannot
        read another tenant's shipments by guessing an order number.
        """
        stmt = select(Fulfillment).where(Fulfillment.order_no == order_no)
        if merchant_id is not None:
            stmt = stmt.where(Fulfillment.merchant_id == merchant_id)
        stmt = stmt.order_by(Fulfillment.created_at.asc(), Fulfillment.id.asc())
        return list(self._session.execute(stmt).scalars().all())

    def list_admin_fulfillments(
        self,
        *,
        merchant_id: int | None = None,
        fulfillment_status: str | None = None,
        order_no: str | None = None,
        carrier: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[Fulfillment], int]:
        """One page of the console queue (API contract section 5).

        ``merchant_id`` is mandatory in practice for a merchant-scoped principal and
        is applied in the query (INV-004): the filter is what keeps a DataScope of
        ``MERCHANT`` from silently meaning "all merchants".
        """
        stmt = select(Fulfillment)
        if merchant_id is not None:
            stmt = stmt.where(Fulfillment.merchant_id == merchant_id)
        if fulfillment_status is not None:
            stmt = stmt.where(Fulfillment.fulfillment_status == _as_value(fulfillment_status))
        if order_no is not None:
            stmt = stmt.where(Fulfillment.order_no == order_no)
        if carrier is not None:
            stmt = stmt.where(Fulfillment.carrier == _as_value(carrier))
        return self._paginate(stmt, page=page, page_size=page_size)

    def items_for(self, fulfillment_id: int) -> list[FulfillmentItem]:
        """The package's lines, ordered by id."""
        stmt = (
            select(FulfillmentItem)
            .where(FulfillmentItem.fulfillment_id == fulfillment_id)
            .order_by(FulfillmentItem.id.asc())
        )
        return list(self._session.execute(stmt).scalars().all())

    def get_item(self, *, fulfillment_id: int, order_item_id: int) -> FulfillmentItem | None:
        """One line of one package. Returns ``None`` when the line is not in it -
        which is how the ship path refuses a request for a line this package does
        not carry."""
        stmt = select(FulfillmentItem).where(
            FulfillmentItem.fulfillment_id == fulfillment_id,
            FulfillmentItem.order_item_id == order_item_id,
        )
        return self._session.execute(stmt).scalars().first()

    # -- the cumulative rule ---------------------------------------------
    def shipped_quantities_for_order(self, order_id: int) -> dict[int, int]:
        """``{order_item_id: total units shipped}`` across **every** package.

        The cumulative rule of design section 5.4 needs the total over the order, so
        this joins ``fulfillment_items`` to ``fulfillments`` and groups by line. The
        join is the point: summing ``fulfillment_items.quantity`` alone would total
        *planned* units, and summing one package's items would miss the others -
        both of which pass a naive per-package check while over-shipping the order.

        Lines that appear in no package are absent from the mapping rather than
        present as zero, so a caller that forgets ``.get(..., 0)`` gets a ``KeyError``
        instead of a silently wrong total.

        ## The status filter, and the defect its absence caused

        Only packages that have **actually shipped** are counted. This filter is
        load-bearing and was **missing in the first version of this method** - a real
        defect, found by ``fulfillment-workflow`` while writing the ship path, not by
        any test of mine:

        * ``PaymentSuccessWorkflow`` step 8 creates a shell in ``UNFULFILLED`` that
          carries the order's **planned** units - one line per order line at full
          quantity;
        * without a status filter this method summed those planned units and returned
          them as *shipped*. On a fresh order, one shell with ``quantity=3`` against a
          ``quantity=3`` order line reported ``3`` shipped when the truth is ``0``;
        * the cumulative guard then computed ``3 + 2 > 3`` and refused a perfectly
          legal shipment of two units - so with a full-width shell, **the first
          shipment of any line was refused and the ship endpoint could never be used.**

        The lesson worth keeping is about the *name*, not the SQL: a method called
        ``shipped_*`` that returns planned units is worse than no method at all,
        because a caller has no reason to doubt it. ``FulfillmentService
        ._shipped_quantities`` carries the same filter independently, so the 70001
        guard and the order's axis cannot disagree about how much has gone out.
        """
        stmt = (
            select(
                FulfillmentItem.order_item_id,
                func.sum(FulfillmentItem.quantity),
            )
            .join(Fulfillment, Fulfillment.id == FulfillmentItem.fulfillment_id)
            .where(
                Fulfillment.order_id == order_id,
                Fulfillment.fulfillment_status.in_(SHIPPED_STATUSES),
            )
            .group_by(FulfillmentItem.order_item_id)
        )
        return {int(row[0]): int(row[1]) for row in self._session.execute(stmt).all()}

    def shipped_quantity_for_order_item(self, order_item_id: int) -> int:
        """Units of one order line that have actually left, across every package.

        Applies the same ``SHIPPED_STATUSES`` filter as
        :meth:`shipped_quantities_for_order`, and for the same reason - see that
        method's docstring for the defect the missing filter caused.

        It does **not** filter by order: ``order_item_id`` is unique to one order, so
        an ``order_id`` condition would be redundant rather than an extra safeguard.
        """
        stmt = (
            select(func.coalesce(func.sum(FulfillmentItem.quantity), 0))
            .join(Fulfillment, Fulfillment.id == FulfillmentItem.fulfillment_id)
            .where(
                FulfillmentItem.order_item_id == order_item_id,
                Fulfillment.fulfillment_status.in_(SHIPPED_STATUSES),
            )
        )
        return int(self._session.execute(stmt).scalar_one())

    def planned_quantities_for_order(self, order_id: int) -> dict[int, int]:
        """``{order_item_id: units allocated to packages}``, regardless of status.

        The unfiltered counterpart of :meth:`shipped_quantities_for_order`, and it
        exists so the distinction is **named** rather than being a filter every caller
        has to remember. Read this one to ask "is every line fully allocated across the
        order's packages?"; read the other to ask "has this line shipped?". That is
        precisely the confusion that made the original
        ``shipped_quantities_for_order`` a defect rather than a convenience.

        On a fresh order it equals the order's own quantities, because the shell
        carries the plan; as shipping splits lines into real packages the two methods
        diverge, which is the behaviour worth asserting.
        """
        stmt = (
            select(
                FulfillmentItem.order_item_id,
                func.sum(FulfillmentItem.quantity),
            )
            .join(Fulfillment, Fulfillment.id == FulfillmentItem.fulfillment_id)
            .where(Fulfillment.order_id == order_id)
            .group_by(FulfillmentItem.order_item_id)
        )
        return {int(row[0]): int(row[1]) for row in self._session.execute(stmt).all()}

    def count_for_order(self, order_id: int) -> int:
        """Used by FG-11 to prove a replayed callback created exactly one package."""
        stmt = select(func.count()).select_from(Fulfillment).where(Fulfillment.order_id == order_id)
        return int(self._session.execute(stmt).scalar_one())

    # -- internals -------------------------------------------------------
    def _paginate(self, stmt: Select[object], *, page: int, page_size: int) -> tuple[list[Fulfillment], int]:
        # Count from the filter chain, not from a fetched page: a page-size cap is
        # not a bound on how many packages exist.
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int(self._session.execute(count_stmt).scalar_one())

        offset = max(page - 1, 0) * page_size
        rows = (
            self._session.execute(
                stmt.order_by(Fulfillment.created_at.desc(), Fulfillment.id.desc())
                .limit(page_size)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return list(rows), total


def _as_value(status: str | object) -> str:
    """Normalise an enum member or a raw string to the stored ``VARCHAR`` value.

    Accepts both because the API layer filters with enums while a row read from the
    database yields a plain string, and a comparison that silently fails on one of
    them is a filter that quietly returns everything.
    """
    value = getattr(status, "value", None)
    return str(value) if value is not None else str(status)
