from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.identity.dependencies import ConsolePrincipal
from app.modules.knowledge.schemas import RetrievalDebugRequest
from app.modules.knowledge.service import KnowledgeService
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


@router.post("/retrieval/debug")
def retrieval_debug(body: RetrievalDebugRequest, principal: ConsolePrincipal, session: SessionDep) -> dict:
    return envelope(data=KnowledgeService(session).debug_retrieval(principal=principal, request=body))


__all__ = ["router"]
