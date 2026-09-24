from __future__ import annotations

from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.errors import (
    AgentRunNotFoundError,
    IdempotencyInProgressError,
    IdempotencyPayloadMismatchError,
    PermissionDeniedError,
)
from app.modules.agent.models import AgentRun
from app.modules.agent.read_tools import execute_read_tool
from app.modules.agent.repository import AgentRepository
from app.modules.agent.schemas import ChatRequest
from app.modules.governance.service import payload_hash
from app.modules.identity.enums import DataScope, PermissionCode
from app.modules.identity.service import Principal
from app.modules.order.repository import IdempotencyRepository
from app.shared.db.base import utc_now

TOOLS: tuple[dict[str, object], ...] = (
    {"name": "analytics.read", "description": "Read merchant analytics", "risk_level": "READ", "enabled": True, "requires_approval": False},
    {"name": "order.read", "description": "Read scoped order facts", "risk_level": "READ", "enabled": True, "requires_approval": False},
    {"name": "knowledge.read", "description": "Retrieve scoped document evidence", "risk_level": "READ", "enabled": True, "requires_approval": False},
)


class AgentService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repository = AgentRepository(session)

    @staticmethod
    def serialize(run: AgentRun) -> dict:
        return {"id": str(run.id), "thread_id": run.thread_id, "user_id": str(run.user_id), "agent_name": run.agent_name, "status": run.status, "query": run.query, "final_answer": run.final_answer, "citations": run.citations or [], "tool_calls": run.tool_calls or [], "pending_action_id": str(run.pending_action_id) if run.pending_action_id is not None else None, "tokens_used": run.tokens_used, "cost_amount": run.cost_amount, "error_code": run.error_code, "error_message": run.error_message, "started_at": run.started_at, "finished_at": run.finished_at, "graph_version": run.graph_version}

    @staticmethod
    def _scope(principal: Principal) -> tuple[int | None, int | None]:
        if principal.data_scope is DataScope.NONE:
            raise PermissionDeniedError("agent access requires data scope")
        if principal.data_scope is DataScope.SELF or not principal.is_staff:
            return principal.user_id, None
        if principal.data_scope is DataScope.ALL:
            return None, None
        if principal.data_scope is not DataScope.MERCHANT or principal.merchant_id is None:
            raise PermissionDeniedError("agent runs require an explicit merchant scope")
        return None, principal.merchant_id

    def chat(self, *, principal: Principal, request: ChatRequest) -> dict:
        principal.require_permission(PermissionCode.AGENT_CHAT.value)
        self._scope(principal)
        if request.thread_id is not None and not self._repository.owns_thread(thread_id=request.thread_id, user_id=principal.user_id):
            raise AgentRunNotFoundError("agent thread not found")
        claims = IdempotencyRepository(self._session)
        scope = f"agent:chat:{principal.user_id}"
        request_hash = payload_hash({**request.model_dump(mode="json"), "merchant_id": principal.merchant_id, "data_scope": principal.data_scope.value, "permissions": sorted(principal.permissions)})
        claim = claims.insert_in_progress(scope=scope, idempotency_key=request.client_request_id, request_hash=request_hash, resource_type="AGENT_RUN")
        if claim is None:
            existing = claims.get_for_update(scope=scope, idempotency_key=request.client_request_id)
            if existing is None or not existing.is_completed or existing.resource_id is None:
                raise IdempotencyInProgressError("agent request is in progress")
            if existing.request_hash != request_hash:
                raise IdempotencyPayloadMismatchError("agent request payload or authorization changed")
            run = self._repository.get(run_id=existing.resource_id, user_id=principal.user_id)
            if run is None:
                raise AgentRunNotFoundError("agent run not found")
            return self.chat_result(run)
        now = utc_now()
        try:
            result = execute_read_tool(self._session, principal=principal, agent_name=request.agent_name,
                                       message=request.message, knowledge_base_id=request.knowledge_base_id,
                                       inputs=request.tool_input, today=now.date())
        except Exception:
            self._session.rollback()
            raise
        run = AgentRun(
            merchant_id=principal.merchant_id,
            user_id=principal.user_id,
            thread_id=request.thread_id or uuid4().hex,
            agent_name=request.agent_name,
            status="SUCCEEDED",
            query=request.message,
            final_answer=result.answer,
            citations=result.citations,
            tool_calls=[{"name": result.name, "status": "SUCCEEDED", "output": result.output}],
            tokens_used=0,
            client_request_id=request.client_request_id,
            started_at=now,
            finished_at=now,
        )
        self._session.add(run)
        self._session.flush()
        claims.mark_completed(claim, resource_type="AGENT_RUN", resource_id=run.id, response_code=0)
        self._session.commit()
        return self.chat_result(run)

    @staticmethod
    def chat_result(run: AgentRun) -> dict:
        return {"thread_id": run.thread_id, "run_id": str(run.id), "final_answer": run.final_answer,
                "citations": run.citations or [], "tool_calls": run.tool_calls or [], "tokens_used": run.tokens_used}

    def list_runs(self, *, principal: Principal, status: str | None, agent_name: str | None, thread_id: str | None, page: int, page_size: int) -> tuple[list[dict], int]:
        principal.require_permission(PermissionCode.AGENT_RUN_READ.value)
        user_id, merchant_id = self._scope(principal)
        rows, total = self._repository.list(user_id=user_id, merchant_id=merchant_id, status=status, agent_name=agent_name, thread_id=thread_id, page=page, page_size=page_size)
        return [self.serialize(row) for row in rows], total

    def get_run(self, *, principal: Principal, run_id: int) -> dict:
        principal.require_permission(PermissionCode.AGENT_RUN_READ.value)
        user_id, merchant_id = self._scope(principal)
        run = self._repository.get(run_id=run_id, user_id=user_id, merchant_id=merchant_id)
        if run is None:
            raise AgentRunNotFoundError("agent run not found")
        return self.serialize(run)

    def cancel(self, *, principal: Principal, run_id: int) -> dict:
        principal.require_permission(PermissionCode.AGENT_CHAT.value)
        user_id, merchant_id = self._scope(principal)
        run = self._repository.get(run_id=run_id, user_id=user_id, merchant_id=merchant_id)
        if run is None:
            raise AgentRunNotFoundError("agent run not found")
        if run.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return self.serialize(run)
        run.status = "CANCELLED"
        run.finished_at = utc_now()
        self._session.commit()
        return self.serialize(run)

    def tools(self, *, principal: Principal) -> list[dict[str, object]]:
        principal.require_permission(PermissionCode.TOOL_REGISTRY_READ.value)
        return [dict(tool) for tool in TOOLS]

    def threads(self, *, principal: Principal) -> list[dict[str, object]]:
        principal.require_permission(PermissionCode.AGENT_RUN_READ.value)
        user_id, merchant_id = self._scope(principal)
        rows, _ = self._repository.list(user_id=user_id, merchant_id=merchant_id, status=None, agent_name=None, thread_id=None, page=1, page_size=100)
        seen: dict[str, dict[str, object]] = {}
        for row in rows:
            seen.setdefault(row.thread_id, {"id": row.thread_id, "title": row.query[:80], "user_id": str(row.user_id), "agent_name": row.agent_name, "last_message_at": row.updated_at, "message_count": 0})
            current_count = seen[row.thread_id]["message_count"]
            seen[row.thread_id]["message_count"] = (current_count if isinstance(current_count, int) else 0) + 1
        return list(seen.values())


__all__ = ["TOOLS", "AgentService"]
