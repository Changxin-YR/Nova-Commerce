from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.shared.db.models.audit import AuditRecord


class AuditRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, record: AuditRecord) -> AuditRecord:
        self._session.add(record)
        self._session.flush()
        return record

    def list_for_merchant(
        self, *, merchant_id: int, resource_type: str | None, page: int, page_size: int
    ) -> tuple[list[AuditRecord], int]:
        stmt = select(AuditRecord).where(AuditRecord.merchant_id == merchant_id)
        if resource_type:
            stmt = stmt.where(AuditRecord.resource_type == resource_type)
        total = int(self._session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
        rows = self._session.execute(
            stmt.order_by(AuditRecord.created_at.desc(), AuditRecord.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        ).scalars().all()
        return list(rows), total
