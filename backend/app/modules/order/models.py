"""Order domain models (spec sections 30, 34, 35, 36).

    orders, order_items, order_status_logs

## INV-014 is the reason these tables exist in this shape

A product may be renamed, repriced, re-imaged or withdrawn at any time, and a
historical order must be completely unaffected. There are two ways to get that:

* **join the live catalogue at read time** - correct only until somebody edits a
  product name, at which point every past order silently changes and the
  customer's receipt no longer matches what they bought;
* **materialise a snapshot into the order** - the order stops depending on the
  catalogue the moment it is created.

This module takes the second. ``order_items`` stores ``product_name``,
``sku_name``, ``unit_price``, the image reference and ``sku_snapshot`` as *values*,
not as pointers. The FK columns (``product_id``, ``sku_id``) are kept for
reporting and traceability, but nothing about a rendered order is read through
them. That is also why the design deliberately does **not** add
``relationship()`` to the catalogue models: a relationship is an invitation to
read the live row.

## INV-006 exactness, and where it is enforced

``SUM(order_items.payable_amount) == orders.payable_amount``. It spans rows, so a
MySQL ``CHECK`` constraint cannot express it. It is enforced in three places, in
increasing order of trustworthiness:

1. the pricing algorithm, whose pro-rata allocation puts the rounding remainder
   on the last eligible line so the parts always sum to the whole (a largest-
   remainder split would also work, but it is harder to hand-verify);
2. an in-transaction assertion that runs after flush, while a rollback is still
   free;
3. the per-row ``CHECK`` constraints below, which each guard one leg of the
   arithmetic so no single row can be internally inconsistent.

## Why ``payable_amount`` is a stored column at all

It is derivable (``original - allocated``), and the ``CHECK`` constraint pins it
to that derivation. Storing it is deliberate: the amount the customer is charged
must be a *frozen fact* about the order, not a recomputation that depends on
today's promotion rules. The constraint makes the freeze self-consistent.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.modules.order.enums import (
    AFTER_SALE_STATUSES,
    FULFILLMENT_STATUSES,
    OPERATOR_TYPES,
    ORDER_STATUSES,
    PAYMENT_STATUSES,
    AfterSaleStatus,
    FulfillmentStatus,
    OrderStatus,
    PaymentStatus,
)
from app.shared.db.base import (
    Base,
    MerchantScopedMixin,
    PkMixin,
    TimestampMixin,
    VersionMixin,
    short_str,
    status_column,
)
from app.shared.db.types import BigIntUnsigned, DateTimeMS, MoneyMinor

__all__ = ["Order", "OrderItem", "OrderStatusLog"]


def _sql_vocabulary(values: tuple[str, ...]) -> str:
    """Render a vocabulary tuple as the SQL list of a ``CHECK (col IN (...))``.

    Derived from the Python enum rather than typed out by hand, so adding a
    member cannot leave the database rejecting a value the application believes
    is legal. (Alembic does not autogenerate ``CHECK`` changes on MySQL - see the
    evidence in ``HANDOFF.md`` section 6 - so a hand-typed list here would drift
    silently and only fail at write time in production.)
    """
    rendered = ",".join(f"'{value}'" for value in values)
    return f"({rendered})"


class Order(Base, PkMixin, TimestampMixin, MerchantScopedMixin, VersionMixin):
    """A customer order - the commercial record of one purchase attempt.

    Created in ``PENDING_PAYMENT`` and never edited after that except by the
    status machine (``order_status``) and Phase 5's payment columns. Amounts,
    snapshots and the receiver are immutable from creation: spec section 35
    makes the order a record of what was agreed, not a live view of it.
    """

    __tablename__ = "orders"
    __table_args__ = (
        # Human/URL-facing identifier, unique per merchant. Scoped by merchant
        # rather than globally because V1's single merchant is a deployment
        # choice, not a modelling one.
        UniqueConstraint("merchant_id", "order_no", name="uq_orders_merchant_order_no"),
        # The second idempotency guard (spec section 48). At the database level
        # rather than in code, because the whole value of the guard is that it
        # holds even when two requests race through an application-level check.
        UniqueConstraint("user_id", "client_request_id", name="uq_orders_user_client_request"),
        CheckConstraint(
            "payable_amount = original_amount - promotion_discount_amount "
            "- coupon_discount_amount + shipping_amount",
            name="payable_consistent",
        ),
        CheckConstraint(
            "original_amount >= 0 AND promotion_discount_amount >= 0 "
            "AND coupon_discount_amount >= 0 AND shipping_amount >= 0 "
            "AND payable_amount >= 0 AND paid_amount >= 0 AND refunded_amount >= 0",
            name="amounts_non_negative",
        ),
        CheckConstraint(f"order_status IN {_sql_vocabulary(ORDER_STATUSES)}", name="status_valid"),
        CheckConstraint(
            f"payment_status IN {_sql_vocabulary(PAYMENT_STATUSES)}",
            name="payment_status_valid",
        ),
        CheckConstraint(
            f"fulfillment_status IN {_sql_vocabulary(FULFILLMENT_STATUSES)}",
            name="fulfillment_status_valid",
        ),
        CheckConstraint(
            f"after_sale_status IN {_sql_vocabulary(AFTER_SALE_STATUSES)}",
            name="after_sale_status_valid",
        ),
        # Phase 5 (PHASE5_DESIGN section 8), FG-12 cap 1 on the order. Both operands
        # are written only by Phase 5, by two different code paths - the payment
        # callback sets `paid_amount`, the refund workflow adds to `refunded_amount` -
        # so this row-level CHECK is what makes "refunded more than was ever
        # collected" impossible at the database boundary rather than merely
        # unhandled in Python. FG-12 proves it with a direct UPDATE through a fresh
        # connection (errno 3819), not through the service.
        #
        # The ordering it implies is correct rather than restrictive: between create
        # and settlement `paid_amount` is 0, so a refund on an unpaid order is refused
        # outright - and there is no money to give back.
        #
        # Both operands are signed BIGINT, which is what makes the plain comparison
        # correct: MySQL promotes a mixed signed/unsigned comparison to UNSIGNED and
        # would reject arithmetically valid rows (HANDOFF section 6).
        CheckConstraint("refunded_amount <= paid_amount", name="refund_cap"),
        # The console's "orders waiting for attention" view and the customer's
        # "my orders" list are the two hot reads; both are covered.
        Index("ix_orders_merchant_status", "merchant_id", "order_status"),
        Index("ix_orders_status_expires_at", "order_status", "expires_at"),
        Index("ix_orders_user_created", "user_id", "created_at"),
        Index("ix_orders_merchant_created", "merchant_id", "created_at"),
    )

    user_id: Mapped[int] = mapped_column(
        # RESTRICT: an order is a financial record, so deleting the buyer must
        # fail loudly rather than cascade the history away or null out the owner
        # (which would also break UNIQUE (user_id, client_request_id)).
        BigIntUnsigned,
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )

    #: ``NV<YYYYMMDD><id:06d>``. Stampable only *after* flush, because it embeds
    #: the auto-increment id - see ``CreateOrderWorkflow``.
    order_no: Mapped[str] = mapped_column(short_str(32), nullable=False)
    #: The client's own request identifier. Required, so that a client which loses
    #: its ``Idempotency-Key`` header still cannot double-submit.
    client_request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: ``sha256`` of the canonical business inputs, used to detect a key or
    #: request id being reused for a *different* cart (10011).
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The four independent status axes (spec section 31) - never derived from
    #: one another, and only ``order_status`` moves through the state machine.
    order_status: Mapped[str] = mapped_column(
        status_column(), nullable=False, default=OrderStatus.PENDING_PAYMENT.value, server_default=OrderStatus.PENDING_PAYMENT.value
    )
    payment_status: Mapped[str] = mapped_column(
        status_column(), nullable=False, default=PaymentStatus.UNPAID.value, server_default=PaymentStatus.UNPAID.value
    )
    fulfillment_status: Mapped[str] = mapped_column(
        status_column(), nullable=False, default=FulfillmentStatus.UNFULFILLED.value, server_default=FulfillmentStatus.UNFULFILLED.value
    )
    after_sale_status: Mapped[str] = mapped_column(
        status_column(), nullable=False, default=AfterSaleStatus.NONE.value, server_default=AfterSaleStatus.NONE.value
    )

    #: Money, all signed BIGINT minor units. Signed on purpose and not merely by
    #: convention: MySQL promotes a mixed signed/unsigned comparison to UNSIGNED,
    #: so the ``payable_consistent`` CHECK above would evaluate its subtraction
    #: in unsigned arithmetic and reject arithmetically correct rows if any
    #: operand were unsigned. Every operand is signed, so no ``CAST`` is needed.
    original_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    promotion_discount_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    coupon_discount_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    shipping_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    payable_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    #: Written by Phase 5 only.
    paid_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    #: Written by Phase 5 only.
    refunded_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )

    #: Accepted as a business input (spec section 38) and refused until Phase 6
    #: resolves coupons. No FK yet, deliberately: a FK to a table that does not
    #: exist cannot be created, and adding it in Phase 6 is an ordinary migration
    #: rather than a redesign.
    coupon_id: Mapped[int | None] = mapped_column(BigIntUnsigned, nullable=True, index=True)

    #: The address is *snapshotted* rather than joined (section 35): an edit to
    #: ``user_addresses`` must never rewrite a historical order. The FK is kept
    #: for traceability only, and is RESTRICT because an address referenced by an
    #: order should not be hard-deleted - it is soft-deleted instead.
    address_id: Mapped[int | None] = mapped_column(
        BigIntUnsigned,
        ForeignKey("user_addresses.id", ondelete="RESTRICT"),
        nullable=True,
    )
    receiver_name: Mapped[str] = mapped_column(short_str(64), nullable=False)
    receiver_phone: Mapped[str] = mapped_column(String(32), nullable=False)
    #: ``{province, city, district, detail, postal_code}``. Masked on every
    #: outbound path (spec section 94), never on write - the row keeps the truth
    #: so that fulfilment can still ship the parcel.
    address_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)

    remark: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Null unless ``order_status`` is ``CANCELLED``/``CLOSED``. Server-owned
    #: (spec section 14.5) because the client cannot know why an order is gone.
    cancel_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    #: Denormalised snapshots for list rendering (spec section 11 addendum), so a
    #: 20-row order list is one query rather than 1 + 20 joins.
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    first_item_name: Mapped[str] = mapped_column(short_str(200), nullable=False, default="")

    #: ``created_at + ORDER_PAYMENT_TIMEOUT_MINUTES``. Phase 6's reconciliation
    #: reads it to reach ``CLOSED``; Phase 4 only records it.
    expires_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    paid_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    #: ``selectin`` rather than ``lazy``: an order list serialises items for all
    #: 20 rows, and lazy loading there is the N+1 that shows up as a slow page
    #: only once somebody has 20 orders in the list.
    #:
    #: ``cascade="all, delete-orphan"``, no ``passive_deletes`` - verified: an ORM
    #: ``session.delete(order)`` succeeds because ``selectin`` has already loaded the
    #: children and the cascade deletes them, while a raw ``DELETE FROM orders`` on an
    #: order that has items is refused with errno 1451 by the ``RESTRICT`` FKs below.
    #: Deliberate: history cannot vanish by accident, but fixtures can still remove one.
    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order",
        lazy="selectin",
        order_by="OrderItem.id",
        cascade="all, delete-orphan",
    )
    status_logs: Mapped[list[OrderStatusLog]] = relationship(
        back_populates="order",
        lazy="selectin",
        order_by="OrderStatusLog.id",
        cascade="all, delete-orphan",
    )

    # -- derived ---------------------------------------------------------
    @property
    def item_total_quantity(self) -> int:
        """Total units, recomputed from the lines.

        Compare with ``item_count``: they are recomputed differently on purpose.
        ``item_count`` is the frozen snapshot that was written at creation, while
        this is derived now. A mismatch between them is a real defect, which is
        why the workflow asserts they agree while a rollback is still cheap.
        """
        return sum(item.quantity for item in self.items)

    @property
    def items_payable_total(self) -> int:
        """INV-006, computed from the persisted lines."""
        return sum(item.payable_amount for item in self.items)

    @property
    def refundable_amount(self) -> int:
        """What a refund could still cover.

        Guarded at zero because ``paid_amount`` and ``refunded_amount`` are
        written by different Phase 5 code paths; a transient ordering that made
        the difference negative would otherwise surface to a customer as a
        negative refundable balance.
        """
        return max(self.paid_amount - self.refunded_amount, 0)

    def __repr__(self) -> str:
        return f"<Order {self.order_no} {self.order_status} payable={self.payable_amount}>"


class OrderItem(Base, PkMixin, TimestampMixin):
    """One line of an order: a *snapshot* of what was bought (INV-014).

    Every human-readable field here is a copy, not a reference. That is the whole
    point of the table: the day the catalogue changes, ``order_items`` does not.
    """

    __tablename__ = "order_items"
    __table_args__ = (
        # Duplicate SKUs are merged before pricing (API contract section 14.2), so
        # per-SKU uniqueness is an invariant rather than an accident - and it
        # makes "the line for this SKU" unambiguous in every later phase.
        UniqueConstraint("order_id", "sku_id", name="uq_order_items_order_sku"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint(
            "original_amount >= 0 AND promotion_discount_amount >= 0 "
            "AND coupon_discount_amount >= 0 AND allocated_discount_amount >= 0 "
            "AND payable_amount >= 0 AND refunded_amount >= 0",
            name="amounts_non_negative",
        ),
        CheckConstraint(
            "allocated_discount_amount = promotion_discount_amount + coupon_discount_amount",
            name="allocated_consistent",
        ),
        CheckConstraint(
            "payable_amount = original_amount - allocated_discount_amount",
            name="payable_consistent",
        ),
        CheckConstraint(
            f"after_sale_status IN {_sql_vocabulary(AFTER_SALE_STATUSES)}",
            name="after_sale_status_valid",
        ),
        # Phase 5 (PHASE5_DESIGN section 8), FG-12 cap 2 at the *row* level: no single
        # line may be refunded beyond what it contributed to the order total.
        #
        # The cap the design calls "cap 2" in the workflow is the **cumulative** one -
        # `sum(refunds placed on this line) + this share <= payable_amount` - which
        # spans rows and therefore cannot be a CHECK; `RefundWorkflow` enforces it
        # against freshly locked rows. This constraint is the per-row half: it makes a
        # single write that overshoots a line impossible even if the workflow's
        # arithmetic is wrong, which is what "the application check is not the
        # boundary" (section 8) means in practice.
        #
        # Signed operands on both sides, for the mixed-comparison reason above.
        CheckConstraint("refunded_amount <= payable_amount", name="refund_cap"),
    )

    order_id: Mapped[int] = mapped_column(
        BigIntUnsigned,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: The inventory row that was locked for this line (spec section 27). Recorded
    #: so a cancellation can release exactly the row that was reserved, in the
    #: same warehouse, even if the SKU moves warehouse later.
    warehouse_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("warehouses.id", ondelete="RESTRICT"), nullable=False
    )
    #: Kept for reporting and traceability only. **Nothing is ever rendered from
    #: these two** - the names below are what the order shows.
    product_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    sku_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("product_skus.id", ondelete="RESTRICT"), nullable=False
    )

    # -- the snapshot (INV-014) ------------------------------------------
    product_name: Mapped[str] = mapped_column(short_str(200), nullable=False)
    sku_name: Mapped[str] = mapped_column(short_str(200), nullable=False)
    #: Object key, not a URL (ADR-011): a stored URL would break when the bucket
    #: or its public host changes, and a historical order must still resolve.
    image_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: Rendered URL at purchase time, kept for the same reason as the key.
    image_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    #: The SKU's attribute snapshot (e.g. colour/size) as it was at purchase.
    sku_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: Unit price in minor units, frozen. The client never sends this
    #: (spec section 38); it is read from the SKU inside the transaction.
    unit_price: Mapped[int] = mapped_column(MoneyMinor, nullable=False)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    original_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    promotion_discount_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    coupon_discount_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    #: ``promotion + coupon``. Denormalised so a single row is self-checkable by
    #: the CHECK constraint above - and so INV-006 can be verified without
    #: re-adding two columns at read time.
    allocated_discount_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    payable_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    #: Per-line refunds, Phase 5.
    refunded_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )

    after_sale_status: Mapped[str] = mapped_column(
        status_column(), nullable=False, default=AfterSaleStatus.NONE.value, server_default=AfterSaleStatus.NONE.value
    )

    order: Mapped[Order] = relationship(back_populates="items", lazy="noload")

    @property
    def discount_amount(self) -> int:
        """Total discount on this line, the number a receipt prints."""
        return self.allocated_discount_amount

    def __repr__(self) -> str:
        return f"<OrderItem sku={self.sku_id} qty={self.quantity} payable={self.payable_amount}>"


class OrderStatusLog(Base, PkMixin, TimestampMixin):
    """Append-only record of every top-level ``order_status`` transition (section 36).

    **Never updated, never deleted.** ``OrderStatusLogRepository`` therefore
    exposes only an append and read methods - there is no ``update``/``delete`` to
    forget about; see that module for why the omission is deliberate.

    Only the *order* status is logged. A payment or fulfillment change is a
    different axis (section 31) and gets its own record in its own phase; mixing
    them here would make "why is this order cancelled?" require reading four
    vocabularies at once.
    """

    __tablename__ = "order_status_logs"
    __table_args__ = (
        CheckConstraint(f"to_status IN {_sql_vocabulary(ORDER_STATUSES)}", name="to_status_valid"),
        CheckConstraint(
            f"from_status IS NULL OR from_status IN {_sql_vocabulary(ORDER_STATUSES)}",
            name="from_status_valid",
        ),
        CheckConstraint(f"operator_type IN {_sql_vocabulary(OPERATOR_TYPES)}", name="operator_valid"),
        Index("ix_order_status_logs_order_created", "order_id", "created_at"),
    )

    order_id: Mapped[int] = mapped_column(
        BigIntUnsigned,
        ForeignKey("orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    #: Denormalised so the log is readable without joining ``orders`` - and so it
    #: survives even if the order row is later archived.
    order_no: Mapped[str] = mapped_column(short_str(32), nullable=False)

    #: NULL for the creation entry (``None -> PENDING_PAYMENT``), which is the
    #: only legal NULL: the order did not exist before, so there is no source
    #: state to name.
    from_status: Mapped[str | None] = mapped_column(status_column(), nullable=True)
    to_status: Mapped[str] = mapped_column(status_column(), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    operator_type: Mapped[str] = mapped_column(status_column(), nullable=False)
    operator_id: Mapped[int | None] = mapped_column(BigIntUnsigned, nullable=True)
    trace_id: Mapped[str | None] = mapped_column(short_str(64), nullable=True)

    order: Mapped[Order] = relationship(back_populates="status_logs", lazy="noload")

    def __repr__(self) -> str:
        return f"<OrderStatusLog {self.order_no} {self.from_status}->{self.to_status}>"
