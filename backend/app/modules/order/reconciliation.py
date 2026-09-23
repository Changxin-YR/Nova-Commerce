"""Close expired unpaid orders from database state, independently of Celery state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.inventory.enums import OperatorType as InventoryOperatorType, ReferenceType
from app.modules.inventory.service import InventoryService
from app.modules.marketing.coupon_service import CouponService
from app.modules.order.enums import OperatorType, OrderStatus, PaymentStatus
from app.modules.order.models import Order
from app.modules.order.repository import OrderStatusLogRepository
from app.modules.order.state_machine import assert_transition
from app.modules.order.workflow import order_release_movement_key
from app.modules.payment.enums import PaymentRecordStatus
from app.modules.payment.models import Payment
from app.shared.db.base import utc_now
from app.shared.db.session import session_scope

EXPIRED_REASON = "payment window expired"


def close_expired_order(session: Session, *, order_id: int, now: datetime) -> bool:
    """Recheck and close one order under row locks; the caller owns the transaction.

    A callback locks payment before order. Taking the same order here prevents a
    callback and this task from each owning one lock and waiting for the other.
    The due-order scan is only a hint; all business guards run again after locks.
    """
    payments = list(
        session.execute(
            select(Payment)
            .where(Payment.order_id == order_id)
            .order_by(Payment.id)
            .with_for_update()
        ).scalars()
    )
    order = session.execute(select(Order).where(Order.id == order_id).with_for_update()).scalar_one_or_none()
    if (
        order is None
        or order.order_status != OrderStatus.PENDING_PAYMENT.value
        or order.expires_at is None
        or order.expires_at > now
        or order.paid_amount != 0
        or order.payment_status not in {PaymentStatus.UNPAID.value, PaymentStatus.PAYING.value}
        or any(payment.status == PaymentRecordStatus.SUCCESS.value for payment in payments)
    ):
        return False

    assert_transition(order.order_status, OrderStatus.CLOSED)
    CouponService(session).release(order=order, now=now)
    inventory = InventoryService(session)
    for item in sorted(order.items, key=lambda row: row.sku_id):
        inventory.release(
            sku_id=item.sku_id,
            quantity=item.quantity,
            idempotency_key=order_release_movement_key(
                order_no=order.order_no, sku_id=item.sku_id
            ),
            warehouse_id=item.warehouse_id,
            reference_type=ReferenceType.ORDER,
            reference_id=order.id,
            operator_type=InventoryOperatorType.WORKER,
            operator_id=None,
            reason=EXPIRED_REASON,
        )

    for payment in payments:
        if payment.status in {
            PaymentRecordStatus.INITIATED.value,
            PaymentRecordStatus.PAYING.value,
        }:
            payment.status = PaymentRecordStatus.CLOSED.value
            payment.closed_at = now
            payment.updated_at = now

    order.order_status = OrderStatus.CLOSED.value
    order.payment_status = PaymentStatus.UNPAID.value
    order.cancel_reason = EXPIRED_REASON
    order.closed_at = now
    order.updated_at = now
    order.version = (order.version or 0) + 1
    OrderStatusLogRepository(session).append(
        order=order,
        from_status=OrderStatus.PENDING_PAYMENT,
        to_status=OrderStatus.CLOSED,
        operator_type=OperatorType.WORKER.value,
        operator_id=None,
        reason=EXPIRED_REASON,
    )
    session.flush()
    return True


def run_expired_order_cycle(*, now: datetime | None = None, limit: int = 100) -> int:
    """Process a bounded due batch; each order commits independently."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    resolved_now = now or utc_now()
    with session_scope(readonly=True) as session:
        ids = list(
            session.execute(
                select(Order.id)
                .where(
                    Order.order_status == OrderStatus.PENDING_PAYMENT.value,
                    Order.expires_at.is_not(None),
                    Order.expires_at <= resolved_now,
                )
                .order_by(Order.expires_at, Order.id)
                .limit(limit)
            ).scalars()
        )
    closed = 0
    for order_id in ids:
        with session_scope() as session:
            closed += int(close_expired_order(session, order_id=order_id, now=resolved_now))
    return closed
