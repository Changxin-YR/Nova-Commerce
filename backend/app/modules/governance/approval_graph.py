from __future__ import annotations

from collections.abc import Callable
from typing import TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy.orm import Session

from app.modules.governance.models import PendingAction
from app.modules.governance.service import PendingActionService


class ApprovalState(TypedDict):
    action_id: int
    status: str


def build_approval_graph(
    *,
    session_factory: Callable[[], Session],
    checkpointer: BaseCheckpointSaver,
    revalidate: Callable[[Session, PendingAction], bool],
    execute: Callable[[Session, dict], dict],
):
    def await_approval(state: ApprovalState) -> ApprovalState:
        interrupt({"action_id": str(state["action_id"]), "requires_approval": True})
        with session_factory() as session:
            action = PendingActionService(session).resume(
                action_id=state["action_id"],
                revalidate=lambda row: revalidate(session, row),
                execute=lambda payload: execute(session, payload),
            )
            return {"action_id": action.id, "status": action.status}

    graph = StateGraph(ApprovalState)
    graph.add_node("AwaitApproval", await_approval)
    graph.add_edge(START, "AwaitApproval")
    graph.add_edge("AwaitApproval", END)
    return graph.compile(checkpointer=checkpointer)
