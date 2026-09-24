"""Persist reproducible knowledge retrieval evaluation results."""

import sqlalchemy as sa
from alembic import op

from app.shared.db.types import BigIntUnsigned, DateTimeMS

revision = "20260924_1600"
down_revision = "20260924_1500"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_evaluations",
        sa.Column("id", BigIntUnsigned(), primary_key=True, autoincrement=True),
        sa.Column("knowledge_base_id", BigIntUnsigned(), sa.ForeignKey("knowledge_bases.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("dataset_name", sa.String(200), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("created_at", DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.Column("updated_at", DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
    )
    op.create_index("ix_knowledge_evaluations_knowledge_base_id", "knowledge_evaluations", ["knowledge_base_id"])
    op.create_index("ix_knowledge_evaluations_created_at", "knowledge_evaluations", ["created_at"])


def downgrade() -> None:
    op.drop_table("knowledge_evaluations")
