"""Coupon templates and lock/use/release transitions against real MySQL."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.errors import (
    CouponAlreadyLockedError,
    CouponNotApplicableError,
    CouponThresholdNotMetError,
    InsufficientStockError,
    PromotionPreviewRequiredError,
)
from app.modules.identity.enums import PermissionCode
from app.modules.identity.models import Permission, Role
from app.modules.identity.repository import RoleRepository
from app.modules.identity.service import AuthService
from app.modules.inventory.models import Inventory
from app.modules.marketing.coupon_schemas import CouponCreate, CouponDraft
from app.modules.marketing.coupon_service import CouponService
from app.modules.marketing.models import CouponTemplate, CouponUsageRecord, UserCoupon
from app.modules.marketing.reconciliation import run_coupon_expiry_cycle
from app.modules.order.service import OrderService
from app.modules.order.workflow import OrderLineInput
from app.modules.payment.providers import sign_body
from app.modules.payment.service import PaymentService
from app.shared.db.base import utc_now
from app.shared.db.session import get_session_factory, session_scope

from .conftest import PASSWORD, Shop

pytestmark = pytest.mark.integration


def _staff(shop: Shop):
    return replace(
        shop.staff,
        permissions=frozenset({PermissionCode.COUPON_READ.value, PermissionCode.COUPON_WRITE.value}),
    )


def _draft(shop: Shop) -> CouponDraft:
    return CouponDraft.model_validate(
        {
            "name": "Order integration coupon",
            "coupon_type": "FIXED_AMOUNT",
            "face_value_amount": 300,
            "discount_bps": None,
            "threshold_amount": 0,
            "max_discount_amount": None,
            "total_quota": 2,
            "per_user_limit": 2,
            "validity_type": "RELATIVE",
            "valid_days": 1,
            "valid_from": None,
            "valid_to": None,
            "applicable_scope": {
                "all_products": False,
                "product_ids": [shop.product_id],
                "category_ids": [],
            },
        }
    )


def _claim(shop: Shop) -> tuple[int, int, CouponDraft, str]:
    draft = _draft(shop)
    with session_scope() as session:
        service = CouponService(session)
        preview = service.preview(principal=_staff(shop), draft=draft)
        assert preview["estimated_impact"]["affected_sku_count"] == 3
        token = preview["preview_token"]
        template = service.create(
            principal=_staff(shop),
            payload=CouponCreate.model_validate({**draft.model_dump(), "preview_token": token}),
        )
        template_id = template.id
        service.transition(principal=_staff(shop), template_id=template_id, publish=True)
        coupon = service.claim(principal=shop.consumer, template_id=template_id)
        coupon_id = coupon.id
    return template_id, coupon_id, draft, token


def _order(shop: Shop, coupon_id: int, suffix: str):
    with get_session_factory()() as session:
        return OrderService(session).create_order(
            principal=shop.consumer,
            items=[OrderLineInput(shop.sku_ids[0], 2)],
            address_id=shop.address_id,
            coupon_id=coupon_id,
            client_request_id=shop.client_request_id(suffix),
            idempotency_key=shop.key(suffix),
        ).order


def test_coupon_locks_on_order_and_releases_on_cancel(shop: Shop) -> None:
    template_id, coupon_id, draft, token = _claim(shop)
    with session_scope() as session:
        same = CouponService(session).create(
            principal=_staff(shop),
            payload=CouponCreate.model_validate({**draft.model_dump(), "preview_token": token}),
        )
        assert same.id == template_id
        with pytest.raises(PromotionPreviewRequiredError):
            CouponService(session).create(
                principal=_staff(shop),
                payload=CouponCreate.model_validate(
                    {**draft.model_dump(), "name": "changed", "preview_token": token}
                ),
            )

    with session_scope(readonly=True) as session:
        cart = OrderService(session).preview(
            principal=shop.consumer,
            items=[OrderLineInput(shop.sku_ids[0], 2)],
            coupon_id=coupon_id,
        )
        assert cart.coupon_discount_amount == 300
    order = _order(shop, coupon_id, "coupon-first")
    assert order.coupon_discount_amount == 300
    with session_scope(readonly=True) as session:
        coupon = session.get(UserCoupon, coupon_id)
        assert coupon is not None and coupon.status == "LOCKED" and coupon.order_id == order.id
    with pytest.raises(CouponAlreadyLockedError):
        _order(shop, coupon_id, "coupon-conflict")

    with session_scope() as session:
        OrderService(session).cancel(principal=shop.consumer, order_no=order.order_no)
    with session_scope(readonly=True) as session:
        coupon = session.get(UserCoupon, coupon_id)
        assert coupon is not None and coupon.status == "UNUSED" and coupon.order_id is None
    second = _order(shop, coupon_id, "coupon-second")
    assert second.coupon_discount_amount == 300
    with session_scope(readonly=True) as session:
        actions = list(session.execute(
            select(CouponUsageRecord.action).where(CouponUsageRecord.user_coupon_id == coupon_id)
            .order_by(CouponUsageRecord.id)
        ).scalars())
        assert actions == ["LOCK", "RELEASE", "LOCK"]


def test_two_orders_cannot_lock_the_same_coupon(shop: Shop) -> None:
    _, coupon_id, _, _ = _claim(shop)
    barrier = Barrier(3)

    def attempt(suffix: str) -> str:
        barrier.wait(timeout=10)
        try:
            _order(shop, coupon_id, suffix)
        except CouponAlreadyLockedError:
            return "LOCKED"
        return "CREATED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, suffix) for suffix in ("coupon-race-a", "coupon-race-b")]
        barrier.wait(timeout=10)
        assert sorted(future.result(timeout=30) for future in futures) == ["CREATED", "LOCKED"]
    with session_scope(readonly=True) as session:
        coupon = session.get(UserCoupon, coupon_id)
        assert coupon is not None and coupon.status == "LOCKED"


def test_concurrent_claims_respect_template_and_user_limits(shop: Shop) -> None:
    draft = _draft(shop).model_copy(update={"total_quota": 1, "per_user_limit": 1})
    with session_scope() as session:
        service = CouponService(session)
        token = service.preview(principal=_staff(shop), draft=draft)["preview_token"]
        template = service.create(
            principal=_staff(shop),
            payload=CouponCreate.model_validate({**draft.model_dump(), "preview_token": token}),
        )
        template_id = template.id
        service.transition(principal=_staff(shop), template_id=template_id, publish=True)
    barrier = Barrier(3)

    def claim() -> str:
        barrier.wait(timeout=10)
        try:
            with session_scope() as session:
                CouponService(session).claim(principal=shop.consumer, template_id=template_id)
        except CouponNotApplicableError:
            return "REJECTED"
        return "ISSUED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim) for _ in range(2)]
        barrier.wait(timeout=10)
        assert sorted(future.result(timeout=30) for future in futures) == ["ISSUED", "REJECTED"]
    with session_scope(readonly=True) as session:
        template = session.get(CouponTemplate, template_id)
        assert template is not None and template.issued_count == 1


def test_selected_coupon_fails_if_threshold_is_not_met(shop: Shop) -> None:
    draft = _draft(shop).model_copy(update={"threshold_amount": 10000})
    with session_scope() as session:
        service = CouponService(session)
        token = service.preview(principal=_staff(shop), draft=draft)["preview_token"]
        template = service.create(
            principal=_staff(shop),
            payload=CouponCreate.model_validate({**draft.model_dump(), "preview_token": token}),
        )
        service.transition(principal=_staff(shop), template_id=template.id, publish=True)
        coupon_id = service.claim(principal=shop.consumer, template_id=template.id).id
    with session_scope(readonly=True) as session, pytest.raises(CouponThresholdNotMetError):
        OrderService(session).preview(
            principal=shop.consumer,
            items=[OrderLineInput(shop.sku_ids[0], 2)],
            coupon_id=coupon_id,
        )


def test_failed_stock_reservation_rolls_coupon_lock_back(shop: Shop) -> None:
    _, coupon_id, _, _ = _claim(shop)
    with session_scope() as session:
        inventory = session.execute(
            select(Inventory).where(
                Inventory.sku_id == shop.sku_ids[0],
                Inventory.warehouse_id == shop.warehouse_id,
            )
        ).scalar_one()
        inventory.available_qty = 0
    with pytest.raises(InsufficientStockError):
        _order(shop, coupon_id, "coupon-no-stock")
    with session_scope(readonly=True) as session:
        coupon = session.get(UserCoupon, coupon_id)
        assert coupon is not None and coupon.status == "UNUSED" and coupon.order_id is None
        assert not list(session.execute(
            select(CouponUsageRecord.id).where(CouponUsageRecord.user_coupon_id == coupon_id)
        ).scalars())


def test_mock_payment_marks_locked_coupon_used(client, shop: Shop) -> None:
    _, coupon_id, _, _ = _claim(shop)
    order = _order(shop, coupon_id, "coupon-pay")
    with session_scope() as session:
        payment = PaymentService(session).create(
            principal=shop.consumer,
            order_no=order.order_no,
            channel="MOCK",
            client_request_id=shop.client_request_id("coupon-payment"),
            idempotency_key=shop.key("coupon-payment"),
        ).payment
        payment_no = payment.payment_no
        amount = payment.amount
    event_id = f"coupon-{shop.marker}-settled"
    body = json.dumps(
        {
            "event_id": event_id,
            "payment_no": payment_no,
            "order_no": order.order_no,
            "event_type": "PAYMENT_SUCCEEDED",
            "transaction_no": f"COUPON-{event_id}",
            "amount": amount,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    stamp = str(int(datetime.now(UTC).timestamp()))
    response = client.post(
        "/api/v1/payments/callbacks/MOCK",
        content=body,
        headers={
            "X-Provider-Event-Id": event_id,
            "X-Provider-Timestamp": stamp,
            "X-Provider-Signature": sign_body(
                secret=get_settings().PAYMENT_CALLBACK_SECRET.get_secret_value(),
                timestamp=stamp,
                raw_body=body,
            ),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    with session_scope(readonly=True) as session:
        coupon = session.get(UserCoupon, coupon_id)
        assert coupon is not None and coupon.status == "USED" and coupon.order_id == order.id
        actions = list(session.execute(
            select(CouponUsageRecord.action).where(CouponUsageRecord.user_coupon_id == coupon_id)
            .order_by(CouponUsageRecord.id)
        ).scalars())
        assert actions == ["LOCK", "USE"]


def test_coupon_http_preview_create_claim_and_admin_list(client, shop: Shop) -> None:
    with session_scope() as session:
        role = session.execute(select(Role).where(Role.merchant_id == shop.merchant_id)).scalar_one()
        roles = RoleRepository(session)
        for code, action in (("coupon:read", "read"), ("coupon:write", "write")):
            permission = roles.get_permission_by_code(code)
            if permission is None:
                permission = roles.add_permission(
                    Permission(code=code, resource="coupon", action=action, description="Coupon tests")
                )
            roles.grant_permission(role_id=role.id, permission_id=permission.id)
    with session_scope() as session:
        auth = AuthService(session, get_settings())
        staff_token = auth.login(
            identifier=shop.staff_username, password=PASSWORD, client_ip="127.0.0.1"
        ).access_token
        consumer_token = auth.login(
            identifier=shop.consumer_username, password=PASSWORD, client_ip="127.0.0.1"
        ).access_token
    staff_headers = {"Authorization": f"Bearer {staff_token}"}
    consumer_headers = {"Authorization": f"Bearer {consumer_token}"}
    draft = _draft(shop).model_dump(mode="json")
    preview = client.post("/api/v1/marketing/coupons/preview", json=draft, headers=staff_headers)
    assert preview.status_code == 200, preview.text
    created = client.post(
        "/api/v1/marketing/coupons",
        json={**draft, "preview_token": preview.json()["data"]["preview_token"]},
        headers=staff_headers,
    )
    assert created.status_code == 200, created.text
    template_id = created.json()["data"]["id"]
    assert created.json()["data"]["status"] == "DRAFT"
    listing = client.get("/api/v1/marketing/admin/coupons", headers=staff_headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["data"]["meta"]["total"] == 1
    published = client.post(
        f"/api/v1/marketing/coupons/{template_id}/publish", json={}, headers=staff_headers
    )
    assert published.status_code == 200, published.text
    claimed = client.post(
        f"/api/v1/marketing/coupons/{template_id}/claim", json={}, headers=consumer_headers
    )
    assert claimed.status_code == 200, claimed.text
    coupon_id = claimed.json()["data"]["id"]
    mine = client.get("/api/v1/marketing/coupons/mine", headers=consumer_headers)
    assert mine.status_code == 200, mine.text
    assert coupon_id in [row["id"] for row in mine.json()["data"]]
    with session_scope(readonly=True) as session:
        template = session.get(CouponTemplate, template_id)
        assert template is not None and template.issued_count == 1


def test_expiry_reconciler_skips_locked_coupons_and_expires_unused(shop: Shop) -> None:
    template_id, unused_id, _, _ = _claim(shop)
    with session_scope() as session:
        unused = session.get(UserCoupon, unused_id)
        assert unused is not None
        unused.valid_from = utc_now() - timedelta(days=2)
        unused.valid_to = utc_now() - timedelta(days=1)
    assert run_coupon_expiry_cycle() == 1
    assert run_coupon_expiry_cycle() == 0
    with session_scope(readonly=True) as session:
        unused = session.get(UserCoupon, unused_id)
        assert unused is not None and unused.status == "EXPIRED"

    with session_scope() as session:
        locked = CouponService(session).claim(principal=shop.consumer, template_id=template_id)
        locked_id = locked.id
    order = _order(shop, locked_id, "coupon-expiry-locked")
    with session_scope() as session:
        locked = session.get(UserCoupon, locked_id)
        assert locked is not None
        locked.valid_from = utc_now() - timedelta(days=2)
        locked.valid_to = utc_now() - timedelta(days=1)
    assert run_coupon_expiry_cycle() == 0
    with session_scope() as session:
        OrderService(session).cancel(principal=shop.consumer, order_no=order.order_no)
    with session_scope(readonly=True) as session:
        locked = session.get(UserCoupon, locked_id)
        assert locked is not None and locked.status == "EXPIRED" and locked.order_id is None
