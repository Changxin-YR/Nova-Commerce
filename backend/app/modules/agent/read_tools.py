from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.errors import AgentToolInputError, AgentToolNotAllowedError
from app.modules.analytics.service import AnalyticsService, Metric
from app.modules.identity.enums import DataScope
from app.modules.identity.service import Principal
from app.modules.knowledge.schemas import RetrievalDebugRequest
from app.modules.knowledge.service import KnowledgeService
from app.modules.order.service import OrderService


class ReadToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_no: str | None = Field(default=None, max_length=64)
    metric: Metric = "sales.gmv"
    from_day: date | None = None
    to_day: date | None = None


@dataclass(frozen=True)
class ReadToolResult:
    name: str
    answer: str
    citations: list[dict]
    output: dict


def execute_read_tool(
    session: Session, *, principal: Principal, agent_name: str,
    message: str, knowledge_base_id: int | None, inputs: ReadToolRequest, today: date,
) -> ReadToolResult:
    if knowledge_base_id is not None:
        if agent_name == "analytics":
            raise AgentToolNotAllowedError("analytics agent only permits analytics.read")
        result = KnowledgeService(session).debug_retrieval(
            principal=principal,
            request=RetrievalDebugRequest(query=message, knowledge_base_id=knowledge_base_id, use_rerank=False),
        )
        citations = result["final_evidence"]
        answer = "\n\n".join(f"[{entry['index']}] {entry['snippet']}" for entry in citations)
        return ReadToolResult("knowledge.read", answer or "未检索到支持此请求的知识证据。", citations, result)
    if agent_name == "analytics":
        if principal.data_scope not in {DataScope.MERCHANT, DataScope.ALL}:
            raise AgentToolNotAllowedError("analytics requires merchant data scope")
        if inputs.order_no is not None:
            raise AgentToolNotAllowedError("analytics agent only permits analytics.read")
        from_day, to_day = inputs.from_day or today, inputs.to_day or today
        result = AnalyticsService(session).metric(principal=principal, metric=inputs.metric,
                                                 from_day=from_day, to_day=to_day, granularity="day")
        answer = json.dumps({"from_day": from_day.isoformat(), "to_day": to_day.isoformat(), **result}, ensure_ascii=False)
        return ReadToolResult("analytics.read", answer, [], result)
    if inputs.order_no is None and message.strip().lower() not in {"orders", "我的订单", "查询订单", "订单列表"}:
        raise AgentToolInputError("provide order_no, request an order list, or select a knowledge base")
    orders = OrderService(session)
    admin = principal.is_staff and principal.data_scope in {DataScope.MERCHANT, DataScope.ALL}
    if inputs.order_no:
        order = (orders.get_admin_order if admin else orders.get_customer_order)(principal=principal, order_no=inputs.order_no)
        rows, total = [order], 1
    else:
        page = (orders.list_admin_orders if admin else orders.list_customer_orders)(principal=principal, page=1, page_size=10)
        rows, total = list(page.rows), page.total
    result = {"total": total, "items": [{"order_no": row.order_no, "order_status": row.order_status,
                                         "paid_amount": row.paid_amount, "payable_amount": row.payable_amount} for row in rows]}
    return ReadToolResult("order.read", json.dumps(result, ensure_ascii=False), [], result)
