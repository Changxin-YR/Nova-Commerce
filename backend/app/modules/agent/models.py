from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db.base import Base, MerchantScopedMixin, PkMixin, TimestampMixin, status_column
from app.shared.db.types import BigIntUnsigned, DateTimeMS, MoneyMinor


class AgentRun(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint("user_id", "client_request_id", name="uq_agent_runs_user_request"),
        CheckConstraint("status IN ('RUNNING','WAITING_APPROVAL','SUCCEEDED','FAILED','CANCELLED')", name="status_valid"),
        Index("ix_agent_runs_merchant_status", "merchant_id", "status"),
    )

    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigIntUnsigned, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(status_column(24), nullable=False, default="RUNNING")
    query: Mapped[str] = mapped_column(String(4000), nullable=False)
    final_answer: Mapped[str | None] = mapped_column(String(8000), nullable=True)
    citations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    tool_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)
    pending_action_id: Mapped[int | None] = mapped_column(BigIntUnsigned, nullable=True)
    tokens_used: Mapped[int | None] = mapped_column(BigIntUnsigned, nullable=True)
    cost_amount: Mapped[int | None] = mapped_column(MoneyMinor, nullable=True)
    error_code: Mapped[int | None] = mapped_column(BigIntUnsigned, nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    graph_version: Mapped[str] = mapped_column(String(32), nullable=False, default="phase6-deterministic")
    client_request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTimeMS, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)


__all__ = ["AgentRun"]
