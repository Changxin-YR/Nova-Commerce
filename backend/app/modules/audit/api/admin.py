from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.errors import envelope
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
            "items": [
                {
                    "id": str(row.id),
                    "actor_id": str(row.actor_id) if row.actor_id is not None else "system",
                    "actor_type": row.actor_type,
                    "action": row.action,
                    "resource_type": row.resource_type,
                    "resource_id": row.resource_id,
                    "trace_id": row.trace_id or "",
                    "before_snapshot": row.before_snapshot,
                    "after_snapshot": row.after_snapshot,
                    "result": row.result,
                    "created_at": row.created_at,
                }
                for row in rows
            ],
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (total + page_size - 1) // page_size if total else 0,
            },
        }
    )
