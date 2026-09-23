"""Real-MySQL expiry checks, including two workers racing for one order."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from app.modules.inventory.enums import MovementType
from app.modules.inventory.models import Inventory, InventoryMovement
from app.modules.order.enums import OrderStatus, PaymentStatus
from app.modules.order.models import Order, OrderStatusLog
from app.modules.order.reconciliation import close_expired_order
from app.modules.order.service import OrderService
from app.modules.order.workflow import OrderLineInput
from app.modules.payment.enums import PaymentRecordStatus
from app.modules.payment.models import Payment
from app.shared.db.base import utc_now
from app.shared.db.session import get_session_factory, session_scope

from .conftest import Shop

pytestmark = pytest.mark.integration


def _order(shop: Shop, *, suffix: str) -> int:
    with get_session_factory()() as session:
        return OrderService(session).create_order(
            principal=shop.consumer,
            items=[OrderLineInput(shop.sku_ids[0], 2)],
            address_id=shop.address_id,
            client_request_id=shop.client_request_id(suffix),
            idempotency_key=shop.key(suffix),
        ).order.id


def test_expiry_closes_payment_and_releases_stock_once(shop: Shop) -> None:
    order_id = _order(shop, suffix="expire-payment")
    now = utc_now()
    with session_scope() as session:
        order = session.get(Order, order_id)
        assert order is not None
        order.expires_at = now - timedelta(minutes=1)
        order.payment_status = PaymentStatus.PAYING.value
        payment = Payment(
            payment_no=f"NVPAY{shop.marker}",
            order_id=order.id,
            order_no=order.order_no,
            merchant_id=shop.merchant_id,
            user_id=shop.consumer_id,
            channel="MOCK",
            amount=order.payable_amount,
            status=PaymentRecordStatus.PAYING.value,
            idempotency_key=shop.key("expire-payment-attempt"),
            client_request_id=shop.client_request_id("expire-payment-attempt"),
            request_hash="a" * 64,
            paid_amount=0,
            refunded_amount=0,
            expires_at=order.expires_at,
        )
        session.add(payment)

    with session_scope() as session:
        assert close_expired_order(session, order_id=order_id, now=now) is True
    with session_scope() as session:
        assert close_expired_order(session, order_id=order_id, now=now) is False

    with session_scope(readonly=True) as session:
        order = session.get(Order, order_id)
        assert order is not None
        assert order.order_status == OrderStatus.CLOSED.value
        assert order.payment_status == PaymentStatus.UNPAID.value
        assert order.closed_at is not None
        assert order.cancel_reason == "payment window expired"
        payment = session.execute(select(Payment).where(Payment.order_id == order_id)).scalar_one()
        assert payment.status == PaymentRecordStatus.CLOSED.value
        assert payment.closed_at is not None
        inventory = session.execute(
            select(Inventory).where(
                Inventory.warehouse_id == shop.warehouse_id,
                Inventory.sku_id == shop.sku_ids[0],
            )
        ).scalar_one()
        assert inventory.locked_qty == 0
        assert inventory.available_qty == 1000
        assert session.execute(
            select(func.count()).select_from(InventoryMovement).where(
                InventoryMovement.reference_id == order_id,
                InventoryMovement.movement_type == MovementType.ORDER_RELEASE.value,
            )
        ).scalar_one() == 1
        logs = session.execute(
            select(OrderStatusLog).where(OrderStatusLog.order_id == order_id)
        ).scalars().all()
        assert [log.to_status for log in logs] == ["PENDING_PAYMENT", "CLOSED"]
        assert logs[-1].operator_type == "WORKER"


def test_two_workers_close_one_expired_order_once(shop: Shop) -> None:
    order_id = _order(shop, suffix="expire-race")
    now = utc_now()
    with session_scope() as session:
        order = session.get(Order, order_id)
        assert order is not None
        order.expires_at = now - timedelta(minutes=1)

    barrier = Barrier(3)

    def attempt() -> bool:
        barrier.wait(timeout=10)
        with session_scope() as session:
            return close_expired_order(session, order_id=order_id, now=now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        barrier.wait(timeout=10)
        assert sorted(future.result(timeout=20) for future in futures) == [False, True]

    with session_scope(readonly=True) as session:
        assert session.execute(
            select(func.count()).select_from(InventoryMovement).where(
                InventoryMovement.reference_id == order_id,
                InventoryMovement.movement_type == MovementType.ORDER_RELEASE.value,
            )
        ).scalar_one() == 1


def test_unexpired_order_remains_open(shop: Shop) -> None:
    order_id = _order(shop, suffix="not-expired")
    with session_scope() as session:
        assert close_expired_order(session, order_id=order_id, now=utc_now()) is False
    with session_scope(readonly=True) as session:
        order = session.get(Order, order_id)
        assert order is not None
        assert order.order_status == OrderStatus.PENDING_PAYMENT.value
