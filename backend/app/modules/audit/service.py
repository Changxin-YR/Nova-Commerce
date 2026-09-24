from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.context import get_context
from app.core.redaction import audit_snapshot
from app.modules.audit.repository import AuditRepository
from app.modules.identity.enums import PermissionCode
from app.modules.identity.service import Principal
from app.shared.db.models.audit import AuditRecord


class AuditService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repository = AuditRepository(session)

    def record(
        self,
        *,
        merchant_id: int | None,
        actor_id: int | None,
        actor_type: str,
        action: str,
        resource_type: str,
        resource_id: int | str,
        result: str,
        before: Any = None,
        after: Any = None,
        trace_id: str | None = None,
    ) -> AuditRecord:
        context = get_context()
        return self._repository.add(
            AuditRecord(
                merchant_id=merchant_id,
                actor_id=actor_id,
                actor_type=actor_type,
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id),
                trace_id=trace_id or context.trace_id,
                before_snapshot=audit_snapshot(before) if before is not None else None,
                after_snapshot=audit_snapshot(after) if after is not None else None,
                result=result,
            )
        )

    def list_admin(
        self,
        *,
        principal: Principal,
        resource_type: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AuditRecord], int]:
        principal.require_permission(PermissionCode.AUDIT_READ.value)
        if principal.merchant_id is None:
            return [], 0
        return self._repository.list_for_merchant(
            merchant_id=principal.merchant_id,
            resource_type=resource_type,
            page=page,
            page_size=page_size,
        )
