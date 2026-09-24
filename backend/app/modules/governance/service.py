from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from hashlib import sha256
from json import dumps
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.errors import (
    AgentBlockedByRiskError,
    PendingActionAlreadyDecidedError,
    PendingActionExpiredError,
    PendingActionNotFoundError,
    PendingActionPayloadChangedError,
    PendingActionStateInvalidError,
    PermissionDeniedError,
)
from app.core.logging import get_logger
from app.modules.audit.service import AuditService
from app.modules.governance.models import PendingAction
from app.modules.governance.repository import PendingActionRepository
from app.modules.identity.enums import DataScope, PermissionCode
from app.modules.identity.service import Principal
from app.shared.db.base import utc_now

logger = get_logger(__name__)


def payload_hash(payload: dict[str, Any]) -> str:
    return sha256(dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


class PendingActionService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repository = PendingActionRepository(session)

    @staticmethod
    def _scope(principal: Principal) -> tuple[int | None, bool]:
        if not principal.is_staff:
            raise PermissionDeniedError("approval access requires staff scope")
        if principal.data_scope is DataScope.ALL:
            return None, True
        if principal.data_scope is not DataScope.MERCHANT or principal.merchant_id is None:
            raise PermissionDeniedError("approval access requires an explicit merchant scope")
        return principal.merchant_id, False

    @staticmethod
    def serialize(action: PendingAction) -> dict[str, Any]:
        return {
            "id": str(action.id),
            "agent_run_id": action.agent_run_id,
            "action_type": action.action_type,
            "tool_name": action.tool_name,
            "summary": action.summary,
            "risk_level": action.risk_level,
            "status": action.status,
            "payload": action.payload,
            "payload_hash": action.payload_hash,
            "requested_by": str(action.requested_by),
            "decided_by": str(action.decided_by) if action.decided_by is not None else None,
            "decision_reason": action.decision_reason,
            "expires_at": action.expires_at,
            "created_at": action.created_at,
            "decided_at": action.decided_at,
            "executed_at": action.executed_at,
            "execution_receipt": action.execution_receipt,
        }

    def list(self, *, principal: Principal, status: str | None, risk_level: str | None, page: int, page_size: int) -> tuple[list[PendingAction], int]:
        principal.require_permission(PermissionCode.PENDING_ACTION_READ.value)
        merchant_id, all_merchants = self._scope(principal)
        return self._repository.list(
            merchant_id=merchant_id,
            all_merchants=all_merchants,
            status=status,
            risk_level=risk_level,
            page=page,
            page_size=page_size,
        )

    def get(self, *, principal: Principal, action_id: int) -> PendingAction:
        principal.require_permission(PermissionCode.PENDING_ACTION_READ.value)
        merchant_id, all_merchants = self._scope(principal)
        action = self._repository.get(action_id=action_id, merchant_id=merchant_id, all_merchants=all_merchants)
        if action is None:
            raise PendingActionNotFoundError("pending action not found")
        return action

    def _decide(self, *, principal: Principal, action_id: int, request_hash: str, approve: bool, reason: str | None) -> PendingAction:
        principal.require_permission(PermissionCode.PENDING_ACTION_APPROVE.value)
        merchant_id, all_merchants = self._scope(principal)
        action = self._repository.get_for_update(action_id=action_id, merchant_id=merchant_id, all_merchants=all_merchants)
        if action is None:
            raise PendingActionNotFoundError("pending action not found")
        now = utc_now()
        if approve and action.risk_level == "CRITICAL":
            raise AgentBlockedByRiskError("critical-risk actions cannot be approved")
        if action.status == "PENDING" and action.expires_at <= now:
            action.status = "EXPIRED"
            action.updated_at = now
            self._session.commit()
            raise PendingActionExpiredError("pending action has expired")
        if action.status != "PENDING":
            if action.status in {"APPROVED", "REJECTED", "EXECUTING", "SUCCEEDED", "FAILED", "EXPIRED"}:
                raise PendingActionAlreadyDecidedError("pending action has already been decided")
            raise PendingActionStateInvalidError("pending action is not pending")
        if action.payload_hash != request_hash:
            raise PendingActionPayloadChangedError("pending action payload changed")
        next_status = "APPROVED" if approve else "REJECTED"
        result = self._session.execute(
            update(PendingAction)
            .where(
                PendingAction.id == action.id,
                PendingAction.status == "PENDING",
                PendingAction.expires_at > now,
            )
            .values(
                status=next_status,
                decided_by=principal.user_id,
                decision_reason=reason,
                decided_at=now,
                updated_at=now,
            )
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            self._session.rollback()
            raise PendingActionStateInvalidError("pending action changed during decision")
        action.status = next_status
        action.decided_by = principal.user_id
        action.decision_reason = reason
        action.decided_at = now
        action.updated_at = now
        AuditService(self._session).record(
            merchant_id=action.merchant_id,
            actor_id=principal.user_id,
            actor_type="STAFF",
            action=f"pending_action.{next_status.lower()}",
            resource_type="PENDING_ACTION",
            resource_id=action.id,
            result="SUCCESS",
            after={"status": next_status},
        )
        self._session.commit()
        return action

    def approve(self, *, principal: Principal, action_id: int, request_hash: str, reason: str | None) -> PendingAction:
        return self._decide(principal=principal, action_id=action_id, request_hash=request_hash, approve=True, reason=reason)

    def reject(self, *, principal: Principal, action_id: int, request_hash: str, reason: str | None) -> PendingAction:
        return self._decide(principal=principal, action_id=action_id, request_hash=request_hash, approve=False, reason=reason)

    def resume(
        self,
        *,
        action_id: int,
        revalidate: Callable[[PendingAction], bool],
        execute: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> PendingAction:
        action = self._repository.get_for_update(action_id=action_id, merchant_id=None, all_merchants=True)
        if action is None:
            raise PendingActionNotFoundError("pending action not found")
        now = utc_now()
        if action.status == "APPROVED" and action.expires_at <= now:
            action.status = "EXPIRED"
            action.updated_at = now
            self._session.commit()
            raise PendingActionExpiredError("pending action has expired")
        if action.status == "SUCCEEDED":
            return action
        if action.status != "APPROVED":
            raise PendingActionStateInvalidError("pending action is not approved")
        if action.risk_level == "CRITICAL":
            raise AgentBlockedByRiskError("critical-risk actions cannot execute")
        if payload_hash(action.payload) != action.payload_hash:
            action.status = "FAILED"
            action.decision_reason = "approved payload integrity check failed"
            self._session.commit()
            raise PendingActionPayloadChangedError("approved payload changed")
        if not revalidate(action):
            action.status = "FAILED"
            action.decision_reason = "business facts changed during resume"
            action.updated_at = now
            self._session.commit()
            raise PendingActionPayloadChangedError("business facts changed during resume")
        action.status = "EXECUTING"
        action.updated_at = now
        self._session.flush()
        try:
            with self._session.begin_nested():
                receipt = execute(action.payload)
        except Exception:
            action.status = "FAILED"
            action.updated_at = utc_now()
            self._session.commit()
            raise
        action.status = "SUCCEEDED"
        action.execution_receipt = receipt
        action.executed_at = utc_now()
        action.updated_at = action.executed_at
        self._session.commit()
        return action

    def expire(self, *, now: datetime | None = None) -> int:
        resolved = now or utc_now()
        count = self._repository.mark_expired(now=resolved, merchant_id=None, all_merchants=True)
        self._session.commit()
        return count


__all__ = ["PendingActionService", "payload_hash"]
