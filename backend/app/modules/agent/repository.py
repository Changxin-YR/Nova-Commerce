from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.agent.models import AgentRun


class AgentRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def owns_thread(self, *, thread_id: str, user_id: int) -> bool:
        return self._session.scalar(select(AgentRun.id).where(
            AgentRun.thread_id == thread_id, AgentRun.user_id == user_id
        ).limit(1)) is not None

    def get(self, *, run_id: int, user_id: int | None = None, merchant_id: int | None = None) -> AgentRun | None:
        stmt = select(AgentRun).where(AgentRun.id == run_id)
        if user_id is not None:
            stmt = stmt.where(AgentRun.user_id == user_id)
        if merchant_id is not None:
            stmt = stmt.where(AgentRun.merchant_id == merchant_id)
        return self._session.execute(stmt).scalar_one_or_none()

    def list(self, *, user_id: int | None, merchant_id: int | None, status: str | None, agent_name: str | None, thread_id: str | None, page: int, page_size: int) -> tuple[list[AgentRun], int]:
        stmt = select(AgentRun)
        if user_id is not None:
            stmt = stmt.where(AgentRun.user_id == user_id)
        if merchant_id is not None:
            stmt = stmt.where(AgentRun.merchant_id == merchant_id)
        if status:
            stmt = stmt.where(AgentRun.status == status)
        if agent_name:
            stmt = stmt.where(AgentRun.agent_name == agent_name)
        if thread_id:
            stmt = stmt.where(AgentRun.thread_id == thread_id)
        total = int(self._session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
        rows = list(self._session.execute(stmt.order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(page_size).offset((page - 1) * page_size)).scalars())
        return rows, total


__all__ = ["AgentRepository"]
