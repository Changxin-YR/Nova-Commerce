from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db.base import Base, MerchantScopedMixin, PkMixin, TimestampMixin, status_column
from app.shared.db.types import BigIntUnsigned, DateTimeMS


class PendingAction(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    __tablename__ = "pending_actions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','APPROVED','REJECTED','EXECUTING','SUCCEEDED','FAILED','EXPIRED')",
            name="status_valid",
        ),
        CheckConstraint(
            "risk_level IN ('READ','LOW','MEDIUM','HIGH','CRITICAL')",
            name="risk_level_valid",
        ),
        Index("ix_pending_actions_merchant_status_expiry", "merchant_id", "status", "expires_at"),
    )

    merchant_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    agent_run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    risk_level: Mapped[str] = mapped_column(status_column(16), nullable=False)
    status: Mapped[str] = mapped_column(status_column(24), nullable=False, default="PENDING")
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    decided_by: Mapped[int | None] = mapped_column(
        BigIntUnsigned, ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    decision_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTimeMS, nullable=False, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    execution_receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)


__all__ = ["PendingAction"]
