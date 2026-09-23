"""Fulfillment domain models - the goods-out record.

    fulfillments, fulfillment_items

## One order, many packages (REQ-FUL-001)

``fulfillments`` is a **package**, not an order-level status field. An order that
ships in two parcels has two ``fulfillments`` rows, each with its own
``fulfillment_no``, carrier, tracking number and lines. That is the whole shape of
this module, and it is why ``fulfillments.order_id`` carries no unique constraint:
the tempting ``UNIQUE (order_id)`` would encode "one order ships once" into the
schema and make a split shipment impossible to record without a migration.

## The cumulative-quantity rule, and where it is *not* enforced

The design's business rule (section 5.4) is that **the sum of shipped quantities
per ``order_item_id`` across all fulfillments of an order must never exceed that
line's** ``quantity`` (``FULFILLMENT_QUANTITY_EXCEEDS_ORDER``, 70001). It spans
rows and spans tables - the total is a sum over ``fulfillment_items`` joined
through ``fulfillments`` and compared against ``order_items.quantity`` - so no
single-row MySQL ``CHECK`` can express it. It is enforced in
``FulfillmentService.ship`` while the relevant rows are locked, exactly as INV-006 is
enforced in ``CreateOrderWorkflow``. There is no separate ``ShipWorkflow`` class, and
``FulfillmentService.ship`` is the only place this rule is applied. A **per-package**
check would be the defect the rule exists to catch: every row would look valid while
the order shipped 3 units of a line whose quantity is 2.

## ``fulfillment_items.quantity`` is the *package line*, not a shipped counter

The design freezes the columns as ``fulfillment_id, order_item_id, quantity``
(PROJECT_BASELINE REQ-FUL-002), so a line records how many units of that order line
travel in **this** package. Shipping fewer than a package line than was planned is
handled by the ship path adjusting the line and creating the residual in a new
package (section 6.3 step 5), not by adding a second "shipped" quantity that could
disagree with the first. One number, one meaning.

``UNIQUE (fulfillment_id, order_item_id)`` exists because a duplicate line inside
one package would let a **single** package over-ship an order line while each row
individually looked valid - the same failure the cumulative rule guards against,
reached through a different door.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.modules.fulfillment.enums import CARRIER_CODES
from app.modules.order.enums import FULFILLMENT_STATUSES, FulfillmentStatus
from app.modules.order.models import OrderItem
from app.shared.db.base import (
    Base,
    MerchantScopedMixin,
    PkMixin,
    TimestampMixin,
    short_str,
    status_column,
)
from app.shared.db.types import BigIntUnsigned, DateTimeMS

__all__ = ["Fulfillment", "FulfillmentItem"]


def _sql_vocabulary(values: tuple[str, ...]) -> str:
    """Render a vocabulary tuple as the SQL list of a ``CHECK (col IN (...))``.

    Derived from the Python enum rather than typed out by hand, so adding a member
    cannot leave the database rejecting a value the application believes is legal.
    (Alembic does not autogenerate ``CHECK`` changes on MySQL - see the evidence in
    ``HANDOFF.md`` section 6 - so a hand-typed list here would drift silently and
    only fail at write time in production.)
    """
    rendered = ",".join(f"'{value}'" for value in values)
    return f"({rendered})"


def _sku_id_for_insert(context: Any) -> int | None:
    """Resolve ``sku_id`` from the order line a ``fulfillment_items`` row is inserted against.

    A column ``default`` rather than a required argument, and that is a deliberate,
    temporary accommodation. ``sku_id`` is ``NOT NULL``, and the two writers of this
    table - ``FulfillmentService.create_shell`` and ``_create_residual_package`` - live
    in another module and did not yet set it, so making the column mandatory in the
    schema alone broke every package insert with ``IntegrityError (1048)``. This default
    keeps the column correct **and** the write path working in one step, rather than
    leaving the shared branch broken while two files are edited in lockstep.

    It costs one ``SELECT`` per inserted line, which is the price of not duplicating the
    value at every call site. When a caller passes ``sku_id`` explicitly this default is
    not consulted at all, so removing it later is behaviour-neutral - the intent is that
    it goes away once the ship path sets the column itself, which is tracked.

    Returns ``None`` when it cannot resolve, letting the database raise its own
    ``NOT NULL`` error rather than inventing a value here.
    """
    params = getattr(context, "get_current_parameters", lambda: {})() or {}
    order_item_id = params.get("order_item_id")
    session = getattr(context, "session", None)
    if order_item_id is None or session is None:
        return None
    order_item = session.get(OrderItem, order_item_id)
    return None if order_item is None else int(order_item.sku_id)

class Fulfillment(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    """One package: the record that goods left the building (REQ-FUL-001).

    Created in ``UNFULFILLED`` by ``PaymentSuccessWorkflow`` step 8 ("goods exist
    to be shipped"), carrying one line per order line at full quantity.
    ``POST /fulfillments/{id}/ship`` is what moves it - and the *order's*
    ``fulfillment_status`` - forward. Creating a package is **not** shipping it,
    which is why the payment path deliberately leaves the order's axis at
    ``UNFULFILLED`` (section 6.1 step 9).

    **Shipping never changes ``order_status``** (REQ-ORD-003/005, design section
    2). There is no ``order_status`` column here and no code in this phase writes
    one from the fulfillment path; the two axes are independent on purpose.
    """

    __tablename__ = "fulfillments"
    __table_args__ = (
        # The public identifier, scoped by merchant: V1's single merchant is a
        # deployment choice, not a modelling one.
        UniqueConstraint("merchant_id", "fulfillment_no", name="uq_fulfillments_merchant_fulfillment_no"),
        CheckConstraint(
            f"fulfillment_status IN {_sql_vocabulary(FULFILLMENT_STATUSES)}",
            name="fulfillment_status_valid",
        ),
        # NULL is valid (nobody has chosen a carrier yet) and so is any code in the
        # allowlist; a free-text carrier is what this rejects.
        CheckConstraint(
            f"carrier IS NULL OR carrier IN {_sql_vocabulary(CARRIER_CODES)}",
            name="carrier_valid",
        ),
        # A package is a package: zero would let a "shipment" exist that carries
        # nothing of the order.
        CheckConstraint("package_count >= 1", name="package_count_positive"),
        # A shipped or delivered package must actually carry shipment data. Without
        # this, a status write alone could claim goods left while the customer's
        # tracking view has neither carrier nor tracking number - a "ghost
        # shipment", which is worse than an unshipped one because it is invisible
        # in the fulfillment queue (`FulfillmentService.ship` stamps all three in one write,
        # section 6.3 step 3, so this is satisfied by construction).
        CheckConstraint(
            "fulfillment_status NOT IN ('SHIPPED','DELIVERED') "
            "OR (carrier IS NOT NULL AND tracking_no IS NOT NULL AND shipped_at IS NOT NULL)",
            name="shipment_data_required",
        ),
        # A package that has not shipped cannot already have been delivered.
        CheckConstraint(
            "delivered_at IS NULL OR shipped_at IS NOT NULL",
            name="delivered_after_shipped",
        ),
        Index("ix_fulfillments_order_created", "order_id", "created_at"),
        Index("ix_fulfillments_merchant_status", "merchant_id", "fulfillment_status"),
        # The deduction target is read per pending package, so it is indexed on its
        # own as well as through the composite keys above. Nothing else needs one:
        # there is deliberately **no** index on ``tracking_no`` - no frozen query
        # filters by it, and the customer's tracking link resolves through the
        # fulfillment id - so an index there would be pure write overhead on the
        # hottest write path in this module.
        Index("ix_fulfillments_warehouse_id", "warehouse_id"),
    )

    #: ``NVF<YYYYMMDD><id:06d>``. Stampable only *after* flush, because it embeds
    #: the auto-increment id - see ``FulfillmentService.create_shell``.
    fulfillment_no: Mapped[str] = mapped_column(short_str(32), nullable=False)

    order_id: Mapped[int] = mapped_column(
        # RESTRICT: a package is the evidence that goods moved. Deleting the order
        # must fail loudly rather than take the shipping history with it.
        BigIntUnsigned,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: Denormalised for list rendering, so a console queue is one query rather than
    #: 1 + 20 joins - the same decision as ``order_status_logs.order_no``.
    order_no: Mapped[str] = mapped_column(short_str(32), nullable=False)

    #: The warehouse the goods come from. NOT NULL and RESTRICT: this is the
    #: deduction target, so a package that cannot name its warehouse cannot be
    #: reconciled against the stock ledger (INV-007). ``order_items`` records the
    #: warehouse that was reserved per line, and the ship path deducts there - a
    #: package shipped from a warehouse other than the one that reserved the stock
    #: is the inventory bug this column exists to make visible.
    warehouse_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )

    #: Re-exported from ``app.modules.order.enums`` rather than re-declared - see
    #: :mod:`app.modules.fulfillment.enums`.
    fulfillment_status: Mapped[str] = mapped_column(
        status_column(),
        nullable=False,
        default=FulfillmentStatus.UNFULFILLED.value,
        server_default=FulfillmentStatus.UNFULFILLED.value,
    )

    #: Carrier **code**, validated against the ``Carrier`` allowlist in
    #: :mod:`app.modules.fulfillment.enums`. NULL until shipped.
    carrier: Mapped[str | None] = mapped_column(short_str(16), nullable=True)
    #: Deliberately not indexed - see ``__table_args__``.
    tracking_no: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: How many parcels this record represents. Defaults to 1: a ``fulfillments``
    #: row normally *is* one parcel, and the column exists for the case where a
    #: carrier accepts several parcels under one tracking number.
    package_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)

    shipped_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    #: ``selectin``: the ship path and the customer's shipments view both render a
    #: package *with* its lines, and a lazy load there is the N+1 that shows up as
    #: a slow page only once an order has several packages.
    #: ``cascade="all, delete-orphan"``, no ``passive_deletes`` - an ORM
    #: ``session.delete(fulfillment)`` removes its lines, while a raw
    #: ``DELETE FROM fulfillments`` is refused by the RESTRICT FK below. Fixtures
    #: can still clean up; nothing can lose shipping history by accident.
    items: Mapped[list[FulfillmentItem]] = relationship(
        back_populates="fulfillment",
        lazy="selectin",
        order_by="FulfillmentItem.id",
        cascade="all, delete-orphan",
    )

    # -- derived ---------------------------------------------------------
    @property
    def total_quantity(self) -> int:
        """Units in this package, recomputed from the lines."""
        return sum(item.quantity for item in self.items)

    @property
    def is_shipped(self) -> bool:
        """Whether goods have left. ``DELIVERED`` implies shipped, by CHECK."""
        return self.fulfillment_status in (
            FulfillmentStatus.SHIPPED.value,
            FulfillmentStatus.DELIVERED.value,
        )

    def __repr__(self) -> str:
        return (
            f"<Fulfillment {self.fulfillment_no} {self.fulfillment_status} "
            f"carrier={self.carrier} tracking={self.tracking_no}>"
        )


class FulfillmentItem(Base, PkMixin, TimestampMixin):
    """One order line's share of **one** package.

    Deliberately thin: ``fulfillment_id, order_item_id, quantity`` is the frozen
    shape (REQ-FUL-002, design section 5.4), plus the two name snapshots that let a
    packing slip render without joining ``order_items``.

    Note what is **absent**: there is no ``order_id``, no ``merchant_id`` and no
    ``sku_id``. Each of those is reachable through ``fulfillments`` or
    ``order_items``, and duplicating them would create a second place for the same
    fact to be wrong - a package line whose ``sku_id`` disagrees with the order
    line it claims to be part of is a shipping error nobody can detect.
    """

    __tablename__ = "fulfillment_items"
    __table_args__ = (
        # One line per order line per package. A duplicate would let a single
        # package over-ship a line while each row individually looked valid.
        UniqueConstraint("fulfillment_id", "order_item_id", name="uq_fulfillment_items_package_line"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        # The SKU lookup for a package line. Indexed because the console's
        # fulfillment queue and the order's shipments view both resolve a line's SKU by
        # id, and because the FK below otherwise has no supporting index (MySQL creates
        # one implicitly for a FK, but naming it explicitly keeps it inspectable and
        # keeps ``alembic check`` seeing the same object the migration creates).
        Index("ix_fulfillment_items_sku_id", "sku_id"),
    )

    fulfillment_id: Mapped[int] = mapped_column(
        BigIntUnsigned,
        ForeignKey("fulfillments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: What is being shipped. RESTRICT: an order line referenced by a package
    #: cannot be deleted out from under it, which is why history survives.
    order_item_id: Mapped[int] = mapped_column(
        BigIntUnsigned,
        ForeignKey("order_items.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    #: The ordered line's SKU, **snapshotted** like the two names below it.
    #:
    #: This column was ruled in, out, and back in during Phase 5, so the reasoning is
    #: worth recording rather than rediscovering. It was originally specified here by
    #: design section 5.4 and then briefly withdrawn in favour of deriving it on the
    #: read path from ``order_item_id -> order_items.sku_id``, on the grounds that a
    #: duplicated key is a second place for one fact to be wrong. The derivation was
    #: sound but not free: it needs a ``sku_by_line`` map built by every caller, and it
    #: raises ``InternalError`` when that map does not cover a line - which is exactly
    #: what happens when a fulfillment references an order line outside the page being
    #: read.
    #:
    #: The captain's final ruling is the column. The decisive argument is the one that
    #: applies to a *snapshot* table: these rows already store ``product_name`` and
    #: ``sku_name`` as values, and carrying a line's names but not the id they came from
    #: is the one genuinely inconsistent combination. ``order_item_id`` remains the
    #: authoritative link (INV-014 - nothing is rendered from the live catalogue); this
    #: id is stored beside the names purely so a reader never needs a second query.
    #:
    #: ``logical FK`` to ``product_skus.id``, ``RESTRICT``: the same policy as every
    #: other reference in this phase, and a FK that mutated this column would be
    #: rejected by MySQL 8 (errno 3823) because the column also participates in a CHECK.
    sku_id: Mapped[int] = mapped_column(
        BigIntUnsigned,
        ForeignKey("product_skus.id", ondelete="RESTRICT"),
        nullable=False,
        default=_sku_id_for_insert,
        doc="The ordered line's SKU, snapshotted at shipping time (INV-014).",
    )

    #: Snapshot of the order line's names, so a packing slip and the customer's
    #: shipments view render from this row alone (the same INV-014 reasoning as
    #: ``order_items``: nothing about a shipped parcel may change because the
    #: catalogue was edited after it left).
    product_name: Mapped[str] = mapped_column(short_str(200), nullable=False)
    sku_name: Mapped[str] = mapped_column(short_str(200), nullable=False)

    #: Units of this order line in **this** package.
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    fulfillment: Mapped[Fulfillment] = relationship(back_populates="items", lazy="noload")

    def __repr__(self) -> str:
        return (
            f"<FulfillmentItem package={self.fulfillment_id} line={self.order_item_id} qty={self.quantity}>"
        )
