"""Align timestamp indexes with ORM metadata for Phase 6 tables."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "20260924_1400"
down_revision: str | None = "20260924_1300"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_agent_runs_created_at", "agent_runs", ["created_at"])
    op.create_index("ix_knowledge_bases_created_at", "knowledge_bases", ["created_at"])
    op.create_index("ix_knowledge_documents_created_at", "knowledge_documents", ["created_at"])
    op.create_index("ix_knowledge_chunks_created_at", "knowledge_chunks", ["created_at"])
    op.create_index("ix_pending_actions_created_at", "pending_actions", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_pending_actions_created_at", table_name="pending_actions")
    op.drop_index("ix_knowledge_chunks_created_at", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_documents_created_at", table_name="knowledge_documents")
    op.drop_index("ix_knowledge_bases_created_at", table_name="knowledge_bases")
    op.drop_index("ix_agent_runs_created_at", table_name="agent_runs")
