from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from sqlalchemy import delete, func, select

from app.core.errors import (
    AgentBlockedByRiskError,
    PendingActionAlreadyDecidedError,
    PendingActionExpiredError,
    PendingActionPayloadChangedError,
    PendingActionStateInvalidError,
)
from app.modules.governance.approval_graph import build_approval_graph
from app.modules.governance.models import PendingAction
from app.modules.governance.service import PendingActionService, payload_hash
from app.shared.db.base import utc_now
from app.shared.db.models.audit import AuditRecord
from app.shared.db.session import get_session_factory

pytestmark = pytest.mark.integration


@pytest.fixture
def action(shop):
    with get_session_factory()() as session:
        row = PendingAction(merchant_id=shop.merchant_id, agent_run_id=shop.marker, action_type="TEST", tool_name="test.execute", summary="Test approval", risk_level="HIGH", status="PENDING", payload={"amount": 100}, payload_hash=payload_hash({"amount": 100}), requested_by=shop.staff_id, expires_at=utc_now() + timedelta(minutes=5))
        session.add(row)
        session.commit()
        action_id = row.id
    try:
        yield action_id
    finally:
        with get_session_factory()() as session:
            session.execute(delete(AuditRecord).where(AuditRecord.merchant_id == shop.merchant_id))
            session.execute(delete(PendingAction).where(PendingAction.id == action_id))
            session.commit()


def approve(session, shop, action):
    principal = replace(shop.staff, permissions=frozenset({"pending_action:approve"}))
    return PendingActionService(session).approve(principal=principal, action_id=action, request_hash=payload_hash({"amount": 100}), reason=None)


def test_expired_approval_is_persisted_and_cannot_resume(shop, action):
    with get_session_factory()() as session:
        session.get(PendingAction, action).expires_at = utc_now() - timedelta(seconds=1)
        session.commit()
        with pytest.raises(PendingActionExpiredError):
            approve(session, shop, action)
    with get_session_factory()() as session:
        assert session.get(PendingAction, action).status == "EXPIRED"
        with pytest.raises(PendingActionStateInvalidError):
            PendingActionService(session).resume(action_id=action, revalidate=lambda row: pytest.fail("expired action revalidated"), execute=lambda payload: pytest.fail("expired action executed"))


def test_concurrent_approval_has_one_winner_and_one_audit(shop, action):
    barrier = Barrier(4)

    def decide():
        with get_session_factory()() as session:
            barrier.wait(timeout=10)
            try:
                approve(session, shop, action)
                return "APPROVED"
            except PendingActionAlreadyDecidedError:
                return "ALREADY_DECIDED"

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda index: decide(), range(4)))
    assert results.count("APPROVED") == 1
    assert results.count("ALREADY_DECIDED") == 3
    with get_session_factory()() as session:
        assert session.scalar(select(func.count()).select_from(AuditRecord).where(AuditRecord.merchant_id == shop.merchant_id, AuditRecord.action == "pending_action.approved")) == 1


def test_resume_revalidates_before_execution(shop, action):
    with get_session_factory()() as session:
        approve(session, shop, action)
    with get_session_factory()() as session, pytest.raises(PendingActionPayloadChangedError):
        PendingActionService(session).resume(action_id=action, revalidate=lambda row: False, execute=lambda payload: pytest.fail("changed business state executed"))
    with get_session_factory()() as session:
        assert session.get(PendingAction, action).status == "FAILED"


def test_approved_action_cannot_resume_after_expiry(shop, action):
    with get_session_factory()() as session:
        approve(session, shop, action)
        session.get(PendingAction, action).expires_at = utc_now() - timedelta(seconds=1)
        session.commit()
    with get_session_factory()() as session, pytest.raises(PendingActionExpiredError):
        PendingActionService(session).resume(action_id=action, revalidate=lambda row: pytest.fail("expired action revalidated"), execute=lambda payload: pytest.fail("expired action executed"))
    with get_session_factory()() as session:
        assert session.get(PendingAction, action).status == "EXPIRED"


@pytest.mark.parametrize("expired", [False, True])
def test_langgraph_checkpoint_resume_revalidates_database(shop, action, expired):
    calls = []
    graph = build_approval_graph(
        session_factory=get_session_factory(),
        checkpointer=InMemorySaver(),
        revalidate=lambda session, row: calls.append("revalidate") is None,
        execute=lambda session, payload: {"executed": calls.append("execute") is None},
    )
    config = {"configurable": {"thread_id": f"approval-{action}"}}
    result = graph.invoke({"action_id": action, "status": "PENDING"}, config)
    assert result["__interrupt__"]
    assert calls == []
    with get_session_factory()() as session:
        assert session.get(PendingAction, action).status == "PENDING"
        approve(session, shop, action)
        if expired:
            session.get(PendingAction, action).expires_at = utc_now() - timedelta(seconds=1)
            session.commit()
    if expired:
        with pytest.raises(PendingActionExpiredError):
            graph.invoke(Command(resume=True), config)
        assert calls == []
        with pytest.raises(PendingActionStateInvalidError):
            graph.invoke(Command(resume=True), config)
        assert calls == []
    else:
        result = graph.invoke(Command(resume=True), config)
        assert result["status"] == "SUCCEEDED"
        assert calls == ["revalidate", "execute"]
        graph.invoke(Command(resume=True), config)
        assert calls == ["revalidate", "execute"]


def test_resume_rolls_back_failed_business_writes(shop, action):
    with get_session_factory()() as session:
        approve(session, shop, action)

    def fail(session, payload):
        row = session.get(PendingAction, action)
        row.summary = "partial business mutation"
        session.flush()
        raise ValueError("business execution failed")

    with get_session_factory()() as session, pytest.raises(ValueError):
        PendingActionService(session).resume(action_id=action, revalidate=lambda row: True, execute=lambda payload: fail(session, payload))
    with get_session_factory()() as session:
        row = session.get(PendingAction, action)
        assert row.status == "FAILED"
        assert row.summary == "Test approval"


def test_critical_action_cannot_be_approved_or_resumed(shop, action):
    with get_session_factory()() as session:
        session.get(PendingAction, action).risk_level = "CRITICAL"
        session.commit()
        with pytest.raises(AgentBlockedByRiskError):
            approve(session, shop, action)
        session.get(PendingAction, action).status = "APPROVED"
        session.commit()
        with pytest.raises(AgentBlockedByRiskError):
            PendingActionService(session).resume(action_id=action, revalidate=lambda row: pytest.fail("critical action revalidated"), execute=lambda payload: pytest.fail("critical action executed"))
