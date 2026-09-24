from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.agent.schemas import ChatRequest
from app.modules.agent.service import AgentService
from app.modules.identity.dependencies import CurrentPrincipal
from app.shared.db.base import utc_now
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


def _sse_event(event: str, payload: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat")
def chat(body: ChatRequest, principal: CurrentPrincipal, session: SessionDep) -> dict:
    return envelope(data=AgentService(session).chat(principal=principal, request=body))


@router.post("/chat/stream")
def chat_stream(body: ChatRequest, principal: CurrentPrincipal, session: SessionDep) -> StreamingResponse:
    result = AgentService(session).chat(principal=principal, request=body)
    run_id = str(result["run_id"])
    thread_id = str(result["thread_id"])
    now = utc_now().isoformat()
    base = {"run_id": run_id, "thread_id": thread_id, "seq": 0, "at": now}
    events = [
        ("run_started", {**base, "seq": 1, "agent_name": body.agent_name, "query": body.message}),
        ("route_selected", {**base, "seq": 2, "route": body.agent_name, "reason": "agent route selected"}),
        ("evidence_ready", {**base, "seq": 3, "citations": result["citations"], "insufficient": body.knowledge_base_id is not None and not result["citations"]}),
        (
            "final_answer",
            {
                **base,
                "seq": 4,
                "answer": str(result["final_answer"]),
                "citations": result["citations"],
                "tokens_used": result["tokens_used"],
            },
        ),
    ]

    def stream():
        for name, payload in events:
            yield _sse_event(name, payload)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/chat/threads")
def threads(principal: CurrentPrincipal, session: SessionDep) -> dict:
    return envelope(data=AgentService(session).threads(principal=principal))
