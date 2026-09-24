"""Add the approval-gated pending action state machine."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.shared.db.types

revision: str = "20260924_1230"
down_revision: str | None = "20260924_1200"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_actions",
        sa.Column("merchant_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("agent_run_id", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("tool_name", sa.String(length=128), nullable=False),
        sa.Column("summary", sa.String(length=500), nullable=False),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("requested_by", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("decided_by", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.Column("decision_reason", sa.String(length=500), nullable=True),
        sa.Column("expires_at", app.shared.db.types.DateTimeMS(), nullable=False),
        sa.Column("decided_at", app.shared.db.types.DateTimeMS(), nullable=True),
        sa.Column("executed_at", app.shared.db.types.DateTimeMS(), nullable=True),
        sa.Column("execution_receipt", sa.JSON(), nullable=True),
        sa.Column("id", app.shared.db.types.BigIntUnsigned(), autoincrement=True, nullable=False),
        sa.Column("created_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.Column("updated_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','EXECUTING','SUCCEEDED','FAILED','EXPIRED')",
            name=op.f("ck_pending_actions_status_valid"),
        ),
        sa.CheckConstraint(
            "risk_level IN ('READ','LOW','MEDIUM','HIGH','CRITICAL')",
            name=op.f("ck_pending_actions_risk_level_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id"], ["merchants.id"],
            name=op.f("fk_pending_actions_merchant_id_merchants"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"],
            name=op.f("fk_pending_actions_requested_by_users"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"],
            name=op.f("fk_pending_actions_decided_by_users"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pending_actions")),
    )
    op.create_index("ix_pending_actions_merchant_id", "pending_actions", ["merchant_id"])
    op.create_index("ix_pending_actions_agent_run_id", "pending_actions", ["agent_run_id"])
    op.create_index("ix_pending_actions_expires_at", "pending_actions", ["expires_at"])
    op.create_index(
        "ix_pending_actions_merchant_status_expiry",
        "pending_actions",
        ["merchant_id", "status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_pending_actions_merchant_status_expiry", table_name="pending_actions")
    op.drop_index("ix_pending_actions_expires_at", table_name="pending_actions")
    op.drop_index("ix_pending_actions_agent_run_id", table_name="pending_actions")
    op.drop_index("ix_pending_actions_merchant_id", table_name="pending_actions")
    op.drop_table("pending_actions")
