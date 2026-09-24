import argparse
import json
from dataclasses import asdict, replace
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.modules.fulfillment.models import Fulfillment
from app.modules.inventory.models import Inventory, InventoryMovement
from app.modules.order.models import Order, OrderItem, OrderStatusLog
from app.modules.payment.models import Payment, PaymentCallback
from app.shared.db.models.audit import AuditRecord
from app.shared.db.models.idempotency import IdempotencyRecord
from app.shared.db.models.outbox import OutboxMessage
from app.shared.db.session import get_session_factory
from tests.integration.payment.conftest import PASSWORD, PAYABLE_AMOUNT, Shop, _purge, _seed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("seed", "seed-checkout", "resolve", "verify", "cleanup"))
    parser.add_argument("--shop")
    arguments = parser.parse_args()
    if arguments.mode in {"seed", "seed-checkout"}:
        shop = _seed(uuid4().hex[:8].upper())
        if arguments.mode == "seed-checkout":
            with get_session_factory()() as session:
                session.execute(delete(Payment).where(Payment.order_id == shop.order_id))
                session.execute(delete(OrderStatusLog).where(OrderStatusLog.order_id == shop.order_id))
                session.execute(delete(OrderItem).where(OrderItem.order_id == shop.order_id))
                session.execute(delete(Order).where(Order.id == shop.order_id))
                session.execute(delete(InventoryMovement).where(InventoryMovement.sku_id.in_(shop.sku_ids)))
                for position in session.scalars(select(Inventory).where(Inventory.sku_id.in_(shop.sku_ids))):
                    position.locked_qty = 0
                session.commit()
            shop = replace(shop, extra={"checkout": True})
        print(json.dumps({"shop": asdict(shop), "password": PASSWORD}))
        return
    shop = Shop(**json.loads(arguments.shop))
    if shop.extra.get("checkout"):
        with get_session_factory()() as session:
            order = session.scalar(select(Order).where(Order.user_id == shop.consumer_id, Order.merchant_id == shop.merchant_id))
            if order is not None:
                payment = session.scalar(select(Payment).where(Payment.order_id == order.id))
                shop = replace(shop, order_id=order.id, order_no=order.order_no,
                               payment_id=payment.id if payment else 0, payment_no=payment.payment_no if payment else "",
                               order_item_ids=tuple(item.id for item in order.items))
    if arguments.mode == "resolve":
        print(json.dumps(asdict(shop)))
        return
    if arguments.mode == "cleanup":
        with get_session_factory()() as session:
            session.execute(delete(IdempotencyRecord).where(
                ((IdempotencyRecord.resource_type == "ORDER") & (IdempotencyRecord.resource_id == shop.order_id))
                | ((IdempotencyRecord.resource_type == "PAYMENT") & (IdempotencyRecord.resource_id == shop.payment_id))
            ))
            session.commit()
        _purge(shop)
        return
    with get_session_factory()() as session:
        order = session.get(Order, shop.order_id)
        payment = session.get(Payment, shop.payment_id)
        assert order.order_status == "PROCESSING"
        assert order.paid_amount == payment.paid_amount == PAYABLE_AMOUNT
        assert payment.status == "SUCCESS"
        callbacks = list(session.scalars(select(PaymentCallback).where(PaymentCallback.payment_no == shop.payment_no)))
        assert len(callbacks) in (1, 2)
        assert all(row.process_status == "PROCESSED" and row.signature_valid for row in callbacks)
        assert session.scalar(select(func.count()).select_from(InventoryMovement).where(InventoryMovement.sku_id.in_(shop.sku_ids), InventoryMovement.movement_type == "ORDER_DEDUCT")) == 2
        assert all(row.locked_qty == 0 for row in session.scalars(select(Inventory).where(Inventory.sku_id.in_(shop.sku_ids))))
        assert session.scalar(select(func.count()).select_from(Fulfillment).where(Fulfillment.order_id == shop.order_id)) == 1
        assert session.scalar(select(func.count()).select_from(AuditRecord).where(AuditRecord.merchant_id == shop.merchant_id, AuditRecord.action == "payment.settled")) == 1
        assert session.scalar(select(func.count()).select_from(OutboxMessage).where(OutboxMessage.merchant_id == shop.merchant_id)) == (2 if shop.extra.get("checkout") else 1)
        print(json.dumps({"verified": ["Order", "Payment", "PaymentCallback", "InventoryMovement", "Fulfillment", "Audit", "Outbox"]}))


if __name__ == "__main__":
    main()
