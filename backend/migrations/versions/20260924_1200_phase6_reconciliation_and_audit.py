"""Phase 6 reconciliation facts, immutable pricing snapshots and audit records."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.shared.db.types

revision: str = "20260924_1200"
down_revision: str | None = "f0d29969cfb5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("promotion_id", app.shared.db.types.BigIntUnsigned(), nullable=True))
    op.add_column("orders", sa.Column("pricing_snapshot", sa.JSON(), nullable=True))
    op.create_index("ix_orders_promotion_id", "orders", ["promotion_id"], unique=False)

    op.create_table(
        "payment_compensation_refunds",
        sa.Column("merchant_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("payment_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("order_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("callback_id", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.Column("amount", app.shared.db.types.MoneyMinor(), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("provider_refund_no", sa.String(length=128), nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("id", app.shared.db.types.BigIntUnsigned(), autoincrement=True, nullable=False),
        sa.Column("created_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.Column("updated_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','PROCESSING','SUCCEEDED','FAILED','RECONCILIATION_REQUIRED')",
            name=op.f("ck_payment_compensation_refunds_status_valid"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_payment_compensation_refunds_amount_positive")),
        sa.ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], name=op.f("fk_payment_compensation_refunds_merchant_id_merchants"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["payment_id"], ["payments.id"], name=op.f("fk_payment_compensation_refunds_payment_id_payments"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"], name=op.f("fk_payment_compensation_refunds_order_id_orders"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payment_compensation_refunds")),
        sa.UniqueConstraint("payment_id", name="uq_compensation_refunds_payment"),
    )
    op.create_index("ix_payment_compensation_refunds_merchant_id", "payment_compensation_refunds", ["merchant_id"])
    op.create_index("ix_payment_compensation_refunds_payment_id", "payment_compensation_refunds", ["payment_id"])
    op.create_index("ix_payment_compensation_refunds_order_id", "payment_compensation_refunds", ["order_id"])
    op.create_index("ix_payment_compensation_refunds_callback_id", "payment_compensation_refunds", ["callback_id"])
    op.create_index(
        "ix_compensation_refunds_status_created",
        "payment_compensation_refunds",
        ["status", "created_at"],
    )
    op.create_index("ix_payment_compensation_refunds_created_at", "payment_compensation_refunds", ["created_at"])

    op.create_table(
        "audit_records",
        sa.Column("merchant_id", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.Column("actor_id", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=128), nullable=False),
        sa.Column("trace_id", sa.String(length=64), nullable=True),
        sa.Column("before_snapshot", sa.JSON(), nullable=True),
        sa.Column("after_snapshot", sa.JSON(), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("id", app.shared.db.types.BigIntUnsigned(), autoincrement=True, nullable=False),
        sa.Column("created_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.Column("updated_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('USER','STAFF','AGENT','MCP','SYSTEM')",
            name=op.f("ck_audit_records_actor_type_valid"),
        ),
        sa.CheckConstraint(
            "result IN ('SUCCESS','FAILURE','BLOCKED')",
            name=op.f("ck_audit_records_result_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"], name=op.f("fk_audit_records_merchant_id_merchants"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_records")),
    )
    op.create_index("ix_audit_records_merchant_id", "audit_records", ["merchant_id"])
    op.create_index("ix_audit_records_actor_id", "audit_records", ["actor_id"])
    op.create_index("ix_audit_records_trace_id", "audit_records", ["trace_id"])
    op.create_index("ix_audit_records_created_at", "audit_records", ["created_at"])
    op.create_index("ix_audit_records_merchant_created", "audit_records", ["merchant_id", "created_at"])
    op.create_index(
        "ix_audit_records_resource",
        "audit_records",
        ["resource_type", "resource_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_records_resource", table_name="audit_records")
    op.drop_index("ix_audit_records_merchant_created", table_name="audit_records")
    op.drop_index("ix_audit_records_created_at", table_name="audit_records")
    op.drop_index("ix_audit_records_trace_id", table_name="audit_records")
    op.drop_index("ix_audit_records_actor_id", table_name="audit_records")
    op.drop_index("ix_audit_records_merchant_id", table_name="audit_records")
    op.drop_table("audit_records")

    op.drop_index("ix_payment_compensation_refunds_created_at", table_name="payment_compensation_refunds")
    op.drop_index("ix_compensation_refunds_status_created", table_name="payment_compensation_refunds")
    op.drop_index("ix_payment_compensation_refunds_callback_id", table_name="payment_compensation_refunds")
    op.drop_index("ix_payment_compensation_refunds_order_id", table_name="payment_compensation_refunds")
    op.drop_index("ix_payment_compensation_refunds_payment_id", table_name="payment_compensation_refunds")
    op.drop_index("ix_payment_compensation_refunds_merchant_id", table_name="payment_compensation_refunds")
    op.drop_table("payment_compensation_refunds")

    op.drop_index("ix_orders_promotion_id", table_name="orders")
    op.drop_column("orders", "pricing_snapshot")
    op.drop_column("orders", "promotion_id")
