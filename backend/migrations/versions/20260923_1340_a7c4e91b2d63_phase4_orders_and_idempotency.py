"""phase4 orders and idempotency

Revision ID: a7c4e91b2d63
Revises: 5cbebbc7b364
Create Date: 2026-09-23 13:40:00.000000

Phase 4 (spec section 7 / design ``docs/architecture/PHASE4_DESIGN.md`` section 4)
adds the four tables that make an order a record rather than a computation:

    orders, order_items, order_status_logs, idempotency_records

## Why this file is hand-written

Alembic generated the skeleton (``--autogenerate``), and this file was reviewed
against it rather than trusted. Two MySQL behaviours recorded in ``HANDOFF.md``
section 6 make reviewing mandatory rather than polite:

* **Alembic does not autogenerate CHECK-constraint changes on MySQL.** A
  constraint edit produces an *empty* migration, so the database silently keeps
  the old rule while the models claim the new one.
* **``compare_type`` does not distinguish BIGINT from BIGINT UNSIGNED.** So a
  signedness change is also invisible to autogenerate.

Both are exactly the class of change this migration is made of - eight CHECK
constraints, four unique keys and a set of signed money columns - so the DDL below
was verified by reading ``information_schema`` back after applying it (see
"Verification" at the bottom of this docstring), not by the migration exiting 0.

## The signed-money decision (do not "tidy" this)

Every money column is ``BIGINT`` **signed** (``MoneyMinor``), and so is
``version``. That is deliberate: ``ck_orders_payable_consistent`` performs
subtraction, and MySQL promotes a mixed signed/unsigned comparison to UNSIGNED -
so a single unsigned operand would make ``0 - 1`` overflow and reject a row that
is arithmetically correct. FG-09 lost an hour to exactly this on the stock ledger
(see ``20260923_1235_5cbebbc7b364_ledger_quantities_signed_bigint.py``). Keeping
every operand signed is why this constraint needs no ``CAST``.

## Deliberately absent

* **No FK for ``orders.coupon_id``.** Phase 6 creates ``coupons``; a FK to a table
  that does not exist cannot be created. Adding it later is an ordinary migration.
* **No single-column index on ``orders.user_id`` / ``orders.merchant_id``** beyond
  the one ``MerchantScopedMixin`` always emits. Both are the leading column of the
  composite indexes below (``(user_id, created_at)``, ``(merchant_id,
  order_status)``, ``(merchant_id, created_at)``), which serve the frozen queries.
  Autogenerate will want to re-add them whenever the models are diffed; the
  answer is "no", because they would be pure write overhead.
* **No ``CHECK`` for INV-006** (``SUM(order_items.payable_amount) ==
  orders.payable_amount``). It spans rows, so MySQL cannot express it; it is
  asserted in-transaction by ``CreateOrderWorkflow`` and guarded per row by
  ``ck_order_items_payable_consistent``.

## Verification

After ``alembic upgrade head``, the following were read back and matched against
the constraints declared here - all four tables, every ``CHECK`` name above, and
``orders.original_amount`` typed ``bigint`` (signed) rather than ``bigint
unsigned``::

    SELECT tc.TABLE_NAME, tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
      FROM information_schema.TABLE_CONSTRAINTS tc
      JOIN information_schema.CHECK_CONSTRAINTS cc
        ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
       AND cc.CONSTRAINT_NAME   = tc.CONSTRAINT_NAME
     WHERE tc.TABLE_SCHEMA = DATABASE()
       AND tc.CONSTRAINT_TYPE = 'CHECK';

(``information_schema.CHECK_CONSTRAINTS`` has no ``TABLE_NAME`` in MySQL 8.4, so
the join through ``TABLE_CONSTRAINTS`` is required - another entry in
``HANDOFF.md`` section 6.)

The migration is one-way safe: ``downgrade()`` drops the four tables in reverse
dependency order and is written out in full, because an unrunnable downgrade
turns a bad deploy into an unrecoverable one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.shared.db.types

# revision identifiers, used by Alembic.
revision: str = "a7c4e91b2d63"
down_revision: str | None = "5cbebbc7b364"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# ---------------------------------------------------------------------------
# Vocabularies
#
# Duplicated from ``app.modules.order.enums`` on purpose: a migration must
# describe the schema *as of this revision* and must keep working even after the
# Python enum gains a member in a later phase. Importing the live enum here would
# silently rewrite history - a Phase 6 enum member would appear in a Phase 4
# migration's constraint, and re-running the migration on a clean database would
# produce a constraint that Phase 4 never had.
#
# These strings are asserted equal to the enums by the data-layer tests, so the
# duplication cannot drift unnoticed.
# ---------------------------------------------------------------------------
ORDER_STATUSES = ("PENDING_PAYMENT", "PROCESSING", "COMPLETED", "CANCELLED", "CLOSED")
PAYMENT_STATUSES = ("UNPAID", "PAYING", "PAID", "PARTIAL_REFUNDED", "REFUNDED")
FULFILLMENT_STATUSES = ("UNFULFILLED", "PARTIAL_SHIPPED", "SHIPPED", "DELIVERED")
AFTER_SALE_STATUSES = ("NONE", "PROCESSING", "PARTIAL_REFUNDED", "REFUNDED")
OPERATOR_TYPES = ("SYSTEM", "CUSTOMER", "STAFF", "AGENT", "MCP", "WORKER")
IDEMPOTENCY_STATUSES = ("IN_PROGRESS", "COMPLETED", "FAILED")


def _vocabulary(values: tuple[str, ...]) -> str:
    """Render a tuple as the SQL body of ``IN (...)``."""
    return "(" + ",".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    """Create the four Phase 4 tables."""
    # -----------------------------------------------------------------
    # idempotency_records - shared with Phase 5's payment callbacks (section 48)
    # -----------------------------------------------------------------
    op.create_table(
        "idempotency_records",
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="IN_PROGRESS", nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("response_snapshot", sa.JSON(), nullable=True),
        sa.Column("resource_type", sa.String(length=32), nullable=True),
        sa.Column("resource_id", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.Column("expires_at", app.shared.db.types.DateTimeMS(), nullable=True),
        sa.Column("id", app.shared.db.types.BigIntUnsigned(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            app.shared.db.types.DateTimeMS(),
            server_default=sa.text("now(3)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.shared.db.types.DateTimeMS(),
            server_default=sa.text("now(3)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"status IN {_vocabulary(IDEMPOTENCY_STATUSES)}",
            name=op.f("ck_idempotency_records_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_records")),
        # The serialisation point for concurrent creates. A `SELECT`-then-`INSERT`
        # check cannot decide this: two transactions can both find nothing, and
        # only an index can nominate one winner.
        sa.UniqueConstraint("scope", "idempotency_key", name="uq_scope_idempotency_key"),
    )
    op.create_index(
        op.f("ix_idempotency_records_created_at"),
        "idempotency_records",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_idempotency_records_expires_at",
        "idempotency_records",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_idempotency_records_resource",
        "idempotency_records",
        ["resource_type", "resource_id"],
        unique=False,
    )

    # -----------------------------------------------------------------
    # orders - the commercial record (design section 4.1)
    # -----------------------------------------------------------------
    op.create_table(
        "orders",
        sa.Column("user_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("client_request_id", sa.String(length=64), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("order_status", sa.String(length=32), server_default="PENDING_PAYMENT", nullable=False),
        sa.Column("payment_status", sa.String(length=32), server_default="UNPAID", nullable=False),
        sa.Column("fulfillment_status", sa.String(length=32), server_default="UNFULFILLED", nullable=False),
        sa.Column("after_sale_status", sa.String(length=32), server_default="NONE", nullable=False),
        # Money: signed BIGINT minor units. See the module docstring.
        sa.Column(
            "original_amount",
            app.shared.db.types.MoneyMinor(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "promotion_discount_amount",
            app.shared.db.types.MoneyMinor(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "coupon_discount_amount",
            app.shared.db.types.MoneyMinor(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "shipping_amount",
            app.shared.db.types.MoneyMinor(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "payable_amount",
            app.shared.db.types.MoneyMinor(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "paid_amount",
            app.shared.db.types.MoneyMinor(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "refunded_amount",
            app.shared.db.types.MoneyMinor(),
            server_default="0",
            nullable=False,
        ),
        # No FK yet: Phase 6 creates `coupons` and adds the constraint.
        sa.Column("coupon_id", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.Column("address_id", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.Column("receiver_name", sa.String(length=64), nullable=False),
        sa.Column("receiver_phone", sa.String(length=32), nullable=False),
        sa.Column("address_snapshot", sa.JSON(), nullable=False),
        sa.Column("remark", sa.String(length=500), nullable=True),
        sa.Column("cancel_reason", sa.String(length=500), nullable=True),
        sa.Column("item_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_item_name", sa.String(length=200), nullable=False),
        sa.Column("expires_at", app.shared.db.types.DateTimeMS(), nullable=True),
        sa.Column("paid_at", app.shared.db.types.DateTimeMS(), nullable=True),
        sa.Column("completed_at", app.shared.db.types.DateTimeMS(), nullable=True),
        sa.Column("cancelled_at", app.shared.db.types.DateTimeMS(), nullable=True),
        sa.Column("closed_at", app.shared.db.types.DateTimeMS(), nullable=True),
        sa.Column("id", app.shared.db.types.BigIntUnsigned(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            app.shared.db.types.DateTimeMS(),
            server_default=sa.text("now(3)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.shared.db.types.DateTimeMS(),
            server_default=sa.text("now(3)"),
            nullable=False,
        ),
        sa.Column("merchant_id", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.Column(
            "version",
            app.shared.db.types.BigIntUnsigned(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        # Every operand here is signed, which is what makes plain subtraction and
        # comparison correct (see the docstring).
        sa.CheckConstraint(
            "original_amount >= 0 AND promotion_discount_amount >= 0 "
            "AND coupon_discount_amount >= 0 AND shipping_amount >= 0 "
            "AND payable_amount >= 0 AND paid_amount >= 0 AND refunded_amount >= 0",
            name=op.f("ck_orders_amounts_non_negative"),
        ),
        sa.CheckConstraint(
            "payable_amount = original_amount - promotion_discount_amount "
            "- coupon_discount_amount + shipping_amount",
            name=op.f("ck_orders_payable_consistent"),
        ),
        sa.CheckConstraint(
            f"order_status IN {_vocabulary(ORDER_STATUSES)}",
            name=op.f("ck_orders_status_valid"),
        ),
        sa.CheckConstraint(
            f"payment_status IN {_vocabulary(PAYMENT_STATUSES)}",
            name=op.f("ck_orders_payment_status_valid"),
        ),
        sa.CheckConstraint(
            f"fulfillment_status IN {_vocabulary(FULFILLMENT_STATUSES)}",
            name=op.f("ck_orders_fulfillment_status_valid"),
        ),
        sa.CheckConstraint(
            f"after_sale_status IN {_vocabulary(AFTER_SALE_STATUSES)}",
            name=op.f("ck_orders_after_sale_status_valid"),
        ),
        # RESTRICT on every FK: errno 3823 forbids a mutating referential action
        # on a column that also carries a CHECK (orders.merchant_id is nullable
        # and participates in the amount/status CHECKs' table), and RESTRICT is
        # the correct policy for financial records anyway.
        sa.ForeignKeyConstraint(
            ["address_id"],
            ["user_addresses.id"],
            name=op.f("fk_orders_address_id_user_addresses"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"],
            ["merchants.id"],
            name=op.f("fk_orders_merchant_id_merchants"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_orders_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_orders")),
        sa.UniqueConstraint("merchant_id", "order_no", name="uq_orders_merchant_order_no"),
        # The second idempotency guard, as a constraint rather than a convention
        # (API contract section 14.3).
        sa.UniqueConstraint(
            "user_id",
            "client_request_id",
            name="uq_orders_user_client_request",
        ),
    )
    op.create_index(op.f("ix_orders_coupon_id"), "orders", ["coupon_id"], unique=False)
    op.create_index(op.f("ix_orders_created_at"), "orders", ["created_at"], unique=False)
    op.create_index(
        "ix_orders_merchant_created", "orders", ["merchant_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_orders_merchant_id"), "orders", ["merchant_id"], unique=False)
    op.create_index(
        "ix_orders_merchant_status", "orders", ["merchant_id", "order_status"], unique=False
    )
    op.create_index("ix_orders_user_created", "orders", ["user_id", "created_at"], unique=False)

    # -----------------------------------------------------------------
    # order_items - the INV-014 snapshot (design section 4.2)
    # -----------------------------------------------------------------
    op.create_table(
        "order_items",
        sa.Column("order_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("warehouse_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("product_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("sku_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        # Snapshot columns. Nothing rendered to a customer reads the catalogue.
        sa.Column("product_name", sa.String(length=200), nullable=False),
        sa.Column("sku_name", sa.String(length=200), nullable=False),
        sa.Column("image_object_key", sa.String(length=512), nullable=True),
        sa.Column("image_url", sa.String(length=512), nullable=True),
        sa.Column("sku_snapshot", sa.JSON(), nullable=True),
        sa.Column("unit_price", app.shared.db.types.MoneyMinor(), nullable=False),
        sa.Column("quantity", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "original_amount",
            app.shared.db.types.MoneyMinor(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "promotion_discount_amount",
            app.shared.db.types.MoneyMinor(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "coupon_discount_amount",
            app.shared.db.types.MoneyMinor(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "allocated_discount_amount",
            app.shared.db.types.MoneyMinor(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "payable_amount",
            app.shared.db.types.MoneyMinor(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "refunded_amount",
            app.shared.db.types.MoneyMinor(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("after_sale_status", sa.String(length=32), server_default="NONE", nullable=False),
        sa.Column("id", app.shared.db.types.BigIntUnsigned(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            app.shared.db.types.DateTimeMS(),
            server_default=sa.text("now(3)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.shared.db.types.DateTimeMS(),
            server_default=sa.text("now(3)"),
            nullable=False,
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_order_items_quantity_positive")),
        sa.CheckConstraint(
            "original_amount >= 0 AND promotion_discount_amount >= 0 "
            "AND coupon_discount_amount >= 0 AND allocated_discount_amount >= 0 "
            "AND payable_amount >= 0 AND refunded_amount >= 0",
            name=op.f("ck_order_items_amounts_non_negative"),
        ),
        # These two make a *single row* self-consistent, which is one leg of
        # INV-006. The cross-row sum is asserted in the workflow.
        sa.CheckConstraint(
            "allocated_discount_amount = promotion_discount_amount + coupon_discount_amount",
            name=op.f("ck_order_items_allocated_consistent"),
        ),
        sa.CheckConstraint(
            "payable_amount = original_amount - allocated_discount_amount",
            name=op.f("ck_order_items_payable_consistent"),
        ),
        sa.CheckConstraint(
            f"after_sale_status IN {_vocabulary(AFTER_SALE_STATUSES)}",
            name=op.f("ck_order_items_after_sale_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_items_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_order_items_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sku_id"],
            ["product_skus.id"],
            name=op.f("fk_order_items_sku_id_product_skus"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["warehouse_id"],
            ["warehouses.id"],
            name=op.f("fk_order_items_warehouse_id_warehouses"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
        # Duplicate SKUs are merged before pricing, so one line per SKU is an
        # invariant, and it makes "the line for this SKU" unambiguous later.
        sa.UniqueConstraint("order_id", "sku_id", name="uq_order_items_order_sku"),
    )
    op.create_index(op.f("ix_order_items_created_at"), "order_items", ["created_at"], unique=False)
    op.create_index(op.f("ix_order_items_order_id"), "order_items", ["order_id"], unique=False)

    # -----------------------------------------------------------------
    # order_status_logs - append-only audit trail (design section 4.3)
    # -----------------------------------------------------------------
    op.create_table(
        "order_status_logs",
        sa.Column("order_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("order_no", sa.String(length=32), nullable=False),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("operator_type", sa.String(length=32), nullable=False),
        sa.Column("operator_id", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("id", app.shared.db.types.BigIntUnsigned(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            app.shared.db.types.DateTimeMS(),
            server_default=sa.text("now(3)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            app.shared.db.types.DateTimeMS(),
            server_default=sa.text("now(3)"),
            nullable=False,
        ),
        # NULL from_status is legal for exactly one row per order: the creation
        # entry (None -> PENDING_PAYMENT), because the order did not exist before.
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN {_vocabulary(ORDER_STATUSES)}",
            name=op.f("ck_order_status_logs_from_status_valid"),
        ),
        sa.CheckConstraint(
            f"to_status IN {_vocabulary(ORDER_STATUSES)}",
            name=op.f("ck_order_status_logs_to_status_valid"),
        ),
        sa.CheckConstraint(
            f"operator_type IN {_vocabulary(OPERATOR_TYPES)}",
            name=op.f("ck_order_status_logs_operator_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["orders.id"],
            name=op.f("fk_order_status_logs_order_id_orders"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_status_logs")),
    )
    op.create_index(
        op.f("ix_order_status_logs_created_at"),
        "order_status_logs",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_order_status_logs_order_created",
        "order_status_logs",
        ["order_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_order_status_logs_order_id"),
        "order_status_logs",
        ["order_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the four Phase 4 tables, in reverse dependency order.

    ``order_items`` and ``order_status_logs`` both reference ``orders``, so they
    must go first. ``idempotency_records`` has no FKs and can go any time; it is
    dropped last so the rollback of a partially-applied upgrade is always valid.

    ## Why there are no ``op.drop_index`` calls here

    The first version of this function dropped each index before its table, and it
    **failed on the first run** with::

        (1553, "Cannot drop index 'ix_order_status_logs_order_created': needed
                in a foreign key constraint")

    MySQL will not drop an index that is serving a foreign key, and
    ``ix_order_status_logs_order_created`` on ``(order_id, created_at)`` serves
    ``fk_order_status_logs_order_id_orders`` (InnoDB picked the composite index
    over the single-column one, which had already been dropped by the time the
    error was raised).

    ``DROP TABLE`` removes a table's indexes and constraints *with* the table, so
    the explicit drops were not merely unnecessary - they were the bug. Worse,
    MySQL DDL is non-transactional, so that failure left the database
    half-downgraded: ``alembic_version`` still named this revision while two
    indexes were already gone. That is exactly why this path is executed in the
    verification pass instead of being assumed to work.
    """
    op.drop_table("order_status_logs")
    op.drop_table("order_items")
    op.drop_table("orders")
    op.drop_table("idempotency_records")
