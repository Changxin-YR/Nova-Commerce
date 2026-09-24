"""Append-only audit records shared by commerce and agent workflows."""

from __future__ import annotations

from sqlalchemy import JSON, CheckConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db.base import Base, MerchantScopedMixin, PkMixin, TimestampMixin, status_column
from app.shared.db.types import BigIntUnsigned


class AuditRecord(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    """One redacted business decision; rows are never updated or deleted."""

    __merchant_fk_target__ = None

    __tablename__ = "audit_records"
    __table_args__ = (
        CheckConstraint("actor_type IN ('USER','STAFF','AGENT','MCP','SYSTEM')", name="actor_type_valid"),
        CheckConstraint("result IN ('SUCCESS','FAILURE','BLOCKED')", name="result_valid"),
        Index("ix_audit_records_merchant_created", "merchant_id", "created_at"),
        Index("ix_audit_records_resource", "resource_type", "resource_id", "created_at"),
    )

    actor_id: Mapped[int | None] = mapped_column(BigIntUnsigned, nullable=True, index=True)
    actor_type: Mapped[str] = mapped_column(status_column(16), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    before_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result: Mapped[str] = mapped_column(status_column(16), nullable=False)


__all__ = ["AuditRecord"]
