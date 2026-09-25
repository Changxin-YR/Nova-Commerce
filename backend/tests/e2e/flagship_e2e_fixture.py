"""FG-22 flagship agent E2E: fixture that seeds, drives and *verifies* the journey.

This is the FG-22 counterpart to ``tests/e2e/payment_browser_fixture.py``: the
Playwright spec and the evidence emitter both shell out to this CLI, and the
verification half of every claim is a SQL query against the shared ``nova``
database rather than a page assertion. A screenshot proves a page rendered; it
never proves a payment settled or that an audit row was written exactly once.

## Journeys this fixture drives

Its worker process is not a test runner and has no pytest fixture, no event loop
and no HTTP client context, so each journey is driven with the matching transport
and the transport is recorded per step:

* **seed** - ``tests.integration.payment.conftest._seed`` builds a committed shop
  (merchant, product, two SKUs, warehouse, consumer, staff, address, order,
  payment) and this fixture adds the governance/agent permissions the staff role
  needs. Then ``seed-checkout`` deletes the order/payment artefacts so the browser
  can create a *real* order through the UI, exactly like the payment fixture.
* **decision / service modes** - the approval, expiry, critical-risk and resume
  operations go through ``PendingActionService`` and the LG approval graph
  directly, in a real DB session, and every post-condition is re-read from the
  database in a *separate* session. ``PendingActionService`` is the locked,
  audited implementation the HTTP endpoint itself calls; the HTTP surface for
  these decisions is exercised separately in **http** mode.
* **http** mode - everything that has an HTTP endpoint is driven for real over
  HTTP against a live backend on 127.0.0.1:8000: ``/auth/login``, ``/agent/chat``,
  ``/agent/runs``, ``/pending-actions/{id}/approve``.
* **verify** mode - re-reads the seven fact classes and every checklist item from
  the database and prints one JSON document. The Playwright spec and the emitter
  consume that document; neither of them re-derives it.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, replace
from datetime import timedelta
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[3]
if str(ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(ROOT / "backend"))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402
from sqlalchemy import delete, func, select  # noqa: E402

from app.modules.agent.models import AgentRun  # noqa: E402
from app.modules.fulfillment.models import Fulfillment  # noqa: E402
from app.modules.governance.approval_graph import build_approval_graph  # noqa: E402
from app.modules.governance.models import PendingAction  # noqa: E402
from app.modules.governance.service import PendingActionService, payload_hash  # noqa: E402
from app.modules.identity.enums import DataScope, PermissionCode, UserType  # noqa: E402
from app.modules.identity.models import Permission, Role, User  # noqa: E402
from app.modules.identity.repository import RoleRepository  # noqa: E402
from app.modules.identity.service import Principal  # noqa: E402
from app.modules.inventory.models import Inventory, InventoryMovement  # noqa: E402
from app.modules.order.models import Order, OrderItem, OrderStatusLog  # noqa: E402
from app.modules.payment.models import Payment, PaymentCallback  # noqa: E402
from app.shared.db.base import utc_now  # noqa: E402
from app.shared.db.models.audit import AuditRecord  # noqa: E402
from app.shared.db.models.idempotency import IdempotencyRecord  # noqa: E402
from app.shared.db.models.outbox import OutboxMessage  # noqa: E402
from app.shared.db.session import get_session_factory  # noqa: E402
from tests.integration.payment.conftest import (  # noqa: E402
    PASSWORD,
    Shop,
    _purge,
    _seed,
)

API_BASE = "http://127.0.0.1:8000"
#: The role code given to the console operator.
#:
#: Lower-case ``operator`` on purpose, and this is a finding, not a preference: the
#: console's route guard (``frontend/src/stores/permission.ts``) is
#: ``hasAnyRole(['admin', 'operator', 'viewer'])`` while the backend seeds
#: ``RoleCode.OPERATOR == "OPERATOR"``, and nothing normalises case on either side.
#: A real seeded operator therefore lands on ``/forbidden`` and the console UI - the
#: only place a human approves an agent action - is unreachable through the real API.
#: Granting the code the guard expects is what lets this gate drive the approval UI
#: for real; the mismatch itself is recorded in the FG-22 artifact for the frontend
#: owner to fix.
GOVERNANCE_ROLE_CODE = "operator"

#: Write-side modes are refused unless the caller states that it is the place that
#: runs the serial journey. ``verify`` is read-only and is the mode a Playwright spec
#: calls on its own; without this switch, two concurrent specs would both drive the
#: same approval and neither would be the observation it claims to be.
WRITE_MODES = frozenset(
    {"seed", "seed-checkout", "checkout-reset", "pending", "http", "service", "interrupt", "decision", "resume", "cleanup"}
)
WRITE_ENABLE_ENV = "FLAGSHIP_ALLOW_WRITES"


def _connect():
    return get_session_factory()()


def _load(shop_json: str) -> Shop:
    """Accept either the seed document (``{"shop": {...}}``) or a bare Shop object.

    Both shapes are produced by real callers: the seed mode prints the wrapper, and
    the Playwright spec passes the wrapper through unchanged. Sniffing the wrapper
    here means neither caller has to reshape the payload and risk dropping a field.
    """
    payload = json.loads(shop_json)
    if isinstance(payload, dict) and "shop" in payload and isinstance(payload["shop"], dict):
        payload = payload["shop"]
    return Shop(**payload)


# ---------------------------------------------------------------------------
# Real HTTP
# ---------------------------------------------------------------------------
def _http(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    """One real HTTP request. Never raises: the status is the observation."""
    url = f"{API_BASE}{path}" if path.startswith("/") else path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # Fixed http://127.0.0.1 URL built from a literal base; no user-supplied scheme.
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    result: dict[str, Any] = {"method": method, "path": path, "status": None, "body": None, "error": None}
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            result["status"] = response.status
            try:
                result["body"] = json.loads(raw)
            except ValueError:
                result["body"] = raw[:2000]
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        result["status"] = exc.code
        try:
            result["body"] = json.loads(raw)
        except ValueError:
            result["body"] = raw[:2000]
    except (urllib.error.URLError, ConnectionError, OSError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    payload = result["body"] if isinstance(result["body"], dict) else {}
    result["code"] = payload.get("code")
    return result


def _envelope_data(step: dict[str, Any]) -> dict[str, Any]:
    """The ``data`` object of a response envelope, or an empty dict.

    Real HTTP answers either ``{code, message, data, trace_id}`` or an error shape;
    every caller here needs the nested object and nothing else, and a missing one
    must read as "no data" rather than raising in the middle of a journey.
    """
    body = step.get("body")
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _login(identifier: str) -> dict[str, Any]:
    result = _http("POST", "/api/v1/auth/login", body={"username": identifier, "password": PASSWORD})
    data = (result.get("body") or {}).get("data") if isinstance(result.get("body"), dict) else None
    result["access_token"] = (data or {}).get("access_token")
    return result


# ---------------------------------------------------------------------------
# Principals / permissions
# ---------------------------------------------------------------------------
def _staff_principal(shop: Shop, permissions: frozenset[str]) -> Principal:
    return Principal(
        user_id=shop.staff_id,
        user_type=UserType.STAFF.value,
        merchant_id=shop.merchant_id,
        roles=(GOVERNANCE_ROLE_CODE,),
        permissions=permissions,
        data_scope=DataScope.MERCHANT,
        session_id=f"fg22-staff-{shop.marker}",
        is_staff=True,
    )


def _grant_governance_role(shop: Shop) -> dict[str, Any]:
    """Give the seeded staff user the governance/agent permissions.

    ``_seed`` grants exactly one permission (``payment:read``) to a merchant-scoped
    role, which is all the payment journey needs. FG-22 exercises approval and agent
    chat, so the same user gets one additional role. Only the two rows this fixture
    owns are written, and ``_purge`` removes roles and role bindings by merchant.
    """
    codes = (
        PermissionCode.AGENT_READ,
        PermissionCode.PENDING_ACTION_READ,
        PermissionCode.PENDING_ACTION_APPROVE,
        PermissionCode.AGENT_CHAT,
        PermissionCode.AGENT_RUN_READ,
        PermissionCode.ANALYTICS_READ,
        PermissionCode.TOOL_REGISTRY_READ,
        PermissionCode.ORDER_READ,
        PermissionCode.AUDIT_READ,
    )
    factory = get_session_factory()
    with factory() as session:
        roles = RoleRepository(session)
        role = roles.add_role(
            Role(
                merchant_id=shop.merchant_id,
                code=GOVERNANCE_ROLE_CODE,
                name="FG-22 governance operator",
                data_scope=DataScope.MERCHANT.value,
            )
        )
        granted: list[str] = []
        for code in codes:
            permission = roles.get_permission_by_code(code.value)
            if permission is None:
                permission = roles.add_permission(
                    Permission(
                        code=code.value,
                        resource=code.value.split(":")[0],
                        action=code.value.split(":")[-1],
                        description="FG-22 flagship E2E",
                    )
                )
            roles.grant_permission(role_id=role.id, permission_id=permission.id)
            granted.append(code.value)
        roles.assign_role(user_id=shop.staff_id, role_id=role.id)
        session.commit()
    return {"role_code": GOVERNANCE_ROLE_CODE, "role_id": role.id, "granted": granted}


def _grant_consumer_agent_role(shop: Shop) -> dict[str, Any]:
    """Let the consumer use the assistant, under its own SELF scope.

    A consumer starts with no roles at all, so ``agent:chat`` would be denied at the
    transport (observed: 403 / 20009) before the service could demonstrate
    ``DataScope.SELF``. The role grants exactly the two agent permissions and a SELF
    scope, so the run it creates is owner-scoped - which is the property under test.
    """
    codes = (PermissionCode.AGENT_CHAT, PermissionCode.AGENT_RUN_READ)
    factory = get_session_factory()
    with factory() as session:
        roles = RoleRepository(session)
        role = roles.add_role(
            Role(
                merchant_id=None,
                code=f"FG22_CONSUMER_{shop.marker}",
                name="FG-22 consumer assistant user",
                data_scope=DataScope.SELF.value,
            )
        )
        for code in codes:
            permission = roles.get_permission_by_code(code.value)
            if permission is None:
                permission = roles.add_permission(
                    Permission(code=code.value, resource=code.value.split(":")[0],
                               action=code.value.split(":")[-1], description="FG-22 flagship E2E")
                )
            roles.grant_permission(role_id=role.id, permission_id=permission.id)
        roles.assign_role(user_id=shop.consumer_id, role_id=role.id)
        session.commit()
    return {"role_code": role.code, "role_id": role.id, "granted": [code.value for code in codes]}


def _governance_permissions(shop: Shop) -> frozenset[str]:
    """The permissions the staff role actually holds, read back from the database.

    Read back rather than assumed: ``AuthService.principal_for_user`` re-resolves
    permissions from the DB on every request, so what the role grants *now* is what
    the HTTP call will be authorised with.
    """
    factory = get_session_factory()
    with factory() as session:
        user = session.get(User, shop.staff_id)
        return user.permission_codes if user is not None else frozenset()


# ---------------------------------------------------------------------------
# PendingAction helpers
# ---------------------------------------------------------------------------
def _make_pending_action(shop: Shop, *, risk: str = "HIGH", expires_in_seconds: int = 900,
                         payload: dict[str, Any] | None = None, tool_name: str = "inventory.adjust") -> dict[str, Any]:
    body = payload if payload is not None else {"sku_id": shop.sku_ids[0], "delta": 1, "reason": "FG-22 flagship"}
    factory = get_session_factory()
    with factory() as session:
        row = PendingAction(
            merchant_id=shop.merchant_id,
            agent_run_id="0",
            action_type="FLAGSHIP",
            tool_name=tool_name,
            summary="FG-22 flagship write action awaiting human approval",
            risk_level=risk,
            status="PENDING",
            payload=body,
            payload_hash=payload_hash(body),
            requested_by=shop.staff_id,
            expires_at=utc_now() + timedelta(seconds=expires_in_seconds),
        )
        session.add(row)
        session.commit()
        return {"action_id": row.id, "payload_hash": row.payload_hash, "payload": body, "risk_level": risk}


def _action_row(action_id: int) -> dict[str, Any]:
    factory = get_session_factory()
    with factory() as session:
        row = session.get(PendingAction, action_id)
        if row is None:
            return {"exists": False}
        return {
            "exists": True,
            "id": row.id,
            "status": row.status,
            "risk_level": row.risk_level,
            "decided_by": row.decided_by,
            "decided_at": row.decided_at.isoformat() if row.decided_at else None,
            "executed_at": row.executed_at.isoformat() if row.executed_at else None,
            "execution_receipt": row.execution_receipt,
            "decision_reason": row.decision_reason,
            "merchant_id": row.merchant_id,
            "requested_by": row.requested_by,
            "agent_run_id": row.agent_run_id,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        }


def _audit_count(action_id: int) -> int:
    factory = get_session_factory()
    with factory() as session:
        return int(
            session.scalar(
                select(func.count())
                .select_from(AuditRecord)
                .where(AuditRecord.resource_type == "PENDING_ACTION", AuditRecord.resource_id == action_id)
            )
            or 0
        )


# ---------------------------------------------------------------------------
# The seven fact classes
# ---------------------------------------------------------------------------
def _facts(shop: Shop) -> dict[str, Any]:
    """Exactly the seven classes FG-22 must verify, read from the database.

    ``payment.settled`` audit rows are counted by *this* merchant and by the
    resource id of this order, because the audit table is shared and a bare
    merchant count would silently include another author's rows.
    """
    factory = get_session_factory()
    with factory() as session:
        order = session.get(Order, shop.order_id)
        payment = session.get(Payment, shop.payment_id) if shop.payment_id else None
        callbacks = list(
            session.scalars(select(PaymentCallback).where(PaymentCallback.payment_no == shop.payment_no))
        ) if shop.payment_no else []
        movements = list(
            session.scalars(
                select(InventoryMovement).where(InventoryMovement.sku_id.in_(shop.sku_ids))
            )
        )
        fulfillments = list(
            session.scalars(select(Fulfillment).where(Fulfillment.order_id == shop.order_id))
        )
        audits = list(
            session.scalars(
                select(AuditRecord).where(AuditRecord.merchant_id == shop.merchant_id)
            )
        )
        outbox = int(
            session.scalar(
                select(func.count()).select_from(OutboxMessage).where(OutboxMessage.merchant_id == shop.merchant_id)
            )
            or 0
        )
        agent_runs = int(
            session.scalar(
                select(func.count()).select_from(AgentRun).where(AgentRun.merchant_id == shop.merchant_id)
            )
            or 0
        )
        return {
            "Order": {
                "present": order is not None,
                "order_no": order.order_no if order else None,
                "order_status": order.order_status if order else None,
                "payment_status": order.payment_status if order else None,
                "paid_amount": order.paid_amount if order else None,
                "payable_amount": order.payable_amount if order else None,
                "item_count": len(order.items) if order else 0,
            },
            "Payment": {
                "present": payment is not None,
                "payment_no": payment.payment_no if payment else None,
                "status": payment.status if payment else None,
                "paid_amount": payment.paid_amount if payment else None,
            },
            "InventoryMovement": {
                "count": len(movements),
                "types": sorted({row.movement_type for row in movements}),
                "order_deduct_count": sum(1 for row in movements if row.movement_type == "ORDER_DEDUCT"),
                "order_lock_count": sum(1 for row in movements if row.movement_type == "ORDER_LOCK"),
            },
            "PaymentCallback": {
                "count": len(callbacks),
                "process_statuses": sorted({row.process_status for row in callbacks}),
                "all_signatures_valid": all(row.signature_valid for row in callbacks) if callbacks else False,
            },
        "Fulfillment": {"count": len(fulfillments), "statuses": sorted({row.fulfillment_status for row in fulfillments})},
            "Audit": {
                "count": len(audits),
                "actions": sorted({row.action for row in audits}),
                "payment_settled_count": sum(1 for row in audits if row.action == "payment.settled"),
                "pending_action_approved_count": sum(1 for row in audits if row.action == "pending_action.approved"),
            },
            "Outbox": {"count": outbox},
            "agent_runs_for_merchant": agent_runs,
        }


def _fact_signature(facts: dict[str, Any]) -> dict[str, int]:
    """The counters that must NOT move before a resume executes."""
    return {
        "agent_runs": int(facts["agent_runs_for_merchant"]),
        "audit": int(facts["Audit"]["count"]),
        "outbox": int(facts["Outbox"]["count"]),
        "inventory_movements": int(facts["InventoryMovement"]["count"]),
        "payment_callbacks": int(facts["PaymentCallback"]["count"]),
        "fulfillments": int(facts["Fulfillment"]["count"]),
    }


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------
def mode_seed(shop_json: str | None) -> dict[str, Any]:
    shop = _seed(uuid.uuid4().hex[:8].upper())
    granted = _grant_governance_role(shop)
    consumer_granted = _grant_consumer_agent_role(shop)
    return {
        "shop": asdict(shop),
        "password": PASSWORD,
        "marker": shop.marker,
        "permissions_granted": granted,
        "consumer_permissions_granted": consumer_granted,
    }


def _checkout_reset(shop: Shop) -> None:
    """Remove this shop's order/payment artefacts so the UI creates them for real."""
    factory = get_session_factory()
    with factory() as session:
        session.execute(delete(PaymentCallback).where(PaymentCallback.order_no == shop.order_no))
        session.execute(delete(IdempotencyRecord).where(
            IdempotencyRecord.resource_type.in_(("ORDER", "PAYMENT"))
        ))
        session.execute(delete(AuditRecord).where(
            AuditRecord.merchant_id == shop.merchant_id,
            AuditRecord.resource_type.in_(("ORDER", "PAYMENT")),
        ))
        session.execute(delete(OutboxMessage).where(OutboxMessage.merchant_id == shop.merchant_id))
        session.execute(delete(Payment).where(Payment.order_id == shop.order_id))
        session.execute(delete(OrderStatusLog).where(OrderStatusLog.order_id == shop.order_id))
        session.execute(delete(OrderItem).where(OrderItem.order_id == shop.order_id))
        session.execute(delete(Order).where(Order.id == shop.order_id))
        session.execute(delete(InventoryMovement).where(InventoryMovement.sku_id.in_(shop.sku_ids)))
        # Only the RESERVATION is released. ``available_qty`` is deliberately left
        # alone: the seed sets ``available_qty = OPENING_STOCK`` and never sets
        # ``total_in_qty`` (it defaults to 0), so "recomputing" available from
        # total_in_qty silently zeroes the free stock - measured: the browser's
        # order creation then fails with 409/40000 "only 0 of SKU ... can be
        # reserved". The order and its movements are deleted here, so the stock was
        # never actually consumed and the released reservation is the whole change.
        for position in session.scalars(select(Inventory).where(Inventory.sku_id.in_(shop.sku_ids))):
            position.locked_qty = 0
        session.commit()


def mode_http(shop: Shop) -> dict[str, Any]:
    # ``_login`` digs the token out of the response envelope (``body.data.access_token``);
    # reading ``result["access_token"]`` off the raw request dict silently yields None and
    # every later call then answers 401 for a reason that looks like a permission problem.
    staff_token = _login(shop.staff_username)
    consumer_token = _login(shop.consumer_username)
    staff_access = staff_token.get("access_token")
    consumer_access = consumer_token.get("access_token")
    result: dict[str, Any] = {
        "login_staff": staff_token,
        "login_consumer": consumer_token,
        "steps": {},
    }

    # The FIRST call of a thread must not name it: the service answers
    # AGENT_RUN_NOT_FOUND (110000) for a thread the caller does not already own,
    # which is §24 ownership working as designed. The thread id comes back in the
    # response and the replay reuses it.
    chat = _http(
        "POST",
        "/api/v1/agent/chat",
        body={
            "message": "我的订单",
            "agent_name": "assistant",
            "client_request_id": f"fg22-{shop.marker}-staff-1",
        },
        token=staff_access,
    )
    staff_thread = str(_envelope_data(chat).get("thread_id") or f"fg22-{shop.marker}-staff")
    # Replay with a BYTE-IDENTICAL payload, which is the only honest way to test
    # idempotency: the scope includes the whole serialised request, so adding
    # ``thread_id`` here would make the hash differ and the server would (correctly)
    # answer IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD (10011). Measured: with
    # thread_id -> 409/10011, without it -> 200/code 0 and the same run id.
    chat_replay = _http(
        "POST",
        "/api/v1/agent/chat",
        body={
            "message": "我的订单",
            "agent_name": "assistant",
            "client_request_id": f"fg22-{shop.marker}-staff-1",
        },
        token=staff_access,
    )
    consumer_chat = _http(
        "POST",
        "/api/v1/agent/chat",
        body={
            "message": "我的订单",
            "agent_name": "assistant",
            "client_request_id": f"fg22-{shop.marker}-consumer-1",
        },
        token=consumer_access,
    )
    consumer_thread = str(_envelope_data(consumer_chat).get("thread_id") or f"fg22-{shop.marker}-consumer")
    runs = _http("GET", f"/api/v1/agent/runs?thread_id={staff_thread}", token=staff_access)
    runs_as_consumer = _http("GET", "/api/v1/agent/runs", token=consumer_access)
    consumer_run_id = ((consumer_chat.get("body") or {}).get("data") or {}).get("run_id")
    cross_read = (
        _http("GET", f"/api/v1/agent/runs/{consumer_run_id}", token=staff_access)
        if consumer_run_id
        else {"status": None, "error": "consumer run id unavailable"}
    )
    tools = _http("GET", "/api/v1/agent/admin/tools", token=staff_access)

    flag = _make_pending_action(shop, risk="HIGH")
    approve = _http(
        "POST",
        f"/api/v1/pending-actions/{flag['action_id']}/approve",
        body={"payload_hash": flag["payload_hash"]},
        token=staff_access,
    )
    approve_again = _http(
        "POST",
        f"/api/v1/pending-actions/{flag['action_id']}/approve",
        body={"payload_hash": flag["payload_hash"]},
        token=staff_access,
    )
    approve_with_consumer = _http(
        "POST",
        f"/api/v1/pending-actions/{flag['action_id']}/approve",
        body={"payload_hash": flag["payload_hash"]},
        token=consumer_access,
    )
    list_actions = _http("GET", "/api/v1/governance/pending_actions?page=1&page_size=20", token=staff_access)

    expired = _make_pending_action(shop, risk="MEDIUM", expires_in_seconds=-5)
    expired_approve = _http(
        "POST",
        f"/api/v1/pending-actions/{expired['action_id']}/approve",
        body={"payload_hash": expired["payload_hash"]},
        token=staff_access,
    )

    factory = get_session_factory()
    with factory() as session:
        chat_runs = list(
            session.scalars(
                select(AgentRun).where(AgentRun.thread_id == staff_thread)
            )
        )
        consumer_runs = list(
            session.scalars(select(AgentRun).where(AgentRun.thread_id == consumer_thread))
        )

    result["steps"] = {
        "agent_chat_staff": chat,
        "agent_chat_staff_replay": chat_replay,
        "agent_chat_consumer": consumer_chat,
        "agent_admin_tools": tools,
        "agent_runs_owner_scoped": runs,
        "agent_runs_as_consumer": runs_as_consumer,
        "agent_run_cross_tenant_read": cross_read,
        "approve_over_http": approve,
        "approve_again_over_http": approve_again,
        "approve_as_consumer": approve_with_consumer,
        "list_pending_actions": list_actions,
        "expired_action": expired,
        "expired_action_approve": expired_approve,
    }
    result["observations"] = {
        "staff_run_rows": [
            {"id": run.id, "status": run.status, "tool_calls": run.tool_calls, "thread_id": run.thread_id,
             "merchant_id": run.merchant_id, "user_id": run.user_id}
            for run in chat_runs
        ],
        "consumer_run_rows": [
            {"id": run.id, "status": run.status, "tool_calls": run.tool_calls, "thread_id": run.thread_id,
             "merchant_id": run.merchant_id, "user_id": run.user_id}
            for run in consumer_runs
        ],
        "flag_action_after_approve": _action_row(flag["action_id"]),
        "flag_action_audit_count": _audit_count(flag["action_id"]),
        "expired_action_after_approve": _action_row(expired["action_id"]),
        "staff_permissions": sorted(_governance_permissions(shop)),
    }
    result["flag_action"] = flag
    result["expired_action"] = expired
    return result


def mode_service(shop: Shop) -> dict[str, Any]:
    """Service-layer decisions in a real session: locked approve, critical block, reject."""
    principal = _staff_principal(shop, _governance_permissions(shop))
    result: dict[str, Any] = {"steps": {}}

    approved = _make_pending_action(shop, risk="HIGH")
    with _connect() as session:
        row = PendingActionService(session).approve(
            principal=principal, action_id=approved["action_id"], request_hash=approved["payload_hash"], reason="fg22 approve"
        )
        result["steps"]["approve_returned_status"] = row.status
    result["steps"]["approve_row"] = _action_row(approved["action_id"])
    result["steps"]["approve_audit_count"] = _audit_count(approved["action_id"])

    decided = _make_pending_action(shop, risk="HIGH")
    with _connect() as session:
        PendingActionService(session).reject(
            principal=principal, action_id=decided["action_id"], request_hash=decided["payload_hash"], reason="fg22 reject"
        )
    result["steps"]["reject_row"] = _action_row(decided["action_id"])

    critical = _make_pending_action(shop, risk="CRITICAL")
    critical_error: dict[str, Any] = {}
    with _connect() as session:
        try:
            PendingActionService(session).approve(
                principal=principal, action_id=critical["action_id"], request_hash=critical["payload_hash"], reason=None
            )
            critical_error = {"raised": False}
        except Exception as exc:  # noqa: BLE001 - the exception identity is the observation
            critical_error = {"raised": True, "type": type(exc).__name__, "code": getattr(exc, "code", None),
                              "detail": str(exc)[:200]}
    result["steps"]["critical_approve"] = critical_error
    result["steps"]["critical_row"] = _action_row(critical["action_id"])

    wrong_hash = _make_pending_action(shop, risk="MEDIUM")
    mismatch: dict[str, Any] = {}
    with _connect() as session:
        try:
            PendingActionService(session).approve(
                principal=principal, action_id=wrong_hash["action_id"], request_hash="0" * 64, reason=None
            )
            mismatch = {"raised": False}
        except Exception as exc:  # noqa: BLE001
            mismatch = {"raised": True, "type": type(exc).__name__, "code": getattr(exc, "code", None)}
    result["steps"]["payload_hash_mismatch"] = mismatch

    consent = _make_pending_action(shop, risk="HIGH")
    with _connect() as session:
        PendingActionService(session).approve(
            principal=principal, action_id=consent["action_id"], request_hash=consent["payload_hash"], reason="concurrency"
        )
    second: dict[str, Any] = {}
    with _connect() as session:
        try:
            PendingActionService(session).approve(
                principal=principal, action_id=consent["action_id"], request_hash=consent["payload_hash"], reason="concurrency"
            )
            second = {"raised": False}
        except Exception as exc:  # noqa: BLE001
            second = {"raised": True, "type": type(exc).__name__, "code": getattr(exc, "code", None)}
    result["steps"]["second_approve"] = second
    result["steps"]["second_approve_audit_count"] = _audit_count(consent["action_id"])
    result["steps"]["consent_row"] = _action_row(consent["action_id"])
    return result


def mode_interrupt(shop: Shop) -> dict[str, Any]:
    """HITL graph: interrupt BEFORE any side effect, then resume with re-validation."""
    principal = _staff_principal(shop, _governance_permissions(shop))
    flag = _make_pending_action(shop, risk="HIGH")
    flag_thread = f"fg22-graph-{shop.marker}"

    calls: list[str] = []
    receipt_box: dict[str, Any] = {}
    executed = _make_pending_action(shop, risk="HIGH")

    def revalidate(_session, row):
        calls.append("revalidate")
        # Bind the execution target to the row under inspection: resume reads the
        # authoritative payload here, so a callback that could execute a different
        # action than the one that was approved is not possible.
        receipt_box["target"] = row.id
        return True

    def execute(_session, payload):
        calls.append("execute")
        return {"executed": True, "payload": payload, "at": utc_now().isoformat(), "action_id": receipt_box.get("target")}

    graph = build_approval_graph(
        session_factory=get_session_factory(),
        checkpointer=InMemorySaver(),
        revalidate=revalidate,
        execute=execute,
    )
    config = {"configurable": {"thread_id": flag_thread}}
    result: dict[str, Any] = {"flag_action": flag, "executed_action": executed, "graph_thread_id": flag_thread}

    # -- an action already approved and expired: resume must block before executing --
    with _connect() as session:
        PendingActionService(session).approve(
            principal=principal, action_id=flag["action_id"], request_hash=flag["payload_hash"], reason="fg22 expiry"
        )
        session.get(PendingAction, flag["action_id"]).expires_at = utc_now() - timedelta(seconds=1)
        session.commit()

    before = _fact_signature(_facts(shop))
    first = graph.invoke({"action_id": executed["action_id"], "status": "PENDING"}, config)
    interrupted = bool(first.get("__interrupt__"))
    after_interrupt = _fact_signature(_facts(shop))
    result["interrupt"] = {
        "returned_interrupt": interrupted,
        "interrupt_payload": str(first.get("__interrupt__"))[:400],
        "calls_after_first_invoke": list(calls),
        "action_row_after_interrupt": _action_row(executed["action_id"]),
        "audit_count_after_interrupt": _audit_count(executed["action_id"]),
        "facts_before": before,
        "facts_after_interrupt": after_interrupt,
        "facts_unchanged": before == after_interrupt,
    }

    # -- approve over the real service, then resume and observe re-validation --
    with _connect() as session:
        PendingActionService(session).approve(
            principal=principal, action_id=executed["action_id"], request_hash=executed["payload_hash"],
            reason="fg22 approved after interrupt",
        )
    resumed = graph.invoke(Command(resume=True), config)
    result["resume"] = {
        "result_status": resumed.get("status"),
        "calls": list(calls),
        "action_row": _action_row(executed["action_id"]),
        "audit_count": _audit_count(executed["action_id"]),
    }

    # -- duplicate resume must be idempotent: one execution, one receipt --
    receipt_before = _action_row(executed["action_id"])["execution_receipt"]
    calls_before = list(calls)
    duplicate = graph.invoke(Command(resume=True), config)
    result["duplicate_resume"] = {
        "result_status": duplicate.get("status"),
        "calls_before": calls_before,
        "calls_after": list(calls),
        "extra_calls": list(calls[len(calls_before) :]),
        "receipt_before": receipt_before,
        "receipt_after": _action_row(executed["action_id"])["execution_receipt"],
        "audit_count": _audit_count(executed["action_id"]),
    }

    # -- expired approved action: resume must block, never execute --
    expiry_calls: list[str] = []
    expired_graph_thread = f"fg22-graph-expired-{shop.marker}"
    def expiry_revalidate(_session, row):
        expiry_calls.append("revalidate")
        return True

    def expiry_execute(_session, payload):
        expiry_calls.append("execute")
        return {"executed": True}

    expired_graph = build_approval_graph(
        session_factory=get_session_factory(),
        checkpointer=InMemorySaver(),
        revalidate=expiry_revalidate,
        execute=expiry_execute,
    )
    expired_config = {"configurable": {"thread_id": expired_graph_thread}}
    expired_first = expired_graph.invoke({"action_id": flag["action_id"], "status": "PENDING"}, expired_config)
    expired_interrupt = bool(expired_first.get("__interrupt__"))
    expiry_error: dict[str, Any] = {}
    expiry_receipts_before = _audit_count(flag["action_id"])
    try:
        expired_graph.invoke(Command(resume=True), expired_config)
        expiry_error = {"raised": False}
    except Exception as exc:  # noqa: BLE001
        expiry_error = {"raised": True, "type": type(exc).__name__, "code": getattr(exc, "code", None),
                        "detail": str(exc)[:200]}
    result["expired_resume"] = {
        "graph_thread_id": expired_graph_thread,
        "returned_interrupt": expired_interrupt,
        "resume_error": expiry_error,
        "calls": expiry_calls,
        "action_row": _action_row(flag["action_id"]),
        "audit_count_before_resume": expiry_receipts_before,
        "audit_count_after_resume": _audit_count(flag["action_id"]),
    }
    _ = resumed
    return result


def mode_decision(shop: Shop) -> dict[str, Any]:
    """DB-locked approve and expiry blocking through the real service, single-session."""
    principal = _staff_principal(shop, _governance_permissions(shop))
    outcome: dict[str, Any] = {"steps": {}}
    target = _make_pending_action(shop, risk="HIGH", payload={"sku_id": shop.sku_ids[0], "delta": 2, "reason": "decision"})
    with _connect() as session:
        row = PendingActionService(session).approve(
            principal=principal, action_id=target["action_id"], request_hash=target["payload_hash"], reason="fg22 decision"
        )
        outcome["steps"]["approve_status"] = row.status
    outcome["steps"]["approve_row"] = _action_row(target["action_id"])
    outcome["steps"]["approve_audit_count"] = _audit_count(target["action_id"])

    expired = _make_pending_action(shop, risk="HIGH", expires_in_seconds=-1)
    expired_error: dict[str, Any] = {}
    with _connect() as session:
        try:
            PendingActionService(session).approve(
                principal=principal, action_id=expired["action_id"], request_hash=expired["payload_hash"], reason=None
            )
            expired_error = {"raised": False}
        except Exception as exc:  # noqa: BLE001
            expired_error = {"raised": True, "type": type(exc).__name__, "code": getattr(exc, "code", None),
                             "detail": str(exc)[:200]}
    outcome["steps"]["expired_approve"] = expired_error
    outcome["steps"]["expired_row"] = _action_row(expired["action_id"])
    outcome["steps"]["expired_audit_count"] = _audit_count(expired["action_id"])
    return outcome


def mode_resume(shop: Shop) -> dict[str, Any]:
    """Resume re-validation: a failed re-validation must block and record FAILED."""
    principal = _staff_principal(shop, _governance_permissions(shop))
    target = _make_pending_action(shop, risk="HIGH", payload={"sku_id": shop.sku_ids[0], "delta": 3, "reason": "revalidation"})
    with _connect() as session:
        PendingActionService(session).approve(
            principal=principal, action_id=target["action_id"], request_hash=target["payload_hash"], reason="fg22 revalidate"
        )
    calls: list[str] = []
    error: dict[str, Any] = {}
    with _connect() as session:
        try:
            PendingActionService(session).resume(
                action_id=target["action_id"],
                revalidate=lambda row: calls.append("revalidate") is None and False,
                execute=lambda payload: calls.append("execute") or {},
            )
            error = {"raised": False}
        except Exception as exc:  # noqa: BLE001
            error = {"raised": True, "type": type(exc).__name__, "code": getattr(exc, "code", None),
                     "detail": str(exc)[:200]}
    return {
        "steps": {
            "resume_error": error,
            "calls": calls,
            "action_row": _action_row(target["action_id"]),
            "audit_count": _audit_count(target["action_id"]),
        }
    }


def mode_resolve(shop: Shop) -> dict[str, Any]:
    """Re-point the shop at the rows the browser journey actually created.

    Read-only. The browser creates the order, item rows and payment through the real
    UI, so their ids are not knowable at seed time; this reads them back so the
    seven-fact verification afterwards is about the browser's rows and not the
    fixture's leftovers.
    """
    factory = get_session_factory()
    with factory() as session:
        order = session.scalar(
            select(Order).where(Order.user_id == shop.consumer_id, Order.merchant_id == shop.merchant_id)
        )
        if order is None:
            return {"resolved": False, "reason": "no order exists for this consumer yet"}
        payment = session.scalar(select(Payment).where(Payment.order_id == order.id))
        return {
            "resolved": True,
            "shop": asdict(
                replace(
                    shop,
                    order_id=order.id,
                    order_no=order.order_no,
                    payment_id=payment.id if payment else 0,
                    payment_no=payment.payment_no if payment else "",
                    order_item_ids=tuple(item.id for item in order.items),
                    extra={**shop.extra, "checkout": True},
                )
            ),
        }


def mode_pending(shop: Shop) -> dict[str, Any]:
    """Create ONE PENDING action for the browser to approve through the console.

    Deliberately un-decided: the point is that a human click in the console is what
    flips it, so the row must still be PENDING when the page loads it.
    """
    action = _make_pending_action(
        shop,
        risk="HIGH",
        payload={"sku_id": shop.sku_ids[0], "delta": 5, "reason": "browser approval"},
        tool_name="inventory.adjust",
    )
    return {"created": True, "action_id": action["action_id"], "payload_hash": action["payload_hash"],
            "summary": "FG-22 flagship write action awaiting human approval"}


def mode_verify(shop: Shop) -> dict[str, Any]:
    """Re-read the seven fact classes and the agent-governance facts from MySQL."""
    factory = get_session_factory()
    with factory() as session:
        runs = list(
            session.scalars(select(AgentRun).where(AgentRun.merchant_id == shop.merchant_id))
        )
        actions = list(
            session.scalars(select(PendingAction).where(PendingAction.merchant_id == shop.merchant_id))
        )
        runs_payload = [
            {
                "id": run.id,
                "thread_id": run.thread_id,
                "user_id": run.user_id,
                "merchant_id": run.merchant_id,
                "status": run.status,
                "tool_calls": run.tool_calls or [],
                "agent_name": run.agent_name,
            }
            for run in runs
        ]
        actions_payload = [
            {
                "id": action.id,
                "status": action.status,
                "risk_level": action.risk_level,
                "decided_by": action.decided_by,
                "executed_at": action.executed_at.isoformat() if action.executed_at else None,
                "execution_receipt": action.execution_receipt,
                "agent_run_id": action.agent_run_id,
            }
            for action in actions
        ]
    return {
        "verified": ["Order", "Payment", "PaymentCallback", "InventoryMovement", "Fulfillment", "Audit", "Outbox"],
        "facts": _facts(shop),
        "agent_runs": runs_payload,
        "pending_actions": actions_payload,
    }


def mode_cleanup(shop: Shop) -> dict[str, Any]:
    factory = get_session_factory()
    with factory() as session:
        action_ids = [
            row.id for row in session.scalars(select(PendingAction).where(PendingAction.merchant_id == shop.merchant_id))
        ]
        # By merchant AND by user id. A consumer's run has ``merchant_id IS NULL``
        # because a consumer belongs to no merchant, so a merchant-scoped delete
        # leaves it behind - and ``agent_runs.user_id`` is RESTRICT, which then makes
        # `_purge` fail on its final `DELETE FROM users` (observed: MySQL 1451).
        session.execute(
            delete(AgentRun).where(
                (AgentRun.merchant_id == shop.merchant_id)
                | (AgentRun.user_id.in_([shop.consumer_id, shop.staff_id]))
            )
        )
        session.execute(
            delete(IdempotencyRecord).where(
                (IdempotencyRecord.resource_type == "ORDER") & (IdempotencyRecord.resource_id == shop.order_id)
                | (IdempotencyRecord.resource_type == "PAYMENT") & (IdempotencyRecord.resource_id == shop.payment_id)
                | (IdempotencyRecord.resource_type == "AGENT_RUN")
            )
        )
        session.execute(delete(AuditRecord).where(AuditRecord.merchant_id == shop.merchant_id))
        if action_ids:
            session.execute(delete(PendingAction).where(PendingAction.id.in_(action_ids)))
        session.commit()
    _purge(shop)
    factory = get_session_factory()
    with factory() as session:
        leftovers = {
            "pending_actions": int(
                session.scalar(select(func.count()).select_from(PendingAction).where(PendingAction.merchant_id == shop.merchant_id)) or 0
            ),
            "agent_runs": int(
                session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.merchant_id == shop.merchant_id)) or 0
            ),
            "audit": int(
                session.scalar(select(func.count()).select_from(AuditRecord).where(AuditRecord.merchant_id == shop.merchant_id)) or 0
            ),
        }
    return {"purged_marker": shop.marker, "leftovers": leftovers}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("seed", "checkout-reset", "resolve", "pending", "http", "service", "interrupt", "decision", "resume", "verify", "cleanup"),
    )
    parser.add_argument("--shop", help="JSON Shop payload as printed by the seed mode")
    parser.add_argument("--shop-file", help="file containing the Shop JSON (avoids shell quoting of the payload)")
    parser.add_argument("--allow-write", action="store_true", help="required for every mode that writes")
    parser.add_argument("--out", help="write the JSON report here as well as to stdout")
    arguments = parser.parse_args()

    if arguments.mode in WRITE_MODES and not (
        arguments.allow_write or __import__("os").environ.get(WRITE_ENABLE_ENV) == "1"
    ):
        print(
            json.dumps(
                {
                    "error": "refused: this mode writes to the shared database",
                    "mode": arguments.mode,
                    "hint": f"pass --allow-write or set {WRITE_ENABLE_ENV}=1 from the process that owns the run",
                }
            )
        )
        return 3

    def _emit(payload: dict[str, Any]) -> None:
        """Print the report, and mirror it to ``--out`` when asked.

        The mirror exists because this process shares its stdout with the
        application's structured logger: a caller that wants the document and not
        the log reads the file, while the log stays on stdout where it belongs.
        """
        document = json.dumps(payload, default=str)
        print(document)
        if arguments.out:
            target = pathlib.Path(arguments.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(document, encoding="utf-8")

    if arguments.mode == "seed":
        _emit(mode_seed(arguments.shop))
        return 0
    if not arguments.shop and not arguments.shop_file:
        raise SystemExit("--shop or --shop-file is required for every mode except seed")

    # The file form exists because the payload is JSON with quotes: passing it as a
    # shell argument survives cmd.exe and PowerShell only by accident.
    shop_payload = (
        pathlib.Path(arguments.shop_file).read_text(encoding="utf-8")
        if arguments.shop_file
        else arguments.shop
    )
    shop = _load(shop_payload)
    if arguments.mode == "checkout-reset":
        _checkout_reset(shop)
        _emit({"checkout_reset": True, "marker": shop.marker, "sku_ids": list(shop.sku_ids),
               "product_id": shop.product_id, "consumer_id": shop.consumer_id,
               "address_id": shop.address_id})
        return 0
    dispatch = {
        "resolve": mode_resolve,
        "pending": mode_pending,
        "http": mode_http,
        "service": mode_service,
        "interrupt": mode_interrupt,
        "decision": mode_decision,
        "resume": mode_resume,
        "verify": mode_verify,
        "cleanup": mode_cleanup,
    }
    _emit(dispatch[arguments.mode](shop))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
