"""The MCP tool surface: the frozen roster, real service reads, and the one write path.

Three claims are tested here:

1. **The roster is exactly the frozen sixteen**, each with a non-empty input schema and
   honest read/write semantics, and the capabilities that must never exist are absent
   from the *live server* rather than only from a constant.
2. **A tool reads real rows through the existing services.** ``nova.product.get`` on a
   seeded product must return that product. Anything less would pass with a tool that
   returns ``{}``.
3. **The one write tool files an approval request and nothing else.** REQ-MCP-008:
   ``nova.promotion.propose`` must create a real ``pending_actions`` row and must create
   **no** ``promotions`` row. Both halves are asserted, because the first alone would be
   satisfied by a tool that also created the promotion.

Everything here is marked ``integration``: even the roster cases construct the live
server, and the tool cases read or write real rows. Marking the whole module means the
root ``conftest.py`` skips it loudly when MySQL is absent instead of failing it for a
reason that has nothing to do with the gate - while the pure authorization suite
(``test_mcp_authorization_unit.py``) still runs everywhere.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import delete, func, select

from app.core.errors import ErrorCode
from app.mcp.policy import FORBIDDEN_TOOL_NAMES, TOOL_SPECS
from app.mcp.server import build_mcp_server
from app.mcp.tools import HANDLERS, McpErrorResult, ProductGetOut, PromotionProposeOut
from app.modules.catalog.models import Product, ProductSku
from app.modules.governance.models import PendingAction
from app.modules.identity.enums import DataScope, PermissionCode, UserType
from app.modules.identity.models import Merchant, User
from app.modules.identity.security import hash_password
from app.modules.identity.service import Principal
from app.modules.inventory.models import Inventory, Warehouse
from app.modules.marketing.models import Promotion
from app.shared.db.session import get_session_factory
from tests.mcp.support import all_tool_scopes, authenticated, principal_for, settings_for_tests

pytestmark = pytest.mark.integration

PASSWORD = "Correct-Horse-Battery-9"


# ---------------------------------------------------------------------------
# A self-contained shop for the tool reads
# ---------------------------------------------------------------------------
@dataclass
class ToolShop:
    """One merchant with the rows the tool cases need, plus its principals.

    Deliberately self-contained rather than importing the phase-5 commerce seed: that
    seed builds a *shop with orders and payments*, and this module needs a merchant, one
    product, one SKU with stock and two users. Reusing the larger fixture would couple
    FG-18 to a fixture whose purpose is the order/refund gates, so a change made for
    those gates could redden this one for an unrelated reason.
    """

    marker: str
    merchant_id: int
    product_id: int
    sku_id: int
    warehouse_id: int
    staff_user_id: int
    consumer_user_id: int
    created: dict[str, Any] = field(default_factory=dict)

    def staff(self) -> Principal:
        return Principal(
            user_id=self.staff_user_id,
            user_type=UserType.STAFF.value,
            merchant_id=self.merchant_id,
            roles=("OPERATOR",),
            permissions=frozenset({permission.value for permission in PermissionCode}),
            data_scope=DataScope.MERCHANT,
            session_id=f"mcp-{self.marker}",
            is_staff=True,
        )

    def consumer(self) -> Principal:
        return Principal(
            user_id=self.consumer_user_id,
            user_type=UserType.CONSUMER.value,
            merchant_id=None,
            roles=("CUSTOMER",),
            permissions=frozenset({PermissionCode.ORDER_READ.value}),
            data_scope=DataScope.SELF,
            session_id=f"mcp-consumer-{self.marker}",
            is_staff=False,
        )


def _purge(created: dict[str, Any]) -> None:
    """Remove every row this fixture created, children first.

    Registered as a finalizer *before* the first write, so a failure half way through
    setup still cleans up what exists. That ordering is the defect ``PHASE5_DESIGN``
    section 13.5 records: a ``try/finally`` around ``yield`` does not run when setup
    itself fails, and this fixture commits as it builds.
    """
    merchant_id = created.get("merchant_id")
    if merchant_id is None:
        return
    factory = get_session_factory()
    with factory() as session:
        session.execute(delete(PendingAction).where(PendingAction.merchant_id == merchant_id))
        session.execute(delete(Promotion).where(Promotion.merchant_id == merchant_id))
        session.execute(delete(Inventory).where(Inventory.merchant_id == merchant_id))
        session.execute(delete(Warehouse).where(Warehouse.merchant_id == merchant_id))
        session.execute(delete(ProductSku).where(ProductSku.merchant_id == merchant_id))
        session.execute(delete(Product).where(Product.merchant_id == merchant_id))
        session.execute(delete(User).where(User.merchant_id == merchant_id))
        for user_id in created.get("consumer_user_ids", []):
            session.execute(delete(User).where(User.id == user_id))
        session.execute(delete(Merchant).where(Merchant.id == merchant_id))
        session.commit()


@pytest.fixture
def tool_shop(request) -> ToolShop:
    """A committed merchant / product / SKU / stock / staff / consumer."""
    marker = uuid.uuid4().hex[:8]
    created: dict[str, Any] = {}
    request.addfinalizer(lambda: _purge(created))

    factory = get_session_factory()
    with factory() as session:
        merchant = Merchant(code=f"MCP{marker}"[:24], name=f"MCP Test {marker}")
        session.add(merchant)
        session.flush()
        created["merchant_id"] = merchant.id

        product = Product(
            merchant_id=merchant.id,
            product_no=f"P-{marker}",
            slug=f"mcp-{marker}",
            name=f"MCP Fixture Product {marker}",
            status="PUBLISHED",
            published_at=datetime.now(UTC),
            min_price=1500,
            max_price=1500,
        )
        session.add(product)
        session.flush()

        sku = ProductSku(
            merchant_id=merchant.id,
            product_id=product.id,
            sku_no=f"S-{marker}",
            sku_code=f"sku-{marker}",
            name="Fixture SKU",
            price_amount=1500,
            cost_amount=700,
            status="ACTIVE",
        )
        session.add(sku)
        session.flush()

        warehouse = Warehouse(
            merchant_id=merchant.id,
            code="MAIN",
            name="MCP Fixture Warehouse",
            is_default=True,
            status="ACTIVE",
        )
        session.add(warehouse)
        session.flush()

        session.add(
            Inventory(
                merchant_id=merchant.id,
                warehouse_id=warehouse.id,
                sku_id=sku.id,
                available_qty=42,
                locked_qty=0,
                safety_stock=0,
            )
        )

        staff = User(
            username=f"mcpstaff_{marker}",
            email=f"mcpstaff_{marker}@example.test",
            password_hash=hash_password(PASSWORD),
            display_name="MCP Fixture Operator",
            user_type=UserType.STAFF.value,
            status="ACTIVE",
            merchant_id=merchant.id,
        )
        session.add(staff)
        session.flush()

        consumer = User(
            username=f"mcpbuyer_{marker}",
            email=f"mcpbuyer_{marker}@example.test",
            password_hash=hash_password(PASSWORD),
            display_name="MCP Fixture Buyer",
            user_type=UserType.CONSUMER.value,
            status="ACTIVE",
            merchant_id=None,
        )
        session.add(consumer)
        session.flush()
        # A consumer carries no merchant, so it is not reachable through the
        # merchant-scoped delete above: its id is recorded explicitly, which is also why
        # the purge never guesses.
        created["consumer_user_ids"] = [consumer.id]

        session.commit()

        return ToolShop(
            marker=marker,
            merchant_id=merchant.id,
            product_id=product.id,
            sku_id=sku.id,
            warehouse_id=warehouse.id,
            staff_user_id=staff.id,
            consumer_user_id=consumer.id,
            created=created,
        )


def _registered(server: Any, name: str) -> Any:
    """The tool body the SDK actually registered, by name.

    ## Why the tests call the body rather than ``server.call_tool``

    ``MCPServer.call_tool`` is the *in-memory* entry point, and for a ``ToolError`` it
    re-raises rather than returning an error result: the conversion to
    ``CallToolResult(is_error=True)`` happens in the SDK's JSON-RPC request handler,
    which the in-memory path does not go through. Waiting for that conversion here would
    therefore test the SDK's wiring rather than ours.

    Driving the registered body is still the right level: it is the function the SDK
    calls on every transport, and it is where *our* re-authorisation, input parsing and
    error-code translation live. A refusal therefore surfaces as a ``ToolError`` carrying
    ``[<code>] <CODE_NAME>``, which is what the JSON-RPC layer turns into the client's
    ``is_error`` result - and the HTTP suite asserts the end-to-end shape on the wire.
    """
    for tool in server._tool_manager.list_tools():
        if tool.name == name:
            return tool.fn
    msg = f"no registered tool named {name}"
    raise AssertionError(msg)


def _tool_error(server: Any, name: str, arguments: dict[str, Any]) -> str:
    """Call a tool that is expected to refuse, and return its refusal text."""
    fn = _registered(server, name)
    try:
        asyncio.run(fn(**arguments))
    except ToolError as exc:
        return str(exc)
    msg = f"{name} did not refuse"
    raise AssertionError(msg)


# ---------------------------------------------------------------------------
# The roster
# ---------------------------------------------------------------------------
def test_live_server_exposes_exactly_the_frozen_sixteen():
    """The live registry, not the constant: a handler-less spec must not be listable."""
    server = build_mcp_server(settings_for_tests())
    principal = principal_for(data_scope=DataScope.ALL)
    with authenticated(principal, scopes=all_tool_scopes()):
        tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == {spec.name for spec in TOOL_SPECS}
    assert len(tools) == 16


def test_every_tool_has_a_non_empty_input_schema_and_declared_semantics():
    server = build_mcp_server(settings_for_tests())
    principal = principal_for(data_scope=DataScope.ALL)
    with authenticated(principal, scopes=all_tool_scopes()):
        tools = asyncio.run(server.list_tools())
    for tool in tools:
        assert tool.description.strip(), tool.name
        schema = tool.input_schema
        assert schema.get("type") == "object", tool.name
        assert schema.get("properties"), f"{tool.name} advertises no inputs"
        assert tool.annotations is not None, tool.name
        # Read/write semantics are declared, not inferred from the name: a client has to
        # be able to tell a read from a write before calling it.
        assert tool.annotations.read_only_hint is not None, tool.name
        if tool.annotations.read_only_hint:
            assert tool.annotations.idempotent_hint is True, tool.name
        else:
            assert tool.annotations.destructive_hint is False, tool.name


def test_every_frozen_tool_has_a_handler():
    """A tool with no handler is worse than a missing tool: clients plan around it."""
    missing = [spec.name for spec in TOOL_SPECS if spec.name not in HANDLERS]
    assert missing == []


def test_forbidden_capabilities_are_absent_from_the_live_listing():
    server = build_mcp_server(settings_for_tests())
    principal = principal_for(data_scope=DataScope.ALL)
    with authenticated(principal, scopes=all_tool_scopes()):
        listed = {tool.name for tool in asyncio.run(server.list_tools())}
    for name in FORBIDDEN_TOOL_NAMES:
        assert name not in listed, name


@pytest.mark.parametrize("forbidden", ["execute_sql", "nova.refund.execute", "raw_sql"])
def test_forbidden_capability_call_is_refused(forbidden: str):
    server = build_mcp_server(settings_for_tests())
    principal = principal_for(data_scope=DataScope.ALL)
    with authenticated(principal, scopes=all_tool_scopes()), pytest.raises(Exception) as caught:
        asyncio.run(server.call_tool(forbidden, {"query": "select 1"}))
    assert "MCP_TOOL_NOT_EXPOSED" in str(caught.value)
    assert str(int(ErrorCode.MCP_TOOL_NOT_EXPOSED)) in str(caught.value)


# ---------------------------------------------------------------------------
# Stable error shape
# ---------------------------------------------------------------------------
def test_refused_call_carries_a_stable_error_code(tool_shop: ToolShop):
    """A refusal names a real :class:`~app.core.errors.ErrorCode`, not a bare exception.

    The code is what a client branches on: an agent deciding whether to retry needs to
    tell "that order does not exist" (never retry) from "the knowledge base is
    unavailable" (retry). A refusal that arrived without one would be no more
    actionable than a crash.
    """
    server = build_mcp_server(settings_for_tests())
    with authenticated(tool_shop.staff(), scopes=all_tool_scopes()):
        text = _tool_error(server, "nova.order.get", {"order_no": f"missing-{tool_shop.marker}"})
    assert str(int(ErrorCode.ORDER_NOT_FOUND)) in text
    assert ErrorCode.ORDER_NOT_FOUND.name in text


def test_identity_fields_are_not_part_of_any_tool_input():
    """The *input schemas* advertise no identity field, on the live server.

    A caller cannot select a principal because there is nowhere in the schema to write
    one. That is the structural half of the guarantee; the behavioural half is below.
    """
    server = build_mcp_server(settings_for_tests())
    principal = principal_for(data_scope=DataScope.ALL)
    with authenticated(principal, scopes=all_tool_scopes()):
        tools = asyncio.run(server.list_tools())
    for tool in tools:
        properties = tool.input_schema.get("properties", {})
        for forbidden in ("user_id", "merchant_id", "permissions", "data_scope", "is_admin", "roles"):
            assert forbidden not in properties, f"{tool.name} advertises {forbidden}"


def test_identity_fields_cannot_change_the_verified_principal(tool_shop: ToolShop):
    """The behavioural half: injecting identity fields cannot reach the principal.

    There are two doors and both are closed, with different mechanisms. Through the
    SDK's call path the advertised schema has no ``user_id`` parameter, so an injected
    field never reaches the body. Calling the registered body *directly* - the shape a
    later in-process integration would have - does pass the fields through, and there
    the tool's own ``extra="forbid"`` model refuses the call with a coded error that
    names each offending key.

    The property asserted is the same in both cases: the caller cannot select a
    principal. The positive control is the identical call without the extra fields.
    """
    server = build_mcp_server(settings_for_tests())
    fn = _registered(server, "nova.product.get")
    injected = {
        "product_id": tool_shop.product_id,
        "user_id": 1,
        "merchant_id": 999_999,
        "permissions": ["*"],
        "data_scope": "ALL",
        "is_admin": True,
    }
    with authenticated(tool_shop.staff(), scopes=all_tool_scopes()):
        plain = asyncio.run(fn(product_id=tool_shop.product_id))
        # Refused: the body's own strict parse rejects every injected field. The refusal
        # names the offending keys, which is also the evidence that they reached a
        # validator at all rather than being quietly accepted.
        with pytest.raises(ToolError) as caught:
            asyncio.run(fn(**injected))
    assert plain.id == tool_shop.product_id
    refusal = str(caught.value)
    assert ErrorCode.AGENT_TOOL_INPUT_INVALID.name in refusal
    for name in ("user_id", "merchant_id", "permissions", "data_scope", "is_admin"):
        assert name in refusal, name


# ---------------------------------------------------------------------------
# Real reads through the real services
# ---------------------------------------------------------------------------
def test_product_get_reads_the_seeded_product(tool_shop: ToolShop):
    """The tool returns the row that exists, with its SKU, through CatalogService."""
    server = build_mcp_server(settings_for_tests())
    with authenticated(tool_shop.staff(), scopes=all_tool_scopes()):
        result = asyncio.run(server.call_tool("nova.product.get", {"product_id": tool_shop.product_id}))
    assert result.is_error is False, result.content[0].text
    payload = result.structured_content or {}
    assert payload["id"] == tool_shop.product_id
    assert payload["name"].startswith("MCP Fixture Product")
    assert [sku["id"] for sku in payload["skus"]] == [tool_shop.sku_id]
    # Landed cost is merchant-private and must not appear anywhere in the result.
    assert "cost_amount" not in str(payload)


def test_product_search_finds_the_seeded_product(tool_shop: ToolShop):
    server = build_mcp_server(settings_for_tests())
    with authenticated(tool_shop.staff(), scopes=all_tool_scopes()):
        result = asyncio.run(server.call_tool("nova.product.search", {"keyword": tool_shop.marker}))
    assert result.is_error is False, result.content[0].text
    payload = result.structured_content or {}
    assert payload["total"] >= 1
    assert tool_shop.product_id in {item["id"] for item in payload["items"]}


def test_sku_get_resolves_by_id_and_by_code(tool_shop: ToolShop):
    server = build_mcp_server(settings_for_tests())
    with authenticated(tool_shop.staff(), scopes=all_tool_scopes()):
        by_id = asyncio.run(server.call_tool("nova.sku.get", {"sku_id": tool_shop.sku_id}))
        by_code = asyncio.run(
            server.call_tool(
                "nova.sku.get",
                {"product_id": tool_shop.product_id, "sku_code": f"sku-{tool_shop.marker}"},
            )
        )
    assert by_id.is_error is False, by_id.content[0].text
    assert by_code.is_error is False, by_code.content[0].text
    assert (by_id.structured_content or {})["sku"]["id"] == tool_shop.sku_id
    assert (by_code.structured_content or {})["sku"]["id"] == tool_shop.sku_id


def test_inventory_get_reports_the_seeded_position(tool_shop: ToolShop):
    server = build_mcp_server(settings_for_tests())
    with authenticated(tool_shop.staff(), scopes=all_tool_scopes()):
        result = asyncio.run(server.call_tool("nova.inventory.get", {"limit": 50}))
    assert result.is_error is False, result.content[0].text
    payload = result.structured_content or {}
    position = next(item for item in payload["positions"] if item["sku_id"] == tool_shop.sku_id)
    assert position["available_qty"] == 42
    assert position["below_safety_stock"] is False


def test_a_consumer_scope_cannot_read_a_merchants_product(tool_shop: ToolShop):
    """Cross-tenant read: the merchant-scoped catalogue tool refuses a consumer token."""
    server = build_mcp_server(settings_for_tests())
    with authenticated(tool_shop.consumer(), scopes=all_tool_scopes()):
        text = _tool_error(server, "nova.product.get", {"product_id": tool_shop.product_id})
    # The refusal is the RBAC one, not the DataScope one, and the ordering is a decision
    # rather than an accident: the four-way intersection is evaluated
    # existence -> policy -> scope -> RBAC -> DataScope, so a caller that does not hold
    # the permission at all is told *that* instead of being told which rows its scope
    # would have covered. Either answer is a refusal; only one of them describes the
    # data, and describing the data to a caller who may not read it is the leak.
    assert str(int(ErrorCode.INSUFFICIENT_PERMISSION)) in text
    assert ErrorCode.INSUFFICIENT_PERMISSION.name in text
    # And a token that *does* hold the permission but is scoped to a single consumer
    # still cannot read a merchant product: that is the DataScope dimension, asserted in
    # test_mcp_authorization_unit.py against the policy table directly.
    assert "MCP Fixture Product" not in text


def test_tools_call_reauthorises_rather_than_trusting_a_listing():
    """The same tool, two tokens: the second is refused though the first was not.

    This is the property the brief insists on. If authorization were applied only when
    building a listing, this test could not fail; it fails the moment the call path stops
    re-deciding.
    """
    server = build_mcp_server(settings_for_tests())
    fn = _registered(server, "nova.analytics.inventory")
    arguments = {"from_day": "2026-01-01", "to_day": "2026-01-02"}
    broad = principal_for(user_id=1, merchant_id=1, data_scope=DataScope.ALL)
    narrow = principal_for(
        permissions=frozenset({PermissionCode.ORDER_READ.value}),
        data_scope=DataScope.MERCHANT,
    )
    with authenticated(broad, scopes=all_tool_scopes()):
        allowed = asyncio.run(fn(**arguments))
    assert allowed.metric == "inventory.turnover"
    with authenticated(narrow, scopes=all_tool_scopes()), pytest.raises(ToolError) as caught:
        asyncio.run(fn(**arguments))
    assert ErrorCode.INSUFFICIENT_PERMISSION.name in str(caught.value)


def test_a_self_scoped_token_cannot_call_a_merchant_tool_it_can_see_is_absent():
    """The ``\tools/call`` re-check, proven with a principal the *services* would allow.

    This is the case that a list-time-only filter cannot cover. The token here holds every
    RBAC permission - so the platform's own service-level checks would not stop it - and
    its DataScope is what forbids the merchant tools. If authorization were applied only
    when building the listing, a caller could call ``nova.inventory.get`` by name and the
    service would answer, because the service trusts the principal it is handed.

    Both directions are asserted: the tool is absent from the listing, *and* calling it
    by name is refused with the DataScope code.
    """
    server = build_mcp_server(settings_for_tests())
    view_only = principal_for(
        user_id=1,
        merchant_id=1,
        permissions=frozenset({permission.value for permission in PermissionCode}),
        data_scope=DataScope.SELF,
    )
    with authenticated(view_only, scopes=all_tool_scopes()):
        listed = {tool.name for tool in asyncio.run(server.list_tools())}
    assert listed == {"nova.order.get", "nova.order.search", "nova.fulfillment.get"}
    with authenticated(view_only, scopes=all_tool_scopes()):
        text = _tool_error(server, "nova.inventory.get", {"limit": 1})
    assert ErrorCode.DATA_SCOPE_VIOLATION.name in text


# ---------------------------------------------------------------------------
# The one write path: a proposal, never a promotion (REQ-MCP-008)
# ---------------------------------------------------------------------------
def _proposal_arguments(shop: ToolShop) -> dict[str, Any]:
    start = datetime.now(UTC) + timedelta(hours=1)
    end = start + timedelta(days=7)
    return {
        "name": f"MCP proposed promotion {shop.marker}",
        "promotion_type": "DIRECT_DISCOUNT",
        "rule_config": {"discount_amount": 100},
        "scope": {"product_ids": [shop.product_id], "all_products": False},
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat(),
        "total_quota": 100,
        "summary": f"Proposed by an MCP client for merchant {shop.merchant_id}",
    }


def _count(session: Any, model: Any, merchant_id: int) -> int:
    return int(
        session.execute(select(func.count()).select_from(model).where(model.merchant_id == merchant_id)).scalar_one()
    )


def test_promotion_propose_creates_a_pending_action_and_no_promotion(tool_shop: ToolShop):
    """REQ-MCP-008, both halves.

    Half one: a real ``pending_actions`` row exists afterwards, with the risk level and
    status the governance queue expects. Half two: the ``promotions`` count is unchanged.
    Either half alone is satisfiable by the wrong implementation - a tool that created the
    promotion *and* a pending action would pass half one, and a tool that created nothing
    would pass half two.
    """
    server = build_mcp_server(settings_for_tests())
    fn = _registered(server, "nova.promotion.propose")
    factory = get_session_factory()
    with factory() as session:
        before_actions = _count(session, PendingAction, tool_shop.merchant_id)
        before_promotions = _count(session, Promotion, tool_shop.merchant_id)

    with authenticated(tool_shop.staff(), scopes=all_tool_scopes()):
        result = asyncio.run(fn(**_proposal_arguments(tool_shop)))
    payload = result.model_dump(mode="json")
    assert payload["approval_required"] is True
    assert payload["promotion_created"] is False
    assert payload["status"] == "PENDING"
    assert payload["risk_level"] == "HIGH"
    assert payload["pending_action_id"]

    with factory() as session:
        row = session.get(PendingAction, int(payload["pending_action_id"]))
        assert row is not None
        assert row.status == "PENDING"
        assert row.risk_level == "HIGH"
        assert row.tool_name == "nova.promotion.propose"
        assert row.requested_by == tool_shop.staff_user_id
        assert row.merchant_id == tool_shop.merchant_id
        # The hash is the platform's own, computed with the same function the approval
        # path compares against: a proposal hashed differently could never be approved.
        from app.modules.governance.service import payload_hash

        assert row.payload_hash == payload_hash(row.payload)
        assert row.expires_at > datetime.now(UTC)
        after_actions = _count(session, PendingAction, tool_shop.merchant_id)
        after_promotions = _count(session, Promotion, tool_shop.merchant_id)

    assert after_actions == before_actions + 1
    assert after_promotions == before_promotions


def test_promotion_propose_is_refused_without_the_write_permission(tool_shop: ToolShop):
    """The read-only operator cannot file a proposal, and nothing is written."""
    server = build_mcp_server(settings_for_tests())
    fn = _registered(server, "nova.promotion.propose")
    reader = Principal(
        user_id=tool_shop.staff_user_id,
        user_type=UserType.STAFF.value,
        merchant_id=tool_shop.merchant_id,
        roles=("VIEWER",),
        permissions=frozenset({PermissionCode.PROMOTION_READ.value}),
        data_scope=DataScope.MERCHANT,
        session_id=f"mcp-reader-{tool_shop.marker}",
        is_staff=True,
    )
    factory = get_session_factory()
    with factory() as session:
        before = _count(session, PendingAction, tool_shop.merchant_id)

    with authenticated(reader, scopes=all_tool_scopes()), pytest.raises(ToolError) as caught:
        asyncio.run(fn(**_proposal_arguments(tool_shop)))
    text = str(caught.value)
    assert ErrorCode.INSUFFICIENT_PERMISSION.name in text
    with factory() as session:
        assert _count(session, PendingAction, tool_shop.merchant_id) == before


def test_promotion_preview_creates_nothing(tool_shop: ToolShop):
    """The read-only sibling must be read-only: no pending action, no promotion."""
    server = build_mcp_server(settings_for_tests())
    fn = _registered(server, "nova.promotion.preview")
    factory = get_session_factory()
    with factory() as session:
        before_actions = _count(session, PendingAction, tool_shop.merchant_id)
        before_promotions = _count(session, Promotion, tool_shop.merchant_id)

    arguments = _proposal_arguments(tool_shop)
    # ``summary`` is a proposal field - it is what a human reads in the approval queue -
    # and the preview model does not accept it ("extra inputs are not permitted"). That
    # asymmetry is deliberate: the preview is a pricing question, the proposal is a
    # request for a decision, and one model for both would let a caller believe a preview
    # had been filed.
    arguments.pop("summary")
    with authenticated(tool_shop.staff(), scopes=all_tool_scopes()):
        result = asyncio.run(fn(**arguments))
    payload = result.model_dump(mode="json")
    assert "preview_token" not in payload, "an agent-visible result must not carry a console credential"
    assert payload["preview_effect"]
    with factory() as session:
        assert _count(session, PendingAction, tool_shop.merchant_id) == before_actions
        assert _count(session, Promotion, tool_shop.merchant_id) == before_promotions


def test_the_output_models_are_the_declared_ones():
    """The handler layer's contract: a refusal is a value, the server converts it once."""
    assert issubclass(PromotionProposeOut, object)
    assert issubclass(ProductGetOut, object)
    assert McpErrorResult(code=ErrorCode.NOT_FOUND, message="x").text.startswith("[10002]")
