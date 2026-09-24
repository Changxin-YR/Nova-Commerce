from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.modules.governance.models import PendingAction


class PendingActionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, *, action_id: int, merchant_id: int | None, all_merchants: bool = False) -> PendingAction | None:
        stmt = select(PendingAction).where(PendingAction.id == action_id)
        if not all_merchants:
            stmt = stmt.where(PendingAction.merchant_id == merchant_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def get_for_update(
        self, *, action_id: int, merchant_id: int | None, all_merchants: bool = False
    ) -> PendingAction | None:
        stmt = select(PendingAction).where(PendingAction.id == action_id).with_for_update()
        if not all_merchants:
            stmt = stmt.where(PendingAction.merchant_id == merchant_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def list(
        self,
        *,
        merchant_id: int | None,
        all_merchants: bool,
        status: str | None,
        risk_level: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[PendingAction], int]:
        stmt = select(PendingAction)
        if not all_merchants:
            stmt = stmt.where(PendingAction.merchant_id == merchant_id)
        if status:
            stmt = stmt.where(PendingAction.status == status)
        if risk_level:
            stmt = stmt.where(PendingAction.risk_level == risk_level)
        total = int(self._session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
        rows = list(
            self._session.execute(
                stmt.order_by(PendingAction.created_at.desc(), PendingAction.id.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            ).scalars()
        )
        return rows, total

    def mark_expired(self, *, now: datetime, merchant_id: int | None, all_merchants: bool) -> int:
        stmt = update(PendingAction).where(
            PendingAction.status == "PENDING", PendingAction.expires_at <= now
        ).values(status="EXPIRED", updated_at=now)
        if not all_merchants:
            stmt = stmt.where(PendingAction.merchant_id == merchant_id)
        result = self._session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)


__all__ = ["PendingActionRepository"]
