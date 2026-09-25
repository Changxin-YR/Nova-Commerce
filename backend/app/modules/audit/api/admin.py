"""Audit console reads.

Transport only: principal, one ``AuditService`` call, then the frozen record shape via
``app.modules.audit.schemas``. The projection used to be an inline comprehension here,
which put the wire contract in the router; it is a schema decision, so it moved.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.audit.schemas import audit_page_meta, audit_record_data
from app.modules.audit.service import AuditService
from app.modules.identity.dependencies import ConsolePrincipal
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/admin/records")
def list_audit_records(
    principal: ConsolePrincipal,
    session: SessionDep,
    resource_type: Annotated[str | None, Query(max_length=64)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    rows, total = AuditService(session).list_admin(
        principal=principal,
        resource_type=resource_type,
        page=page,
        page_size=page_size,
    )
    return envelope(
        data={
            "items": [audit_record_data(row) for row in rows],
            "meta": audit_page_meta(page=page, page_size=page_size, total=total),
        }
    )
