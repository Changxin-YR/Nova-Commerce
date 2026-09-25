"""The sixteen MCP tool implementations.

Every function here is a thin adapter: it extracts the caller's identity from the
*verified token*, opens one database session, calls an existing service, and
shapes the result for the wire. No tool contains an authorization rule of its own
(that lives in :mod:`app.mcp.policy` and in the services) and no tool issues SQL.

## Why the identity comes from the context and never from the arguments

``current_principal()`` reads the principal the verifier put on the token. The
input models in :mod:`app.mcp.policy` have no ``user_id``/``merchant_id``/
``permissions`` field and set ``extra="forbid"``, so an attempt to select a
principal through the wire is a validation error rather than a quietly ignored
key. That combination is what makes "MCP tool parameters cannot escalate
privileges" a property of the types rather than a review comment.

## Why the service calls run in a worker thread

The services are synchronous SQLAlchemy code, and the MCP request path is async.
Calling them inline would block the event loop for the duration of every query,
which under an agent issuing tool calls concurrently turns a fast server into a
serialized one. ``anyio.to_thread.run_sync`` keeps the loop free; the session is
created *inside* the thread so no SQLAlchemy object ever crosses a thread
boundary, which SQLAlchemy does not support.

## Why ``_session()`` is a context manager rather than a commit/rollback at the end

A tool that raised halfway through would otherwise leave a session holding an open
transaction on a pooled connection. The context manager rolls back on any exit
that is not a successful commit, so a failed tool cannot pin a connection or leak
a half-written transaction into the next call.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, NoReturn

import anyio
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.errors import (
    AppError,
    DataScopeViolationError,
    ErrorCode,
    PermissionDeniedError,
)
from app.core.redaction import RedactionPurpose, redact
from app.mcp.auth import PRINCIPAL_CLAIM
from app.mcp.policy import (
    TOOL_SPECS_BY_NAME,
    AfterSaleGetInput,
    AnalyticsAnomaliesInput,
    AnalyticsInventoryInput,
    AnalyticsProductPerformanceInput,
    AnalyticsRefundsInput,
    AnalyticsSalesInput,
    FulfillmentGetInput,
    InventoryGetInput,
    KnowledgeSearchInput,
    OrderGetInput,
    OrderSearchInput,
    ProductGetInput,
    ProductSearchInput,
    PromotionPreviewInput,
    PromotionProposeInput,
    SkuGetInput,
)
from app.modules.aftersales.service import AfterSaleService
from app.modules.analytics.service import AnalyticsService
from app.modules.catalog.models import Product, ProductSku
from app.modules.catalog.service import CatalogService
from app.modules.fulfillment.service import FulfillmentService
from app.modules.governance.models import PendingAction
from app.modules.governance.service import payload_hash
from app.modules.identity.enums import DataScope
from app.modules.identity.service import Principal
from app.modules.inventory.service import InventoryService
from app.modules.knowledge.schemas import RetrievalDebugRequest
from app.modules.knowledge.service import KnowledgeService
from app.modules.marketing.schemas import PromotionDraft
from app.modules.marketing.service import PromotionService
from app.modules.order.service import OrderService
from app.shared.db.base import utc_now
from app.shared.db.session import get_session_factory

#: Registered by ``server.build_mcp_server`` so the tools can produce proposals
#: with the same TTL the server advertises. Set once at construction rather than
#: read from the global settings object, so a test that builds two servers with
#: two settings objects does not get whichever one imported first.
_PROPOSAL_TTL_SECONDS: int = 900

#: How far back ``nova.analytics.anomalies`` compares. Fixed rather than a
#: parameter: an anomaly is only meaningful against a *stated* baseline, and a
#: caller-supplied one would let a client pick the comparison that flatters it.
_ANOMALY_BASELINE_DAYS = 7


def configure_tool_runtime(*, proposal_ttl_seconds: int) -> None:
    """Bind the tool module's runtime constants to one server's settings."""
    global _PROPOSAL_TTL_SECONDS  # one server per worker process
    _PROPOSAL_TTL_SECONDS = proposal_ttl_seconds


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class McpErrorResult:
    """A refusal, as a *value* rather than an exception.

    ## Why the tool layer returns errors instead of raising them

    The obvious design is to raise a domain error and let the server turn it into
    ``CallToolResult(is_error=True)``. Two things argue against it here.

    First, a raise can only carry one shape of information, and this layer has two
    kinds of refusal: an argument-level refusal that is part of the tool's ordinary
    contract (``provide sku_id or sku_code``), and a request-level fault the caller
    cannot fix (no verified token). Returning the first keeps "the tool answered"
    and "the request could not be processed" visibly different in the code, rather
    than distinguished only by which exception type was raised.

    Second, it keeps the *code* structural. Every refusal carries a member of
    :class:`~app.core.errors.ErrorCode`, so the MCP surface cannot grow a second
    error vocabulary beside the platform's - a test can assert the code is a real
    enum member without parsing a message.

    The single exception that remains, :class:`McpToolError`, exists for exactly one
    situation: the verified principal is absent or unusable, which means
    authorization could not be evaluated at all. That is not a tool answer; it is a
    failed request, and it must not be confusable with a tool that chose to refuse.
    """

    code: ErrorCode
    message: str
    tool_name: str = ""

    @property
    def numeric_code(self) -> int:
        return int(self.code)

    @property
    def text(self) -> str:
        """The stable, client-visible rendering: numeric code, name, then message.

        The number first because a client branches on it; the name because a human
        reading a log needs to know what ``20009`` means without a lookup table.
        """
        return f"[{int(self.code)}] {self.code.name}: {self.message}"


class McpToolError(ToolError):
    """A refused call, as an *anticipated* tool failure in the SDK's own vocabulary.

    ## Why not a plain ``Exception``, and why not ``MCPError``

    The SDK sorts every failure a tool body can produce into three buckets, and the
    bucket decides what the caller receives:

    * ``ToolError`` - "a failure you saw coming". The call answers with
      ``is_error=True`` and the exception's text in ``content``, logged at INFO.
      This is what an authorization refusal is.
    * ``MCPError`` - a *protocol* error. It is re-raised through the JSON-RPC layer, so
      the call does not answer at all; it becomes a transport-level error object. An
      authorization decision reported that way loses the tool-shaped error result a
      client is written against.
    * anything else - treated as a *crash*: the client is told only
      "Error executing tool <name>" and the detail stays in the server log. A refusal
      raised as a plain ``Exception`` therefore reaches the client as an
      information-free crash, which is the opposite of what an agent needs in order to
      decide whether retrying is worth anything.

    ``ToolError`` is the middle of the three and the correct one. Its text is
    ``[<numeric code>] <CODE_NAME>: <message>``, so the stable
    :class:`~app.core.errors.ErrorCode` value survives to the client even though the SDK
    wraps it once more with the tool's name.
    """

    def __init__(self, business_code: ErrorCode, message: str) -> None:
        self.business_code = business_code
        super().__init__(McpErrorResult(code=business_code, message=message).text)

    @property
    def error_code(self) -> ErrorCode:
        return self.business_code


def error_result(code: ErrorCode, message: str, *, tool_name: str = "") -> McpErrorResult:
    """Construct an error *value*, for the two call sites that inspect rather than raise."""
    return McpErrorResult(code=code, message=message, tool_name=tool_name)


def _fail(code: ErrorCode, message: str) -> NoReturn:
    """Refuse immediately. Typed ``NoReturn`` so it reads as a return at the call site."""
    raise McpToolError(code, message)


#: Service errors carry their own stable code, so no mapping table is needed:
#: ``AppError.code`` is already a member of :class:`~app.core.errors.ErrorCode`
#: (the construction-time guard in ``_error()`` enforces it). A second mapping
#: here would be a second place for the two to disagree.


def error_code_for(exc: AppError) -> ErrorCode:
    """A service exception's own stable code, for the server's exception boundary.

    Exported rather than duplicated: the mapping is "the platform already decided", and
    a second implementation of that decision is how a business code silently becomes
    10000 on one transport and 50003 on another.
    """
    return exc.code


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
def current_verified_token() -> Any:
    """The verified ``AccessToken`` for this request, or raise :class:`McpToolError`."""
    token = get_access_token()
    if token is None:
        raise McpToolError(ErrorCode.MCP_TOKEN_INVALID, "no verified bearer token is present on this request")
    return token


def current_principal() -> Principal:
    """The server-resolved principal for this request."""
    token = current_verified_token()
    claims = token.claims or {}
    principal = claims.get(PRINCIPAL_CLAIM)
    if not isinstance(principal, Principal):
        raise McpToolError(ErrorCode.MCP_TOKEN_INVALID, "the verified token carries no resolved principal")
    return principal


def current_scopes() -> frozenset[str]:
    """The token's granted scopes."""
    return frozenset(current_verified_token().scopes)


# ---------------------------------------------------------------------------
# Session plumbing
# ---------------------------------------------------------------------------
@contextmanager
def _session(*, commit: bool = False) -> Iterator[Session]:
    """One session per tool call, with the transaction decided by the caller."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        if commit:
            session.commit()
        else:
            session.rollback()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def _in_thread(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a synchronous service call off the event loop."""
    return await anyio.to_thread.run_sync(lambda: function(*args, **kwargs))


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------
# Field names and money units are the platform's: amounts are BIGINT minor units
# (spec section 19), so ``price_amount=1999`` means 19.99. Renaming them per tool
# would create a second vocabulary for the same fact.
class _ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductSummaryOut(_ToolOutput):
    id: int
    product_no: str
    name: str
    status: str
    min_price_amount: int
    max_price_amount: int
    sales_count: int
    category_name: str | None = None


class SkuOut(_ToolOutput):
    id: int
    sku_no: str
    sku_code: str
    name: str
    product_id: int
    price_amount: int
    market_price_amount: int
    status: str
    attribute_snapshot: dict = Field(default_factory=dict)


class ProductGetOut(ProductSummaryOut):
    subtitle: str | None = None
    description: str | None = None
    skus: list[SkuOut] = Field(default_factory=list)


class ProductSearchOut(_ToolOutput):
    total: int
    items: list[ProductSummaryOut] = Field(default_factory=list)


class SkuGetOut(_ToolOutput):
    sku: SkuOut


class OrderSummaryOut(_ToolOutput):
    id: int
    order_no: str
    order_status: str
    payment_status: str
    fulfillment_status: str
    payable_amount: int
    paid_amount: int
    refunded_amount: int
    item_count: int
    created_at: datetime | None = None


class OrderGetOut(OrderSummaryOut):
    expires_at: datetime | None = None
    paid_at: datetime | None = None


class OrderSearchOut(_ToolOutput):
    total: int
    items: list[OrderSummaryOut] = Field(default_factory=list)


class FulfillmentOut(_ToolOutput):
    id: int
    fulfillment_no: str
    order_id: int
    #: The *package* status, named after the column: a fulfillment row is one
    #: package, and calling it ``status`` here would invite confusing it with the
    #: order's own ``fulfillment_status`` axis (which this tool does not report).
    fulfillment_status: str
    carrier: str | None = None
    tracking_no: str | None = None
    shipped_at: datetime | None = None


class FulfillmentGetOut(_ToolOutput):
    order_no: str
    packages: list[FulfillmentOut] = Field(default_factory=list)


class AfterSaleGetOut(_ToolOutput):
    id: int
    after_sale_no: str
    order_no: str
    claim_status: str
    type: str
    requested_amount: int
    approved_amount: int
    refunded_amount: int
    reason: str
    created_at: datetime | None = None


class InventoryPositionOut(_ToolOutput):
    sku_id: int
    product_id: int
    sku_code: str
    sku_name: str
    warehouse_id: int
    available_qty: int
    locked_qty: int
    safety_stock: int
    below_safety_stock: bool


class InventoryGetOut(_ToolOutput):
    total_positions: int
    returned: int
    low_stock_only: bool
    positions: list[InventoryPositionOut] = Field(default_factory=list)


class AnalyticsWindowOut(_ToolOutput):
    metric: str
    unit: str | None
    from_day: str
    to_day: str
    granularity: str
    total: float | None = None
    previous_total: float | None = None
    series: dict = Field(default_factory=dict)
    dimensions: list[dict] = Field(default_factory=list)


class AnomalyOut(_ToolOutput):
    from_day: str
    to_day: str
    granularity: str
    baseline_days: int
    order_count: float | None = None
    previous_order_count: float | None = None
    refund_rate: float | None = None
    order_count_change_ratio: float | None = None
    anomaly_suspected: bool
    basis: str


class KnowledgeEvidenceOut(_ToolOutput):
    index: int
    doc_id: str
    doc_name: str
    chunk_id: str
    score: float
    snippet: str


class KnowledgeSearchOut(_ToolOutput):
    query: str
    knowledge_base_id: int
    total_evidence: int
    evidence: list[KnowledgeEvidenceOut] = Field(default_factory=list)


class PromotionPreviewOut(_ToolOutput):
    #: Deliberately NOT the service's ``preview_token``. That token is a
    #: single-use credential for the console's create endpoint, and putting a
    #: credential in an agent-visible tool result is how it ends up in a
    #: transcript, a log and a model's context.
    preview_effect: str = "no promotion was created or modified; this is an estimate only"
    conflicts: list[dict] = Field(default_factory=list)
    estimated_impact: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class PromotionProposeOut(_ToolOutput):
    pending_action_id: int | None
    status: str
    risk_level: str
    action_type: str
    approval_required: bool
    promotion_created: bool
    expires_at: datetime
    message: str


# ---------------------------------------------------------------------------
# Serialisers
# ---------------------------------------------------------------------------
def _product_summary(product: Product) -> ProductSummaryOut:
    return ProductSummaryOut(
        id=product.id,
        product_no=product.product_no,
        name=product.name,
        status=product.status,
        min_price_amount=product.min_price,
        max_price_amount=product.max_price,
        sales_count=product.sales_count,
        category_name=product.category.name if product.category is not None else None,
    )


def _sku_out(sku: ProductSku) -> SkuOut:
    """A SKU as the wire sees it.

    ``cost_amount`` is **not** in :class:`SkuOut` and is not read here: landed cost
    is merchant-private (see the model's own note), and a tool that never touches
    the field cannot leak it through a later refactor.
    """
    return SkuOut(
        id=sku.id,
        sku_no=sku.sku_no,
        sku_code=sku.sku_code,
        name=sku.name,
        product_id=sku.product_id,
        price_amount=sku.price_amount,
        market_price_amount=sku.market_price_amount,
        status=sku.status,
        attribute_snapshot=dict(sku.attribute_snapshot or {}),
    )


#: Redaction purpose used for every tool read that can carry free text. MCP is one
#: of the purposes ``app.core.redaction`` treats as "no PII at all, even masked",
#: because a tool result is copied into prompts, transcripts and third-party logs.
_MCP_PURPOSE = RedactionPurpose.MCP


def _order_summary(order: Any) -> OrderSummaryOut:
    """An order as the wire sees it.

    The receiver name, phone, address snapshot and free-text remark on the order row
    are **not read here at all**. They are the customer's personal data, and an MCP
    client is an external system by definition (spec sections 94, 109): the tool
    exists to answer "what is the state of this order", which is exactly what the
    fields below carry. Products, prices, statuses and counts are not PII.
    """
    return OrderSummaryOut(
        id=order.id,
        order_no=order.order_no,
        order_status=order.order_status,
        payment_status=order.payment_status,
        fulfillment_status=order.fulfillment_status,
        payable_amount=order.payable_amount,
        paid_amount=order.paid_amount,
        refunded_amount=order.refunded_amount,
        item_count=order.item_count,
        created_at=order.created_at,
    )


def _change_ratio(value: int | float | None, reference: int | float | None) -> float | None:
    """Percentage change, or ``None`` when there is no baseline to compare against.

    Returning ``None`` rather than ``0.0`` matters: "no orders last week" and
    "exactly the same number of orders" are different situations, and reporting the
    first as zero change is how an anomaly detector reports calm during a blackout.
    """
    if not reference:
        return None
    current = float(value or 0)
    baseline = float(reference)
    return float(Decimal(str(current - baseline)) / Decimal(str(baseline)))


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
async def product_search(principal: Principal, arguments: ProductSearchInput) -> ProductSearchOut:
    def run() -> ProductSearchOut:
        with _session() as session:
            rows, total = CatalogService(session).list_admin(
                principal=principal,
                page=arguments.page,
                page_size=arguments.page_size,
                keyword=arguments.keyword,
                status=arguments.status,
            )
            return ProductSearchOut(total=total, items=[_product_summary(row) for row in rows])

    return await _in_thread(run)


async def product_get(principal: Principal, arguments: ProductGetInput) -> ProductGetOut:
    def run() -> ProductGetOut:
        with _session() as session:
            product = CatalogService(session).detail(principal=principal, product_id=arguments.product_id)
            return ProductGetOut(
                **_product_summary(product).model_dump(),
                subtitle=product.subtitle,
                description=product.description,
                skus=[_sku_out(sku) for sku in sorted(product.skus, key=lambda row: (row.sort_order, row.id))],
            )

    return await _in_thread(run)


async def sku_get(principal: Principal, arguments: SkuGetInput) -> Any:
    """One SKU, resolved by id or by (product, code).

    Both resolution paths go through the merchant-scoped *product* read, so the
    authorization applied to a SKU is the authorization applied to its product. A
    direct SKU lookup would have to trust either the SKU's own ``merchant_id``
    column or an id the caller supplied, and a tool is not the place to add a second
    ownership rule.
    """
    if arguments.sku_id is None and arguments.sku_code is None:
        return _fail(ErrorCode.VALIDATION_ERROR, "provide sku_id or sku_code")
    if arguments.sku_id is not None and arguments.product_id is not None:
        # Mixing the two selectors would make "which product did you mean" ambiguous
        # precisely when the caller is trying to widen the search.
        return _fail(
            ErrorCode.VALIDATION_ERROR,
            "provide either sku_id or (product_id + sku_code), not both",
        )

    def run() -> SkuGetOut | McpErrorResult:
        with _session() as session:
            catalog = CatalogService(session)
            if arguments.product_id is not None and arguments.sku_code is not None:
                product = catalog.detail(principal=principal, product_id=arguments.product_id)
                for sku in product.skus:
                    if sku.sku_code == arguments.sku_code:
                        return SkuGetOut(sku=_sku_out(sku))
                return _fail(ErrorCode.SKU_NOT_FOUND, "no SKU with that code belongs to the product")
            if arguments.sku_id is not None:
                product_id = _product_id_for_sku(session, sku_id=arguments.sku_id)
                if product_id is None:
                    return _fail(ErrorCode.SKU_NOT_FOUND, "sku not found")
                product = catalog.detail(principal=principal, product_id=product_id)
                for sku in product.skus:
                    if sku.id == arguments.sku_id:
                        return SkuGetOut(sku=_sku_out(sku))
                # The product loaded without the SKU: it was deleted or the id is
                # not this merchant's. Same answer as "no such SKU" on purpose - the
                # row's existence is not the caller's business.
                return _fail(ErrorCode.SKU_NOT_FOUND, "sku not found")
            return _fail(ErrorCode.VALIDATION_ERROR, "provide sku_id or sku_code")

    return await _in_thread(run)


def _product_id_for_sku(session: Session, *, sku_id: int) -> int | None:
    """The product a SKU belongs to, so the merchant-scoped product read can scope it.

    A single-column lookup by primary key, not a business read: the catalogue
    *service* owns the authorization and returns the SKU. Returning ``None`` rather
    than raising keeps the caller free to answer with the tool's own error code
    instead of an exception shape.
    """
    sku = session.get(ProductSku, sku_id)
    return None if sku is None else sku.product_id


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
async def order_get(principal: Principal, arguments: OrderGetInput) -> OrderGetOut:
    def run() -> OrderGetOut:
        with _session() as session:
            service = OrderService(session)
            admin = principal.is_staff and principal.data_scope is not DataScope.SELF
            order = (
                service.get_admin_order(principal=principal, order_no=arguments.order_no)
                if admin
                else service.get_customer_order(principal=principal, order_no=arguments.order_no)
            )
            summary = _order_summary(order)
            return OrderGetOut(
                **summary.model_dump(),
                expires_at=order.expires_at,
                paid_at=order.paid_at,
            )

    return await _in_thread(run)


async def order_search(principal: Principal, arguments: OrderSearchInput) -> Any:
    if not (principal.is_staff and principal.data_scope is not DataScope.SELF) and (
        arguments.payment_status or arguments.fulfillment_status or arguments.order_no
    ):
        # Checked before the session opens, because it is a statement about the
        # tool and the principal - not about the data. The consumer order list has no
        # such filters, and silently ignoring them would answer "no results" for a
        # filter that was never applied, which an agent reads as "this customer has
        # no refunded orders" rather than "that question is not supported here".
        return _fail(
            ErrorCode.VALIDATION_ERROR,
            "the consumer order list supports only order_status and paging",
        )

    def run() -> OrderSearchOut:
        with _session() as session:
            service = OrderService(session)
            admin = principal.is_staff and principal.data_scope is not DataScope.SELF
            if admin:
                page = service.list_admin_orders(
                    principal=principal,
                    order_status=arguments.order_status,
                    payment_status=arguments.payment_status,
                    fulfillment_status=arguments.fulfillment_status,
                    order_no=arguments.order_no,
                    page=arguments.page,
                    page_size=arguments.page_size,
                )
            else:
                page = service.list_customer_orders(
                    principal=principal,
                    order_status=arguments.order_status,
                    page=arguments.page,
                    page_size=arguments.page_size,
                )
            return OrderSearchOut(total=page.total, items=[_order_summary(row) for row in page.rows])

    return await _in_thread(run)


# ---------------------------------------------------------------------------
# Fulfillment
# ---------------------------------------------------------------------------
async def fulfillment_get(principal: Principal, arguments: FulfillmentGetInput) -> FulfillmentGetOut:
    def run() -> FulfillmentGetOut:
        with _session() as session:
            rows = FulfillmentService(session).get_for_order_no(
                principal=principal, order_no=arguments.order_no
            )
            return FulfillmentGetOut(
                order_no=arguments.order_no,
                packages=[
                    FulfillmentOut(
                        id=row.id,
                        fulfillment_no=row.fulfillment_no,
                        order_id=row.order_id,
                        fulfillment_status=row.fulfillment_status,
                        carrier=row.carrier,
                        tracking_no=row.tracking_no,
                        shipped_at=row.shipped_at,
                    )
                    for row in rows
                ],
            )

    return await _in_thread(run)


# ---------------------------------------------------------------------------
# After-sales
# ---------------------------------------------------------------------------
async def after_sale_get(principal: Principal, arguments: AfterSaleGetInput) -> AfterSaleGetOut:
    def run() -> AfterSaleGetOut:
        with _session() as session:
            service = AfterSaleService(session)
            admin = principal.is_staff and principal.data_scope is not DataScope.SELF
            claim = (
                service.get_admin_claim(principal=principal, after_sale_no=arguments.after_sale_no)
                if admin
                else service.get_customer_claim(principal=principal, after_sale_no=arguments.after_sale_no)
            )
            return AfterSaleGetOut(
                id=claim.id,
                after_sale_no=claim.after_sale_no,
                order_no=claim.order_no,
                claim_status=claim.claim_status,
                type=claim.type,
                requested_amount=claim.requested_amount,
                approved_amount=claim.approved_amount,
                refunded_amount=claim.refunded_amount,
                # The claim reason is customer-written free text and the one field
                # here that routinely carries personal detail ("the courier rang
                # my number at..."), so it is scrubbed before leaving the platform.
                reason=str(redact(claim.reason, purpose=_MCP_PURPOSE)),
                created_at=claim.created_at,
            )

    return await _in_thread(run)


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------
async def inventory_get(principal: Principal, arguments: InventoryGetInput) -> InventoryGetOut:
    def run() -> InventoryGetOut:
        with _session() as session:
            service = InventoryService(session)
            if arguments.low_stock_only:
                positions, total = service.list_positions(
                    merchant_id=principal.merchant_id,
                    low_stock_only=True,
                    limit=arguments.limit,
                )
            else:
                positions, total = service.list_positions(merchant_id=principal.merchant_id, limit=arguments.limit)
            return InventoryGetOut(
                total_positions=total,
                returned=len(positions),
                low_stock_only=arguments.low_stock_only,
                positions=[_position_out(row, session=session) for row in positions],
            )

    return await _in_thread(run)


def _position_out(row: Any, *, session: Session) -> InventoryPositionOut:
    sku = session.get(ProductSku, row.sku_id)
    return InventoryPositionOut(
        sku_id=row.sku_id,
        product_id=sku.product_id if sku is not None else 0,
        sku_code=sku.sku_code if sku is not None else "",
        sku_name=sku.name if sku is not None else "",
        warehouse_id=row.warehouse_id,
        available_qty=row.available_qty,
        locked_qty=row.locked_qty,
        safety_stock=row.safety_stock,
        below_safety_stock=row.available_qty <= row.safety_stock,
    )


# ---------------------------------------------------------------------------
# Analytics
# ---------------------------------------------------------------------------
def _invalid_window(tool_name: str, detail: str) -> McpErrorResult:
    return error_result(ErrorCode.VALIDATION_ERROR, detail, tool_name=tool_name)


def _window(tool_name: str, arguments: Any) -> tuple[date, date] | McpErrorResult:
    """Parse the two date arguments, refusing anything the service would reject.

    Parsed here rather than in the model so the failure names the field: a pydantic
    date error on the MCP surface arrives as a generic tool error and tells the
    caller nothing it can act on. ``date.fromisoformat`` rather than ``strptime``
    because it is exact - ``strptime`` accepts ``2026-1-1``, and an analytics window
    whose bounds were silently reinterpreted is worse than a refusal.
    """
    try:
        from_day = date.fromisoformat(arguments.from_day)
        to_day = date.fromisoformat(arguments.to_day)
    except ValueError:
        return _invalid_window(tool_name, "from_day and to_day must be YYYY-MM-DD")
    if from_day > to_day:
        return _invalid_window(tool_name, "from_day must not be after to_day")
    return from_day, to_day


def _granularity(tool_name: str, value: str) -> str | McpErrorResult:
    if value not in {"day", "week", "month"}:
        return _invalid_window(tool_name, "granularity must be day, week or month")
    return value


def _analytics_call(service: AnalyticsService, **kwargs: Any) -> dict:
    """Run one metric, translating the service's refusals into coded results.

    The analytics service checks ``data_scope`` itself and reports a plain
    ``PermissionDeniedError`` for a principal whose scope does not reach the rows.
    That distinction matters to a caller deciding what to do next: a token with the
    right scope but a misconfigured merchant is a configuration problem, while a
    consumer token asking for merchant analytics is simply not allowed to. The codes
    are the platform's own (20009 / 20010) rather than new ones.
    """
    try:
        return service.metric(**kwargs)
    except PermissionDeniedError as exc:
        raise McpToolError(ErrorCode.INSUFFICIENT_PERMISSION, exc.public_message) from exc
    except DataScopeViolationError as exc:
        raise McpToolError(ErrorCode.DATA_SCOPE_VIOLATION, exc.public_message) from exc


async def analytics_sales(principal: Principal, arguments: AnalyticsSalesInput) -> Any:
    if arguments.metric not in {"sales.gmv", "sales.order_count"}:
        return _fail(ErrorCode.VALIDATION_ERROR, "metric must be sales.gmv or sales.order_count")
    return await _metric(principal, arguments, arguments.metric)


async def analytics_inventory(principal: Principal, arguments: AnalyticsInventoryInput) -> Any:
    return await _metric(principal, arguments, "inventory.turnover")


async def analytics_product_performance(principal: Principal, arguments: AnalyticsProductPerformanceInput) -> Any:
    return await _metric(principal, arguments, "product.performance")


async def analytics_refunds(principal: Principal, arguments: AnalyticsRefundsInput) -> Any:
    return await _metric(principal, arguments, "refund.rate")


async def _metric(principal: Principal, arguments: Any, metric: str) -> Any:
    tool_name = _TOOL_FOR_METRIC[metric]
    window = _window(tool_name, arguments)
    if isinstance(window, McpErrorResult):
        return window
    granularity = _granularity(tool_name, arguments.granularity)
    if isinstance(granularity, McpErrorResult):
        return granularity
    from_day, to_day = window

    def run() -> AnalyticsWindowOut:
        with _session() as session:
            result = _analytics_call(
                AnalyticsService(session),
                principal=principal,
                metric=metric,
                from_day=from_day,
                to_day=to_day,
                granularity=granularity,
            )
            # The service answers with a nested document (``period``/``summary``/
            # ``series``); it is flattened here because an MCP tool result is read by
            # a model, and a nested envelope of single-key objects costs context
            # without adding information.
            period = dict(result.get("period") or {})
            summary = dict(result.get("summary") or {})
            return AnalyticsWindowOut(
                metric=str(result.get("metric", metric)),
                unit=result.get("unit"),
                from_day=str(period.get("from", from_day.isoformat())),
                to_day=str(period.get("to", to_day.isoformat())),
                granularity=str(period.get("granularity", granularity)),
                total=summary.get("total"),
                previous_total=None,
                series={
                    str(entry.get("bucket")): entry.get("value") for entry in (result.get("series") or [])
                },
                dimensions=list(result.get("dimensions") or []),
            )

    return await _in_thread(run)


#: Which tool owns which metric. Used only to name the tool in an error message, so
#: a caller that got a bad date back learns *which* call was malformed.
_TOOL_FOR_METRIC: dict[str, str] = {
    "sales.gmv": "nova.analytics.sales",
    "sales.order_count": "nova.analytics.sales",
    "inventory.turnover": "nova.analytics.inventory",
    "product.performance": "nova.analytics.product_performance",
    "refund.rate": "nova.analytics.refunds",
}


async def analytics_anomalies(principal: Principal, arguments: AnalyticsAnomaliesInput) -> Any:
    """Sales volume and refund rate together, against a fixed trailing baseline.

    Two service calls rather than one invented metric: the platform's
    ``AnalyticsService`` already computes each projection, and deriving an "anomaly
    score" locally would be a second, unreviewed definition of the same numbers.
    What this tool adds is the comparison and an explicit statement of what it
    compared against (``basis``), so a caller cannot mistake the output for a
    platform metric.
    """
    tool_name = "nova.analytics.anomalies"
    window = _window(tool_name, arguments)
    if isinstance(window, McpErrorResult):
        return window
    granularity = _granularity(tool_name, arguments.granularity)
    if isinstance(granularity, McpErrorResult):
        return granularity
    from_day, to_day = window
    baseline_days = (to_day - from_day).days + 1
    previous_to = from_day - timedelta(days=1)
    previous_from = previous_to - timedelta(days=baseline_days - 1)

    def run() -> AnomalyOut:
        with _session() as session:
            service = AnalyticsService(session)
            current = _analytics_call(
                service,
                principal=principal,
                metric="sales.order_count",
                from_day=from_day,
                to_day=to_day,
                granularity=granularity,
            )
            previous = _analytics_call(
                service,
                principal=principal,
                metric="sales.order_count",
                from_day=previous_from,
                to_day=previous_to,
                granularity=granularity,
            )
            refunds = _analytics_call(
                service,
                principal=principal,
                metric="refund.rate",
                from_day=from_day,
                to_day=to_day,
                granularity=granularity,
            )
            order_count = dict(current.get("summary") or {}).get("total")
            previous_count = dict(previous.get("summary") or {}).get("total")
            change = _change_ratio(order_count, previous_count)
            refund_rate = dict(refunds.get("summary") or {}).get("total")
            return AnomalyOut(
                from_day=from_day.isoformat(),
                to_day=to_day.isoformat(),
                granularity=granularity,
                baseline_days=baseline_days,
                order_count=order_count,
                previous_order_count=previous_count,
                refund_rate=refund_rate,
                order_count_change_ratio=change,
                anomaly_suspected=change is not None and abs(change) >= 0.5,
                basis=(
                    f"order_count for {from_day.isoformat()}..{to_day.isoformat()} compared with the "
                    f"equally long window ending {previous_to.isoformat()}; refund_rate is same-window"
                ),
            )

    return await _in_thread(run)


# ---------------------------------------------------------------------------
# Knowledge
# ---------------------------------------------------------------------------
async def knowledge_search(principal: Principal, arguments: KnowledgeSearchInput) -> KnowledgeSearchOut:
    """Retrieve evidence chunks.

    Snippets are returned as ``redact``d text rather than raw document content: a
    knowledge base can hold a pasted support conversation, and this is the one tool
    whose output is *entirely* free text (spec sections 94, 132).
    """
    def run() -> KnowledgeSearchOut:
        with _session() as session:
            result = KnowledgeService(session).debug_retrieval(
                principal=principal,
                request=RetrievalDebugRequest(
                    query=arguments.query,
                    knowledge_base_id=arguments.knowledge_base_id,
                    top_k=arguments.top_k,
                    use_rerank=arguments.use_rerank,
                ),
            )
            evidence = list(result.get("final_evidence") or [])
            return KnowledgeSearchOut(
                query=arguments.query,
                knowledge_base_id=arguments.knowledge_base_id,
                total_evidence=len(evidence),
                evidence=[
                    KnowledgeEvidenceOut(
                        index=int(entry["index"]),
                        doc_id=str(entry["doc_id"]),
                        doc_name=str(entry["doc_name"]),
                        chunk_id=str(entry["chunk_id"]),
                        score=float(entry["score"]),
                        snippet=str(redact(entry["snippet"], purpose=_MCP_PURPOSE)),
                    )
                    for entry in evidence
                ],
            )

    return await _in_thread(run)


# ---------------------------------------------------------------------------
# Promotions (the only write path)
# ---------------------------------------------------------------------------
def _draft(arguments: PromotionPreviewInput) -> PromotionDraft | McpErrorResult:
    """Build the promotion service's own draft model from the wire arguments.

    The MCP layer deliberately does not re-implement the promotion rules: if the
    input is not a valid draft, ``PromotionDraft`` refuses it and that refusal is
    the answer. A second validator here would be a second set of rules to keep in
    agreement with the first.
    """
    try:
        return PromotionDraft(
            name=arguments.name,
            description=arguments.description,
            promotion_type=arguments.promotion_type,
            priority=arguments.priority,
            stackable=arguments.stackable,
            rule_config=dict(arguments.rule_config),
            scope=dict(arguments.scope),
            starts_at=arguments.starts_at,  # type: ignore[arg-type]
            ends_at=arguments.ends_at,  # type: ignore[arg-type]
            total_quota=arguments.total_quota,
        )
    except Exception as exc:  # noqa: BLE001 - pydantic's ValidationError, reported as a coded refusal
        return _fail(ErrorCode.PROMOTION_RULE_INVALID, f"promotion draft is invalid: {exc}")


async def promotion_preview(principal: Principal, arguments: PromotionPreviewInput) -> Any:
    draft = _draft(arguments)
    if isinstance(draft, McpErrorResult):
        return draft

    def run() -> PromotionPreviewOut:
        with _session() as session:
            preview = PromotionService(session).preview(principal=principal, draft=draft)
            return PromotionPreviewOut(
                conflicts=list(preview.get("conflicts") or []),
                estimated_impact=dict(preview.get("estimated_impact") or {}),
                warnings=[str(item) for item in (preview.get("warnings") or [])],
            )

    return await _in_thread(run)


async def promotion_propose(principal: Principal, arguments: PromotionProposeInput) -> Any:
    """Create an approval request. Never create the promotion (REQ-MCP-008).

    ## Why this tool exists at all

    The rest of the surface is read-only, and the one thing an agent is allowed to
    *change* is the approval queue. That is not a compromise of "no writes"; it is
    the only shape in which an LLM may influence commerce state: it can make a
    proposal, and a human with ``promotion:write`` decides. The action is therefore
    recorded with ``risk_level=HIGH`` and ``status=PENDING``, and the actual
    promotion is created later by the governance resume path in
    :mod:`app.modules.governance.approval_graph`, which re-validates the payload
    before executing.

    ## Why ``payload_hash`` is stored on the row

    ``PendingActionService.approve`` compares the hash it is given with the stored
    one, and ``resume`` recomputes it to detect a payload edited between approval
    and execution. A proposal written without a correct hash would be *decidable*
    only with a matching hash the console cannot compute, so the hash is computed
    here with the platform's own :func:`payload_hash` rather than a local copy -
    two implementations of the same hash is exactly how approval silently stops
    matching.

    ## What this does not prove

    It does not prove the promotion is valid: that is ``promotion_preview``'s job,
    and the resume path's re-validation. Creating the row is the tool's whole
    effect. Consequently a caller can propose a rule set that preview would have
    refused; the human reviewing the queue sees the payload and the preview is one
    call away. This is a deliberate division: making the write path depend on the
    read path would mean a promotion cannot be proposed while analytics is
    degraded.
    """
    draft = _draft(arguments)
    if isinstance(draft, McpErrorResult):
        return draft
    # A local re-check of the one permission that gates a write. The policy layer
    # already intersected it, and this is not redundancy for its own sake: the write
    # path is the one place where being wrong is not a refusal a caller can retry
    # around, so the check is repeated at the point of effect rather than trusted
    # from the caller's frame.
    if not principal.has_permission(TOOL_SPECS_BY_NAME["nova.promotion.propose"].permission.value):
        return _fail(
            ErrorCode.INSUFFICIENT_PERMISSION,
            "proposing a promotion requires promotion:write",
        )
    if principal.merchant_id is None:
        return _fail(
            ErrorCode.MCP_WRITE_NOT_PERMITTED,
            "a proposal must name the merchant it is for; a token without one cannot propose",
        )

    payload: dict[str, Any] = {
        "promotion": draft.model_dump(mode="json"),
        "summary": arguments.summary,
        "proposed_via": "mcp",
        "tool_name": "nova.promotion.propose",
    }
    now = utc_now()
    expires_at = now + timedelta(seconds=_PROPOSAL_TTL_SECONDS)

    def run() -> PromotionProposeOut:
        with _session(commit=True) as session:
            action = PendingAction(
                merchant_id=principal.merchant_id,
                agent_run_id=f"mcp:{principal.session_id or principal.user_id}",
                action_type="PROMOTION_CREATE",
                tool_name="nova.promotion.propose",
                summary=arguments.summary,
                risk_level="HIGH",
                status="PENDING",
                payload=payload,
                payload_hash=payload_hash(payload),
                requested_by=principal.user_id,
                expires_at=expires_at,
            )
            session.add(action)
            session.flush()
            action_id = action.id
            status = action.status
            risk = action.risk_level
            action_type = action.action_type
            return PromotionProposeOut(
                pending_action_id=action_id,
                status=status,
                risk_level=risk,
                action_type=action_type,
                approval_required=True,
                # Stated as a fact rather than implied: this tool has no code path
                # that constructs a Promotion, and the value is here so a client
                # can assert on it without reading this source.
                promotion_created=False,
                expires_at=expires_at,
                message=(
                    "Approval request created. No promotion exists yet; a human with promotion:write "
                    "must approve this action before anything is published."
                ),
            )

    return await _in_thread(run)


# ---------------------------------------------------------------------------
# Handlers and validation
# ---------------------------------------------------------------------------
#: Tool name -> handler. Keyed by the frozen name so an accidental rename in the
#: registry surfaces as a missing handler at import time rather than as a tool
#: that lists but cannot run.
HANDLERS: dict[str, Any] = {
    "nova.product.search": product_search,
    "nova.product.get": product_get,
    "nova.sku.get": sku_get,
    "nova.order.get": order_get,
    "nova.order.search": order_search,
    "nova.fulfillment.get": fulfillment_get,
    "nova.after_sale.get": after_sale_get,
    "nova.inventory.get": inventory_get,
    "nova.analytics.sales": analytics_sales,
    "nova.analytics.inventory": analytics_inventory,
    "nova.analytics.product_performance": analytics_product_performance,
    "nova.analytics.refunds": analytics_refunds,
    "nova.analytics.anomalies": analytics_anomalies,
    "nova.knowledge.search": knowledge_search,
    "nova.promotion.preview": promotion_preview,
    "nova.promotion.propose": promotion_propose,
}


def invalid_arguments(tool_name: str, exc: Exception) -> NoReturn:
    """The one place a pydantic rejection becomes a coded MCP refusal."""
    _fail(ErrorCode.AGENT_TOOL_INPUT_INVALID, f"invalid arguments for {tool_name}: {exc}")


async def dispatch(tool_name: str, arguments: dict[str, Any]) -> Any:
    """Validate, then run, then return a serialisable output model.

    Returns an :class:`McpErrorResult` for every refusal this layer can express, and
    raises :class:`McpToolError` only when the request itself could not be
    authorized.
    """
    spec = TOOL_SPECS_BY_NAME.get(tool_name)
    if spec is None:
        return _fail(ErrorCode.MCP_TOOL_NOT_EXPOSED, f"unknown tool {tool_name!r}")
    handler = HANDLERS.get(tool_name)
    if handler is None:  # pragma: no cover - guarded by the registry test
        return _fail(ErrorCode.MCP_TOOL_NOT_EXPOSED, f"no handler is registered for {tool_name!r}")
    try:
        parsed = spec.input_model.model_validate(arguments)
    except Exception as exc:  # noqa: BLE001 - a pydantic ValidationError, reported as a code
        return invalid_arguments(tool_name, exc)

    principal = current_principal()  # raises McpToolError when the request is unauthorized
    result: Any = await handler(principal, parsed)
    # A tool that refused reports it as a value (see McpErrorResult). The SDK's call
    # path has no slot for a non-model return, so the refusal is raised here - once,
    # at the boundary - rather than at each of the fifteen call sites. The raised text
    # is the same string the value would have produced.
    if isinstance(result, McpErrorResult):
        raise McpToolError(result.code, result.message)
    return result


__all__ = [
    "HANDLERS",
    "AfterSaleGetOut",
    "AnalyticsWindowOut",
    "AnomalyOut",
    "FulfillmentGetOut",
    "InventoryGetOut",
    "KnowledgeSearchOut",
    "McpErrorResult",
    "McpToolError",
    "OrderGetOut",
    "OrderSearchOut",
    "ProductGetOut",
    "ProductSearchOut",
    "PromotionPreviewOut",
    "PromotionProposeOut",
    "SkuGetOut",
    "after_sale_get",
    "analytics_anomalies",
    "analytics_inventory",
    "analytics_product_performance",
    "analytics_refunds",
    "analytics_sales",
    "configure_tool_runtime",
    "current_principal",
    "current_scopes",
    "current_verified_token",
    "dispatch",
    "error_code_for",
    "error_result",
    "fulfillment_get",
    "inventory_get",
    "knowledge_search",
    "order_get",
    "order_search",
    "product_get",
    "product_search",
    "promotion_preview",
    "promotion_propose",
    "sku_get",
]
