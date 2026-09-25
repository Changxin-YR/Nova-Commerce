"""The frozen external MCP tool surface, and the rule that decides who may call it.

Two things live here and nothing else:

1. :data:`TOOL_SPECS` - the sixteen tool names, their OAuth scope, their RBAC
   permission, their risk level and the data scope they need. It is a module-level
   constant because it is a *contract*: an MCP client caches tool metadata, so
   adding permission requirements to an existing tool is a breaking change that
   belongs in a reviewed diff, not in a runtime lookup.
2. :func:`authorize_tool` - the intersection.

## Why the intersection is four-way and not one check

Spec section 90 requires the effective authorization of a tool to be

    token scope  鈭? RBAC permission  鈭? DataScope  鈭? tool policy

and each term exists because the other three cannot express it:

* **Scope** answers "what did the *client* ask for and the *user* consent to".
  An OAuth grant narrower than the user's role must win, or consent means nothing.
* **RBAC** answers "what may this user do at all". A token cannot grant a
  capability its subject lacks.
* **DataScope** answers "whose rows". A permission to read orders is not an
  entitlement to read *every merchant's* orders, and for a consumer the only
  readable orders are their own.
* **Tool policy** answers "what is this tool allowed to be in general" - the risk
  level, whether it is a write, and whether the platform exposes it at all.

Checking any one of these alone produces a specific, well-known failure: scope-only
lets a customer's token read another merchant's analytics; RBAC-only ignores OAuth
consent; DataScope-only ignores whether the integration is allowed the verb at all.

## Forbidden capabilities

:data:`FORBIDDEN_TOOL_NAMES` records the capabilities that must *never* exist as
MCP tools. They are listed as data so that a test can assert their absence against
the live server rather than trusting this comment. The point is not that these
operations are unwise - several are perfectly good console features - but that an
agent-reachable surface must not carry them: an LLM in a loop must not be able to
move money, rewrite stock, override state machines, republish a catalogue, or run
arbitrary SQL. Anything of that shape belongs behind the human approval of
``nova.promotion.propose`` plus the governance queue, never in ``tools/list``.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from app.modules.identity.enums import DataScope, PermissionCode
from app.modules.identity.service import Principal

# ---------------------------------------------------------------------------
# Risk vocabulary
# ---------------------------------------------------------------------------
#: Mirrors the ``pending_actions.risk_level`` CHECK constraint, so a proposal
#: written by an MCP tool can never fail the column's own constraint.
RiskLevel = str
READ_RISK: RiskLevel = "READ"
LOW_RISK: RiskLevel = "LOW"
MEDIUM_RISK: RiskLevel = "MEDIUM"
HIGH_RISK: RiskLevel = "HIGH"

#: The data-scope floor each tool needs, expressed as the minimum rank.
#: ``SELF`` tools are the ones a consumer may hold; ``MERCHANT`` tools require a
#: staff principal attached to a merchant; ``ALL`` tools require platform scope.
SCOPE_SELF = DataScope.SELF
SCOPE_MERCHANT = DataScope.MERCHANT
SCOPE_ALL = DataScope.ALL


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------
# Every model sets ``extra="forbid"``. That is an authorization control, not
# input hygiene: a tool that silently ignored ``user_id`` would still let a caller
# *believe* they had selected a principal, and the first implementation that
# honoured the field would be a privilege-escalation bug. Rejecting it makes the
# attempt visible on the wire (spec section 90: identity comes from the token).
class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductSearchInput(_ToolInput):
    keyword: str | None = Field(default=None, max_length=100, description="Substring match on product name")
    status: str | None = Field(default=None, max_length=32, description="Product status filter, e.g. PUBLISHED")
    page: int = Field(default=1, ge=1, le=1000)
    page_size: int = Field(default=20, ge=1, le=100)


class ProductGetInput(_ToolInput):
    product_id: int = Field(gt=0, description="Product id owned by the caller's merchant")


class SkuGetInput(_ToolInput):
    sku_id: int | None = Field(default=None, gt=0, description="SKU id")
    sku_code: str | None = Field(default=None, min_length=1, max_length=64, description="Merchant-facing SKU code")
    product_id: int | None = Field(default=None, gt=0, description="Product to resolve the SKU within")


class OrderGetInput(_ToolInput):
    order_no: str = Field(min_length=1, max_length=64)


class OrderSearchInput(_ToolInput):
    order_status: str | None = Field(default=None, max_length=32)
    payment_status: str | None = Field(default=None, max_length=32)
    fulfillment_status: str | None = Field(default=None, max_length=32)
    order_no: str | None = Field(default=None, max_length=64, description="Exact order number filter")
    page: int = Field(default=1, ge=1, le=1000)
    page_size: int = Field(default=20, ge=1, le=100)


class FulfillmentGetInput(_ToolInput):
    order_no: str = Field(min_length=1, max_length=64, description="Order whose packages should be returned")


class AfterSaleGetInput(_ToolInput):
    after_sale_no: str = Field(min_length=1, max_length=64)


class InventoryGetInput(_ToolInput):
    low_stock_only: bool = Field(default=False, description="Return only positions at or below safety stock")
    limit: int = Field(default=50, ge=1, le=200)
    threshold_multiplier: float = Field(default=1.0, gt=0, le=10)


class AnalyticsSalesInput(_ToolInput):
    metric: str = Field(default="sales.gmv", max_length=64, description="sales.gmv or sales.order_count")
    from_day: str = Field(description="Inclusive UTC start date, YYYY-MM-DD", max_length=10)
    to_day: str = Field(description="Inclusive UTC end date, YYYY-MM-DD", max_length=10)
    granularity: str = Field(default="day", max_length=16, description="day, week or month")


class AnalyticsInventoryInput(_ToolInput):
    from_day: str = Field(description="Inclusive UTC start date, YYYY-MM-DD", max_length=10)
    to_day: str = Field(description="Inclusive UTC end date, YYYY-MM-DD", max_length=10)
    granularity: str = Field(default="day", max_length=16)


class AnalyticsProductPerformanceInput(_ToolInput):
    from_day: str = Field(description="Inclusive UTC start date, YYYY-MM-DD", max_length=10)
    to_day: str = Field(description="Inclusive UTC end date, YYYY-MM-DD", max_length=10)
    granularity: str = Field(default="day", max_length=16)


class AnalyticsRefundsInput(_ToolInput):
    from_day: str = Field(description="Inclusive UTC start date, YYYY-MM-DD", max_length=10)
    to_day: str = Field(description="Inclusive UTC end date, YYYY-MM-DD", max_length=10)
    granularity: str = Field(default="day", max_length=16)


class AnalyticsAnomaliesInput(_ToolInput):
    from_day: str = Field(description="Inclusive UTC start date, YYYY-MM-DD", max_length=10)
    to_day: str = Field(description="Inclusive UTC end date, YYYY-MM-DD", max_length=10)
    granularity: str = Field(default="day", max_length=16)


class KnowledgeSearchInput(_ToolInput):
    query: str = Field(min_length=1, max_length=2000)
    knowledge_base_id: int = Field(gt=0)
    top_k: int = Field(default=5, ge=1, le=20)
    use_rerank: bool = Field(default=False, description="Disabled by default: MCP reads stay cheap and deterministic")


class PromotionPreviewInput(_ToolInput):
    name: str = Field(min_length=1, max_length=200)
    promotion_type: str = Field(max_length=32, description="DIRECT_DISCOUNT, PERCENT_DISCOUNT or FULL_REDUCTION")
    rule_config: dict = Field(description="Type-specific rule fields; validated by the promotion service")
    scope: dict = Field(description="PromotionScope document; validated by the promotion service")
    starts_at: str = Field(description="ISO-8601 timestamp with timezone", max_length=40)
    ends_at: str = Field(description="ISO-8601 timestamp with timezone", max_length=40)
    total_quota: int = Field(gt=0, le=10_000_000)
    description: str | None = Field(default=None, max_length=500)
    priority: int = Field(default=100, ge=0, le=10000)
    stackable: bool = False


class PromotionProposeInput(PromotionPreviewInput):
    """A proposal is a preview plus a reason for a human to read.

    ``summary`` is required because the approval queue is the only place a human
    sees this payload, and a queue of rows called "promotion proposal" with no
    explanation is a queue that gets rubber-stamped.
    """

    summary: str = Field(min_length=1, max_length=500)


# ---------------------------------------------------------------------------
# The frozen registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One externally-visible capability and the four keys that open it."""

    name: str
    title: str
    description: str
    scope: str
    permission: PermissionCode
    risk_level: RiskLevel
    input_model: type[_ToolInput]
    min_data_scope: DataScope
    read_only: bool
    idempotent: bool

    @property
    def destructive(self) -> bool:
        return not self.read_only


#: The sixteen frozen tool names. Order is presentation order in ``tools/list``.
TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="nova.product.search",
        title="Search products",
        description="Search the caller's merchant catalogue by name substring and status.",
        scope="catalog:read",
        permission=PermissionCode.PRODUCT_READ,
        risk_level=READ_RISK,
        input_model=ProductSearchInput,
        min_data_scope=SCOPE_MERCHANT,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.product.get",
        title="Get product",
        description="One product of the caller's merchant, with its SKUs.",
        scope="catalog:read",
        permission=PermissionCode.PRODUCT_READ,
        risk_level=READ_RISK,
        input_model=ProductGetInput,
        min_data_scope=SCOPE_MERCHANT,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.sku.get",
        title="Get SKU",
        description="One SKU of the caller's merchant, resolved by id or code.",
        scope="catalog:read",
        permission=PermissionCode.PRODUCT_READ,
        risk_level=READ_RISK,
        input_model=SkuGetInput,
        min_data_scope=SCOPE_MERCHANT,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.order.get",
        title="Get order",
        description="One order: the caller's own for a consumer, merchant-scoped for staff.",
        scope="orders:read",
        permission=PermissionCode.ORDER_READ,
        risk_level=READ_RISK,
        input_model=OrderGetInput,
        min_data_scope=SCOPE_SELF,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.order.search",
        title="Search orders",
        description="Paged orders visible to the caller, with the frozen status filters.",
        scope="orders:read",
        permission=PermissionCode.ORDER_READ,
        risk_level=READ_RISK,
        input_model=OrderSearchInput,
        min_data_scope=SCOPE_SELF,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.fulfillment.get",
        title="Get fulfillment",
        description="The packages of one order, subject to the same ownership rule as the order.",
        scope="fulfillment:read",
        permission=PermissionCode.FULFILLMENT_READ,
        risk_level=READ_RISK,
        input_model=FulfillmentGetInput,
        min_data_scope=SCOPE_SELF,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.after_sale.get",
        title="Get after-sale claim",
        description="One after-sale claim, merchant-scoped for staff.",
        scope="after_sale:read",
        permission=PermissionCode.AFTER_SALE_READ,
        risk_level=READ_RISK,
        input_model=AfterSaleGetInput,
        min_data_scope=SCOPE_MERCHANT,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.inventory.get",
        title="Get inventory",
        description="Stock positions of the caller's merchant, optionally only those at or below safety stock.",
        scope="inventory:read",
        permission=PermissionCode.INVENTORY_READ,
        risk_level=READ_RISK,
        input_model=InventoryGetInput,
        min_data_scope=SCOPE_MERCHANT,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.analytics.sales",
        title="Sales analytics",
        description="Paid-order revenue or order count over a UTC date window.",
        scope="analytics:read",
        permission=PermissionCode.ANALYTICS_READ,
        risk_level=READ_RISK,
        input_model=AnalyticsSalesInput,
        min_data_scope=SCOPE_MERCHANT,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.analytics.inventory",
        title="Inventory analytics",
        description="Stock turnover over a UTC date window.",
        scope="analytics:read",
        permission=PermissionCode.ANALYTICS_READ,
        risk_level=READ_RISK,
        input_model=AnalyticsInventoryInput,
        min_data_scope=SCOPE_MERCHANT,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.analytics.product_performance",
        title="Product performance analytics",
        description="Paid order-item revenue per product over a UTC date window.",
        scope="analytics:read",
        permission=PermissionCode.ANALYTICS_READ,
        risk_level=READ_RISK,
        input_model=AnalyticsProductPerformanceInput,
        min_data_scope=SCOPE_MERCHANT,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.analytics.refunds",
        title="Refund analytics",
        description="Completed-refund rate over a UTC date window.",
        scope="analytics:read",
        permission=PermissionCode.ANALYTICS_READ,
        risk_level=READ_RISK,
        input_model=AnalyticsRefundsInput,
        min_data_scope=SCOPE_MERCHANT,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.analytics.anomalies",
        title="Sales anomalies",
        description="Sales volume and refund rate together, for spotting a period that moved unusually.",
        scope="analytics:read",
        permission=PermissionCode.ANALYTICS_READ,
        risk_level=READ_RISK,
        input_model=AnalyticsAnomaliesInput,
        min_data_scope=SCOPE_MERCHANT,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.knowledge.search",
        title="Search knowledge base",
        description="Retrieve evidence chunks from one knowledge base of the caller's merchant.",
        scope="knowledge:read",
        permission=PermissionCode.KNOWLEDGE_READ,
        risk_level=READ_RISK,
        input_model=KnowledgeSearchInput,
        min_data_scope=SCOPE_MERCHANT,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.promotion.preview",
        title="Preview a promotion",
        description=(
            "Price a draft promotion against the live catalogue: conflicts and estimated impact. "
            "Creates nothing."
        ),
        scope="promotion:read",
        permission=PermissionCode.PROMOTION_READ,
        risk_level=READ_RISK,
        input_model=PromotionPreviewInput,
        min_data_scope=SCOPE_MERCHANT,
        read_only=True,
        idempotent=True,
    ),
    ToolSpec(
        name="nova.promotion.propose",
        title="Propose a promotion for approval",
        description=(
            "Create a PENDING approval request for a promotion. This tool NEVER creates or publishes a "
            "promotion: a human with promotion:write approves it in the console (REQ-MCP-008)."
        ),
        scope="promotion:propose",
        permission=PermissionCode.PROMOTION_WRITE,
        risk_level=HIGH_RISK,
        input_model=PromotionProposeInput,
        min_data_scope=SCOPE_MERCHANT,
        read_only=False,
        idempotent=False,
    ),
)

TOOL_SPECS_BY_NAME: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}

#: Capability names that must never appear in ``tools/list``. Asserted absent by
#: ``tests/mcp/test_mcp_tools.py`` against the live server, so this list is a test
#: input rather than a comment.
FORBIDDEN_TOOL_NAMES: tuple[str, ...] = (
    "nova.refund.execute",
    "refund.execute",
    "nova.inventory.adjust",
    "inventory.adjust",
    "nova.order.force_update",
    "order.force_update",
    "nova.promotion.create",
    "promotion.create",
    "nova.rbac.update",
    "rbac.update",
    "nova.data_scope.update",
    "data_scope.update",
    "execute_sql",
    "raw_sql",
    "shell",
    "http_request",
)


@dataclass(frozen=True, slots=True)
class Denial:
    """Why a tool is not authorised, in a form a test can assert on."""

    tool_name: str
    reason: str
    code: str


def required_scopes_for(spec: ToolSpec, *, write_scope: str) -> frozenset[str]:
    """The OAuth scopes a tool needs: its own, plus the write scope when it writes."""
    scopes = {spec.scope}
    if not spec.read_only:
        scopes.add(write_scope)
    return frozenset(scopes)


def scope_denial(spec: ToolSpec, token_scopes: frozenset[str], *, write_scope: str) -> Denial | None:
    """The missing-scope denial for ``spec``, or ``None`` when the scope is held."""
    missing = sorted(required_scopes_for(spec, write_scope=write_scope) - token_scopes)
    if missing:
        return Denial(
            tool_name=spec.name,
            reason=f"token is missing required scope(s): {', '.join(missing)}",
            code="MCP_TOKEN_INSUFFICIENT_SCOPE",
        )
    return None


def data_scope_denial(spec: ToolSpec, principal: Principal) -> Denial | None:
    """Whether the principal's DataScope reaches the rows ``spec`` reads.

    The comparison is by *rank* (``NONE < SELF < MERCHANT < ALL``), so a broader
    scope always satisfies a narrower requirement and a principal with no scope
    satisfies nothing. Rank comparison rather than equality matters for the
    console case: an ``ALL`` operator must be able to use the merchant-scoped
    tools, and a test that required equality would have quietly forced a
    duplicated tool for every scope.
    """
    if principal.data_scope.covers(spec.min_data_scope):
        return None
    return Denial(
        tool_name=spec.name,
        reason=(
            f"data scope {principal.data_scope.value} does not cover the required "
            f"{spec.min_data_scope.value} for this tool"
        ),
        code="DATA_SCOPE_VIOLATION",
    )


def rbac_denial(spec: ToolSpec, principal: Principal) -> Denial | None:
    """Whether the principal holds ``spec``'s RBAC permission."""
    if principal.has_permission(spec.permission.value):
        return None
    return Denial(
        tool_name=spec.name,
        reason=f"principal lacks the required permission {spec.permission.value}",
        code="INSUFFICIENT_PERMISSION",
    )


def authorize_tool(
    *,
    tool_name: str,
    principal: Principal,
    token_scopes: frozenset[str],
    write_scope: str,
) -> Denial | None:
    """The full four-way intersection. ``None`` means authorised.

    Returns the *first* denial in a fixed order (existence, policy, scope, RBAC,
    DataScope) rather than all of them, so the caller gets one stable code and the
    wire response cannot be used to enumerate which of four independent conditions
    a probing token happens to satisfy. The order is also the order of increasing
    information disclosure: an unknown name reveals the surface, a missing scope
    reveals only the client's own grant, and a DataScope failure reveals that the
    tool exists and the permission is held.
    """
    spec = TOOL_SPECS_BY_NAME.get(tool_name)
    if spec is None:
        return Denial(
            tool_name=tool_name,
            reason="tool is not exposed by this server",
            code="MCP_TOOL_NOT_EXPOSED",
        )
    denial = scope_denial(spec, token_scopes, write_scope=write_scope)
    if denial is not None:
        return denial
    denial = rbac_denial(spec, principal)
    if denial is not None:
        return denial
    return data_scope_denial(spec, principal)


def authorized_tool_names(
    *,
    principal: Principal,
    token_scopes: frozenset[str],
    write_scope: str,
) -> tuple[str, ...]:
    """The subset of the frozen surface this principal may see *and* call.

    ``tools/list`` and ``tools/call`` both filter through this function, which is
    the point: two implementations of "may they call it" is how a server ends up
    listing a tool it will then refuse, or worse, calling one it never listed.
    """
    return tuple(
        spec.name
        for spec in TOOL_SPECS
        if authorize_tool(
            tool_name=spec.name,
            principal=principal,
            token_scopes=token_scopes,
            write_scope=write_scope,
        )
        is None
    )


__all__ = [
    "FORBIDDEN_TOOL_NAMES",
    "HIGH_RISK",
    "LOW_RISK",
    "MEDIUM_RISK",
    "READ_RISK",
    "SCOPE_ALL",
    "SCOPE_MERCHANT",
    "SCOPE_SELF",
    "TOOL_SPECS",
    "TOOL_SPECS_BY_NAME",
    "AfterSaleGetInput",
    "AnalyticsAnomaliesInput",
    "AnalyticsInventoryInput",
    "AnalyticsProductPerformanceInput",
    "AnalyticsRefundsInput",
    "AnalyticsSalesInput",
    "Denial",
    "FulfillmentGetInput",
    "InventoryGetInput",
    "KnowledgeSearchInput",
    "OrderGetInput",
    "OrderSearchInput",
    "ProductGetInput",
    "ProductSearchInput",
    "PromotionPreviewInput",
    "PromotionProposeInput",
    "RiskLevel",
    "SkuGetInput",
    "ToolSpec",
    "authorize_tool",
    "authorized_tool_names",
    "data_scope_denial",
    "rbac_denial",
    "required_scopes_for",
    "scope_denial",
]
