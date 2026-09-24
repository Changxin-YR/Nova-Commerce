"""Add merchant-scoped knowledge and agent run records."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import app.shared.db.types

revision: str = "20260924_1300"
down_revision: str | None = "20260924_1230"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("merchant_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.String(length=1000), nullable=True),
        sa.Column("embedding_model", sa.String(length=128), nullable=False),
        sa.Column("id", app.shared.db.types.BigIntUnsigned(), autoincrement=True, nullable=False),
        sa.Column("created_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.Column("updated_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], name=op.f("fk_knowledge_bases_merchant_id_merchants"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_bases")),
        sa.UniqueConstraint("merchant_id", "name", name="uq_knowledge_bases_merchant_name"),
    )
    op.create_index("ix_knowledge_bases_merchant_id", "knowledge_bases", ["merchant_id"])

    op.create_table(
        "knowledge_documents",
        sa.Column("merchant_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("knowledge_base_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("object_key", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("uploaded_by", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("processed_at", app.shared.db.types.DateTimeMS(), nullable=True),
        sa.Column("id", app.shared.db.types.BigIntUnsigned(), autoincrement=True, nullable=False),
        sa.Column("created_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.Column("updated_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.CheckConstraint("status IN ('UPLOADED','PROCESSING','READY','FAILED','ARCHIVED')", name=op.f("ck_knowledge_documents_status_valid")),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], name=op.f("fk_knowledge_documents_merchant_id_merchants"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["knowledge_base_id"], ["knowledge_bases.id"], name=op.f("fk_knowledge_documents_base_id_knowledge_bases"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"], name=op.f("fk_knowledge_documents_uploaded_by_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_documents")),
        sa.UniqueConstraint("object_key", name="uq_knowledge_documents_object_key"),
    )
    op.create_index("ix_knowledge_documents_merchant_id", "knowledge_documents", ["merchant_id"])
    op.create_index("ix_knowledge_documents_knowledge_base_id", "knowledge_documents", ["knowledge_base_id"])
    op.create_index("ix_knowledge_documents_base_status", "knowledge_documents", ["knowledge_base_id", "status"])

    op.create_table(
        "knowledge_chunks",
        sa.Column("document_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column("id", app.shared.db.types.BigIntUnsigned(), autoincrement=True, nullable=False),
        sa.Column("created_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.Column("updated_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["knowledge_documents.id"], name=op.f("fk_knowledge_chunks_document_id_knowledge_documents"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_chunks")),
    )
    op.create_index("ix_knowledge_chunks_document", "knowledge_chunks", ["document_id"])

    op.create_table(
        "agent_runs",
        sa.Column("merchant_id", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", app.shared.db.types.BigIntUnsigned(), nullable=False),
        sa.Column("agent_name", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("query", sa.String(length=4000), nullable=False),
        sa.Column("final_answer", sa.String(length=8000), nullable=True),
        sa.Column("citations", sa.JSON(), nullable=True),
        sa.Column("tool_calls", sa.JSON(), nullable=True),
        sa.Column("pending_action_id", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.Column("tokens_used", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.Column("cost_amount", app.shared.db.types.MoneyMinor(), nullable=True),
        sa.Column("error_code", app.shared.db.types.BigIntUnsigned(), nullable=True),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("graph_version", sa.String(length=32), nullable=False),
        sa.Column("client_request_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", app.shared.db.types.DateTimeMS(), nullable=False),
        sa.Column("finished_at", app.shared.db.types.DateTimeMS(), nullable=True),
        sa.Column("id", app.shared.db.types.BigIntUnsigned(), autoincrement=True, nullable=False),
        sa.Column("created_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.Column("updated_at", app.shared.db.types.DateTimeMS(), server_default=sa.text("now(3)"), nullable=False),
        sa.CheckConstraint("status IN ('RUNNING','WAITING_APPROVAL','SUCCEEDED','FAILED','CANCELLED')", name=op.f("ck_agent_runs_status_valid")),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], name=op.f("fk_agent_runs_merchant_id_merchants"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_agent_runs_user_id_users"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_agent_runs")),
        sa.UniqueConstraint("user_id", "client_request_id", name="uq_agent_runs_user_request"),
    )
    op.create_index("ix_agent_runs_merchant_id", "agent_runs", ["merchant_id"])
    op.create_index("ix_agent_runs_thread_id", "agent_runs", ["thread_id"])
    op.create_index("ix_agent_runs_user_id", "agent_runs", ["user_id"])
    op.create_index("ix_agent_runs_merchant_status", "agent_runs", ["merchant_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_merchant_status", table_name="agent_runs")
    op.drop_index("ix_agent_runs_user_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_thread_id", table_name="agent_runs")
    op.drop_index("ix_agent_runs_merchant_id", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_knowledge_chunks_document", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
    op.drop_index("ix_knowledge_documents_base_status", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_knowledge_base_id", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_documents_merchant_id", table_name="knowledge_documents")
    op.drop_table("knowledge_documents")
    op.drop_index("ix_knowledge_bases_merchant_id", table_name="knowledge_bases")
    op.drop_table("knowledge_bases")
