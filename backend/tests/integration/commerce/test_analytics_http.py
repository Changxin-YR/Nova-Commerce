"""Frozen analytics responses from a settled order and real inventory ledger."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.main import create_app
from app.modules.analytics.service import AnalyticsService
from app.modules.identity.enums import PermissionCode
from app.modules.identity.models import Permission, Role
from app.modules.identity.repository import RoleRepository
from app.modules.order.models import Order
from app.modules.order.workflow import OrderLineInput
from app.shared.db.session import get_session_factory

from .seed import PASSWORD, Shop, paid_order

pytestmark = pytest.mark.integration


def _allow_analytics(shop: Shop) -> None:
    with get_session_factory()() as session:
        roles = RoleRepository(session)
        role_id = session.execute(select(Role.id).where(
            Role.merchant_id == shop.merchant_id, Role.code == "ORDER_OPERATOR"
        )).scalar_one()
        code = PermissionCode.ANALYTICS_READ.value
        permission = roles.get_permission_by_code(code)
        if permission is None:
            try:
                with session.begin_nested():
                    permission = roles.add_permission(Permission(
                        code=code, resource="analytics", action="read",
                        description="Analytics fixture",
                    ))
            except IntegrityError:
                permission = roles.get_permission_by_code(code)
        assert permission is not None
        roles.grant_permission(role_id=role_id, permission_id=permission.id)
        session.commit()


def _metric(client: TestClient, headers: dict[str, str], name: str, params: dict | None = None) -> dict:
    response = client.get(
        f"/api/v1/analytics/admin/metrics/{name}", headers=headers, params=params,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_paid_order_drives_all_five_metrics_with_scope_and_permissions(shop: Shop) -> None:
    order = paid_order(shop, lines=[OrderLineInput(shop.sku_ids[0], 3)], suffix="analytics")
    with get_session_factory()() as session:
        settled = session.execute(select(Order).where(Order.id == order.id)).scalar_one()
        assert settled.paid_at is not None
        paid_day = settled.paid_at.date()
        paid_amount = settled.paid_amount

    with TestClient(create_app()) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": shop.staff_username, "password": PASSWORD},
        )
        assert login.status_code == 200, login.text
        headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}
        assert client.get("/api/v1/analytics/admin/metrics/sales.gmv", headers=headers).status_code == 403
        _allow_analytics(shop)

        window = {"from": paid_day.isoformat(), "to": paid_day.isoformat()}
        gmv = _metric(client, headers, "sales.gmv", window)
        assert gmv["unit"] == "minor_currency"
        assert gmv["series"] == [{"bucket": paid_day.isoformat(), "value": paid_amount}]
        assert gmv["summary"]["total"] == paid_amount

        orders = _metric(client, headers, "sales.order_count", window)
        assert orders["unit"] == "count"
        assert orders["summary"]["total"] == 1
        product = _metric(client, headers, "product.performance", window)
        assert product["summary"]["total"] == paid_amount
        assert product["dimensions"][0]["key"] == "sku_id"
        turnover = _metric(client, headers, "inventory.turnover", window)
        assert turnover["unit"] == "ratio"
        # Three units sold. The fixture's three SKUs each enter the ledger
        # with 1000 units during this UTC day, so the opening position is 0
        # and the closing position is 2997.
        assert turnover["summary"]["total"] == pytest.approx(round(6 / 2997, 6))
        refunds = _metric(client, headers, "refund.rate", window)
        assert refunds["unit"] == "ratio"
        assert refunds["summary"]["total"] == 0

        month = _metric(client, headers, "sales.gmv", {**window, "granularity": "month"})
        assert month["series"][0]["bucket"] == paid_day.strftime("%Y-%m")
        yesterday = paid_day - timedelta(days=1)
        empty = _metric(client, headers, "sales.gmv", {
            "from": yesterday.isoformat(), "to": yesterday.isoformat(),
        })
        assert empty["series"] == []
        assert empty["summary"] == {"total": 0, "average": 0, "change_ratio": 0.0}

        invalid = client.get("/api/v1/analytics/admin/metrics/sales.gmv", headers=headers,
                             params={"from": paid_day.isoformat(), "to": yesterday.isoformat()})
        assert invalid.status_code == 422
        unknown = client.get("/api/v1/analytics/admin/metrics/unknown", headers=headers)
        assert unknown.status_code == 422

    foreign = replace(
        shop.staff, merchant_id=shop.merchant_id + 999999,
        permissions=frozenset({PermissionCode.ANALYTICS_READ.value}),
    )
    with get_session_factory()() as session:
        scoped = AnalyticsService(session).metric(
            principal=foreign, metric="sales.gmv", from_day=paid_day,
            to_day=paid_day, granularity="day",
        )
    assert scoped["series"] == []
