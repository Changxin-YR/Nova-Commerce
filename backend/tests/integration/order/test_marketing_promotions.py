"""Promotion preview, creation and order pricing against real MySQL."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.errors import PromotionPreviewRequiredError, ValidationError
from app.modules.identity.enums import PermissionCode
from app.modules.identity.models import Permission, Role
from app.modules.identity.repository import RoleRepository
from app.modules.identity.service import AuthService
from app.modules.marketing.models import Promotion, PromotionProduct
from app.modules.marketing.schemas import PromotionCreate, PromotionDraft, PromotionScope
from app.modules.marketing.service import PromotionService
from app.modules.order.service import OrderService
from app.modules.order.workflow import OrderLineInput
from app.shared.db.base import utc_now
from app.shared.db.session import get_session_factory, session_scope

from .conftest import PASSWORD, Shop

pytestmark = pytest.mark.integration


def _staff(shop: Shop):
    return replace(
        shop.staff,
        permissions=frozenset(
            {
                PermissionCode.PROMOTION_READ.value,
                PermissionCode.PROMOTION_WRITE.value,
            }
        ),
    )


def _draft(shop: Shop) -> PromotionDraft:
    now = utc_now()
    return PromotionDraft.model_validate(
        {
            "name": "Order integration promotion",
            "description": "Two units receive a fixed discount",
            "promotion_type": "DIRECT_DISCOUNT",
            "priority": 100,
            "stackable": False,
            "rule_config": {"discount_amount": 200},
            "scope": {
                "all_products": False,
                "product_ids": [shop.product_id],
                "category_ids": [],
                "brand_ids": [],
            },
            "starts_at": now - timedelta(minutes=1),
            "ends_at": now + timedelta(hours=1),
            "total_quota": 1,
        }
    )


def _publish(shop: Shop) -> tuple[PromotionDraft, int, str]:
    principal = _staff(shop)
    draft = _draft(shop)
    with session_scope() as session:
        service = PromotionService(session)
        preview = service.preview(principal=principal, draft=draft)
        assert preview["estimated_impact"]["affected_sku_count"] == 3
        assert preview["conflicts"] == []
        token = preview["preview_token"]
        promotion = service.create(
            principal=principal,
            payload=PromotionCreate.model_validate(
                {
                    **draft.model_dump(),
                    "preview_token": token,
                }
            ),
        )
        promotion_id = promotion.id
        service.transition(principal=principal, promotion_id=promotion_id, publish=True)
    return draft, promotion_id, token


def _create(shop: Shop, suffix: str) -> int:
    with get_session_factory()() as session:
        return (
            OrderService(session)
            .create_order(
                principal=shop.consumer,
                items=[OrderLineInput(shop.sku_ids[0], 2)],
                address_id=shop.address_id,
                client_request_id=shop.client_request_id(suffix),
                idempotency_key=shop.key(suffix),
            )
            .order.promotion_discount_amount
        )


def test_preview_token_and_published_rule_price_the_order(shop: Shop) -> None:
    draft, promotion_id, token = _publish(shop)
    with session_scope() as session:
        service = PromotionService(session)
        same = service.create(
            principal=_staff(shop),
            payload=PromotionCreate.model_validate({**draft.model_dump(), "preview_token": token}),
        )
        assert same.id == promotion_id
        with pytest.raises(PromotionPreviewRequiredError):
            service.create(
                principal=_staff(shop),
                payload=PromotionCreate.model_validate(
                    {
                        **draft.model_dump(),
                        "name": "changed after preview",
                        "preview_token": token,
                    }
                ),
            )
        assert (
            session.execute(
                select(func.count())
                .select_from(PromotionProduct)
                .where(PromotionProduct.promotion_id == promotion_id)
            ).scalar_one()
            == 1
        )

    with session_scope(readonly=True) as session:
        cart = OrderService(session).preview(
            principal=shop.consumer, items=[OrderLineInput(shop.sku_ids[0], 2)]
        )
        assert cart.promotion_discount_amount == 400

    assert _create(shop, "promotion-first") == 400
    with session_scope(readonly=True) as session:
        row = session.get(Promotion, promotion_id)
        assert row is not None and row.used_quota == 1
        cart = OrderService(session).preview(
            principal=shop.consumer, items=[OrderLineInput(shop.sku_ids[0], 2)]
        )
        assert cart.promotion_discount_amount == 0
    assert _create(shop, "promotion-second") == 0


def test_preview_rejects_scope_ids_outside_the_merchant(shop: Shop) -> None:
    draft = _draft(shop)
    for scope in (
        PromotionScope(all_products=False, product_ids=[2**63 - 1]),
        PromotionScope(all_products=False, category_ids=[2**63 - 1]),
        PromotionScope(all_products=False, brand_ids=[2**63 - 1]),
    ):
        with session_scope() as session, pytest.raises(ValidationError):
            PromotionService(session).preview(
                principal=_staff(shop), draft=draft.model_copy(update={"scope": scope})
            )


def test_concurrent_orders_cannot_exceed_promotion_quota(shop: Shop) -> None:
    _, promotion_id, _ = _publish(shop)
    barrier = Barrier(3)

    def attempt(suffix: str) -> int:
        barrier.wait(timeout=10)
        return _create(shop, suffix)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, suffix) for suffix in ("promo-a", "promo-b")]
        barrier.wait(timeout=10)
        assert sorted(future.result(timeout=25) for future in futures) == [0, 400]
    with session_scope(readonly=True) as session:
        row = session.get(Promotion, promotion_id)
        assert row is not None and row.used_quota == 1


def test_promotion_http_preview_create_and_task_routes(client, shop: Shop) -> None:
    with session_scope() as session:
        role = session.execute(select(Role).where(Role.merchant_id == shop.merchant_id)).scalar_one()
        roles = RoleRepository(session)
        for code, action in (("promotion:read", "read"), ("promotion:write", "write")):
            permission = roles.get_permission_by_code(code)
            if permission is None:
                permission = roles.add_permission(
                    Permission(code=code, resource="promotion", action=action, description="Promotion tests")
                )
            roles.grant_permission(role_id=role.id, permission_id=permission.id)
    with session_scope() as session:
        token = (
            AuthService(session, get_settings())
            .login(identifier=shop.staff_username, password=PASSWORD, client_ip="127.0.0.1")
            .access_token
        )

    headers = {"Authorization": f"Bearer {token}"}
    draft = _draft(shop).model_dump(mode="json")
    preview = client.post("/api/v1/marketing/promotions/preview", json=draft, headers=headers)
    assert preview.status_code == 200, preview.text
    proof = preview.json()["data"]
    assert proof["promotion"]["id"] is None
    created = client.post(
        "/api/v1/marketing/promotions",
        json={**draft, "preview_token": proof["preview_token"]},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    promotion_id = created.json()["data"]["id"]
    assert created.json()["data"]["status"] == "DRAFT"

    published = client.post(f"/api/v1/marketing/promotions/{promotion_id}/publish", json={}, headers=headers)
    assert published.status_code == 200, published.text
    assert published.json()["data"]["status"] == "ACTIVE"
    storefront = client.get("/api/v1/marketing/promotions")
    assert storefront.status_code == 200
    assert promotion_id in [row["id"] for row in storefront.json()["data"]]
    listing = client.get("/api/v1/marketing/admin/promotions", headers=headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["data"]["meta"]["total"] == 1
    ended = client.post(f"/api/v1/marketing/promotions/{promotion_id}/unpublish", json={}, headers=headers)
    assert ended.status_code == 200, ended.text
    assert ended.json()["data"]["status"] == "ENDED"
