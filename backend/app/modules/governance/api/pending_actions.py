from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.governance.schemas import DecidePendingActionRequest
from app.modules.governance.service import PendingActionService
from app.modules.identity.dependencies import ConsolePrincipal
from app.shared.db.session import get_session

router = APIRouter()
task_router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/pending_actions")
def list_pending_actions(
    principal: ConsolePrincipal,
    session: SessionDep,
    status: Annotated[str | None, Query(max_length=24)] = None,
    risk_level: Annotated[str | None, Query(max_length=16)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    rows, total = PendingActionService(session).list(
        principal=principal,
        status=status,
        risk_level=risk_level,
        page=page,
        page_size=page_size,
    )
    return envelope(
        data={
            "items": [PendingActionService.serialize(row) for row in rows],
            "meta": {"page": page, "page_size": page_size, "total": total, "total_pages": (total + page_size - 1) // page_size if total else 0},
        }
    )


@router.get("/pending_actions/{action_id}")
def get_pending_action(action_id: int, principal: ConsolePrincipal, session: SessionDep) -> dict:
    return envelope(data=PendingActionService.serialize(PendingActionService(session).get(principal=principal, action_id=action_id)))


def _decision(action_id: int, body: DecidePendingActionRequest, principal: ConsolePrincipal, session: Session, approve: bool) -> dict:
    service = PendingActionService(session)
    action = (service.approve if approve else service.reject)(
        principal=principal,
        action_id=action_id,
        request_hash=body.payload_hash,
        reason=body.decision_reason,
    )
    return envelope(data=service.serialize(action))


@task_router.post("/pending-actions/{action_id}/approve")
def approve_pending_action(action_id: int, body: DecidePendingActionRequest, principal: ConsolePrincipal, session: SessionDep) -> dict:
    return _decision(action_id, body, principal, session, True)


@task_router.post("/pending-actions/{action_id}/reject")
def reject_pending_action(action_id: int, body: DecidePendingActionRequest, principal: ConsolePrincipal, session: SessionDep) -> dict:
    return _decision(action_id, body, principal, session, False)


__all__ = ["router", "task_router"]
