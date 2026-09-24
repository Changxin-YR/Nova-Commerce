from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.agent.service import AgentService
from app.modules.identity.dependencies import CurrentPrincipal
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/runs")
def list_runs(principal: CurrentPrincipal, session: SessionDep, status: Annotated[str | None, Query(max_length=24)] = None, agent_name: Annotated[str | None, Query(max_length=32)] = None, thread_id: Annotated[str | None, Query(max_length=64)] = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict:
    rows, total = AgentService(session).list_runs(principal=principal, status=status, agent_name=agent_name, thread_id=thread_id, page=page, page_size=page_size)
    return envelope(data={"items": rows, "meta": {"page": page, "page_size": page_size, "total": total, "total_pages": (total + page_size - 1) // page_size if total else 0}})


@router.get("/runs/{run_id}")
def get_run(run_id: int, principal: CurrentPrincipal, session: SessionDep) -> dict:
    return envelope(data=AgentService(session).get_run(principal=principal, run_id=run_id))


@router.post("/runs/{run_id}/cancel")
def cancel_run(run_id: int, principal: CurrentPrincipal, session: SessionDep) -> dict:
    return envelope(data=AgentService(session).cancel(principal=principal, run_id=run_id))
