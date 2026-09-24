from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.modules.inventory.models import Inventory, InventoryMovement
from app.modules.order.models import Order
from app.modules.order.reconciliation import close_expired_order
from app.modules.payment.compensation import PaymentCompensationService
from app.modules.payment.models import Payment, PaymentCompensationRefund
from app.shared.db.base import utc_now
from app.shared.db.session import get_session_factory

from .conftest import PAYABLE_AMOUNT, _purge, _seed
from .test_payment_http import _settlement_payload, _signed_body

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("provider_fails", [False, True])
@pytest.mark.parametrize("expiry_worker_ran", [False, True])
def test_late_payment_preserves_closed_order_and_refunds_once(client, provider_fails, expiry_worker_ran):
    shop = _seed(uuid4().hex[:8].upper())
    factory = get_session_factory()
    calls = []

    def sender(payment, amount, *, idempotency_key):
        assert idempotency_key == f"late-payment-refund:{intent_id}"
        calls.append((payment.id, amount))
        if provider_fails:
            raise RuntimeError("provider timeout")
        return f"refund-{shop.marker}"

    try:
        with factory() as session:
            order = session.get(Order, shop.order_id)
            order.expires_at = utc_now() - timedelta(seconds=1)
            session.flush()
            if expiry_worker_ran:
                assert close_expired_order(session, order_id=shop.order_id, now=utc_now())
            session.commit()
            positions = list(session.execute(select(Inventory.sku_id, Inventory.available_qty, Inventory.locked_qty).where(Inventory.warehouse_id == shop.warehouse_id)).all())
            movements = list(session.scalars(select(InventoryMovement.id).where(InventoryMovement.sku_id.in_(shop.sku_ids))))

        payload = _settlement_payload(event_id=f"late-{shop.marker}", payment_no=shop.payment_no, order_no=shop.order_no, amount=PAYABLE_AMOUNT)
        body, headers = _signed_body(payload, secret=get_settings().PAYMENT_CALLBACK_SECRET.get_secret_value())
        for delivery in range(2):
            response = client.post("/api/v1/payments/callbacks/MOCK", content=body, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["code"] == (0 if delivery == 0 else 60004)

        with factory() as session:
            intents = list(session.scalars(select(PaymentCompensationRefund).where(PaymentCompensationRefund.payment_id == shop.payment_id)))
            assert len(intents) == 1
            intent_id = intents[0].id
            assert intents[0].status == "PENDING"
            assert session.get(Payment, shop.payment_id).paid_amount == PAYABLE_AMOUNT

        for attempt in range(2):
            with factory() as session:
                result = PaymentCompensationService(session, sender=sender).execute(compensation_id=intent_id)
                assert result.replayed == (attempt == 1)
                assert result.refund.status == ("RECONCILIATION_REQUIRED" if provider_fails else "SUCCEEDED")

        assert calls == [(shop.payment_id, PAYABLE_AMOUNT)]
        with factory() as session:
            order = session.get(Order, shop.order_id)
            payment = session.get(Payment, shop.payment_id)
            assert order.order_status == "CLOSED"
            assert payment.refunded_amount == (0 if provider_fails else PAYABLE_AMOUNT)
            assert order.refunded_amount == payment.refunded_amount
            current_positions = list(session.execute(select(Inventory.sku_id, Inventory.available_qty, Inventory.locked_qty).where(Inventory.warehouse_id == shop.warehouse_id)).all())
            current_movements = list(session.scalars(select(InventoryMovement).where(InventoryMovement.sku_id.in_(shop.sku_ids))))
            if expiry_worker_ran:
                assert current_positions == positions
                assert [row.id for row in current_movements] == movements
            else:
                assert [(row[0], row[1] + row[2]) for row in current_positions] == [(row[0], row[1] + row[2]) for row in positions]
                assert all(row[2] == 0 for row in current_positions)
                added = [row for row in current_movements if row.id not in movements]
                assert len(added) == 2
                assert all(row.movement_type == "ORDER_RELEASE" for row in added)
        response = client.post("/api/v1/payments/callbacks/MOCK", content=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["code"] == 60004
        assert len(calls) == 1
    finally:
        _purge(shop)


def test_crash_after_provider_dispatch_never_dispatches_again(client):
    shop = _seed(uuid4().hex[:8].upper())
    factory = get_session_factory()
    calls = []

    def sender(payment, amount, *, idempotency_key):
        calls.append((payment.id, amount, idempotency_key))
        raise SystemExit("worker terminated after provider accepted refund")

    try:
        with factory() as session:
            order = session.get(Order, shop.order_id)
            order.expires_at = utc_now() - timedelta(seconds=1)
            session.commit()
        payload = _settlement_payload(event_id=f"crash-{shop.marker}", payment_no=shop.payment_no, order_no=shop.order_no, amount=PAYABLE_AMOUNT)
        body, headers = _signed_body(payload, secret=get_settings().PAYMENT_CALLBACK_SECRET.get_secret_value())
        assert client.post("/api/v1/payments/callbacks/MOCK", content=body, headers=headers).json()["code"] == 0
        with factory() as session:
            intent_id = session.scalar(select(PaymentCompensationRefund.id).where(PaymentCompensationRefund.payment_id == shop.payment_id))
        with factory() as session, pytest.raises(SystemExit):
            PaymentCompensationService(session, sender=sender).execute(compensation_id=intent_id)
        with factory() as session:
            result = PaymentCompensationService(session, sender=sender).execute(compensation_id=intent_id)
            assert result.replayed
            assert result.refund.status == "RECONCILIATION_REQUIRED"
            assert session.get(Payment, shop.payment_id).refunded_amount == 0
            assert session.get(Order, shop.order_id).order_status == "CLOSED"
        assert calls == [(shop.payment_id, PAYABLE_AMOUNT, f"late-payment-refund:{intent_id}")]
    finally:
        _purge(shop)
