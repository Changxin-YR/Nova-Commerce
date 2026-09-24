from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.agent.service import AgentService
from app.modules.identity.dependencies import ConsolePrincipal
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/admin/tools")
def list_tools(principal: ConsolePrincipal, session: SessionDep) -> dict:
    return envelope(data=AgentService(session).tools(principal=principal))
