from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.identity.dependencies import ConsolePrincipal
from app.modules.knowledge.schemas import CreateKnowledgeBaseRequest, RunEvaluationRequest
from app.modules.knowledge.service import KnowledgeService
from app.shared.db.session import get_session

router = APIRouter()
task_router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/admin/bases")
def list_bases(principal: ConsolePrincipal, session: SessionDep) -> dict:
    return envelope(data=KnowledgeService(session).list_bases(principal=principal))


@router.post("/admin/bases")
def create_base(body: CreateKnowledgeBaseRequest, principal: ConsolePrincipal, session: SessionDep) -> dict:
    return envelope(data=KnowledgeService(session).create_base(principal=principal, request=body))


@router.get("/admin/bases/{base_id}/documents")
def list_documents(base_id: int, principal: ConsolePrincipal, session: SessionDep, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)) -> dict:
    rows, total = KnowledgeService(session).list_documents(principal=principal, base_id=base_id, page=page, page_size=page_size)
    return envelope(data={"items": rows, "meta": {"page": page, "page_size": page_size, "total": total, "total_pages": (total + page_size - 1) // page_size if total else 0}})


@router.post("/admin/bases/{base_id}/documents")
def upload_document(base_id: int, principal: ConsolePrincipal, session: SessionDep, file: UploadFile = File(...)) -> dict:
    data = file.file.read()
    return envelope(data=KnowledgeService(session).upload(principal=principal, base_id=base_id, file_name=file.filename or "upload", content_type=file.content_type or "application/octet-stream", data=data))


@router.get("/admin/evaluation")
def evaluation(principal: ConsolePrincipal, session: SessionDep, knowledge_base_id: int) -> dict:
    return envelope(data=KnowledgeService(session).evaluations(principal=principal, base_id=knowledge_base_id))


@router.post("/admin/evaluation")
def run_evaluation(body: RunEvaluationRequest, principal: ConsolePrincipal, session: SessionDep) -> dict:
    return envelope(data=KnowledgeService(session).run_evaluation(principal=principal, request=body))


@task_router.post("/knowledge/documents/{document_id}/reprocess")
def reprocess_document(document_id: int, principal: ConsolePrincipal, session: SessionDep) -> dict:
    return envelope(data=KnowledgeService(session).transition(principal=principal, document_id=document_id, status="PROCESSING"))


@task_router.post("/knowledge/documents/{document_id}/archive")
def archive_document(document_id: int, principal: ConsolePrincipal, session: SessionDep) -> dict:
    return envelope(data=KnowledgeService(session).transition(principal=principal, document_id=document_id, status="ARCHIVED"))


__all__ = ["router", "task_router"]
