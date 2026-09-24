"""Idempotent compensation refunds for verified late payments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.modules.audit.service import AuditService
from app.modules.order.enums import PaymentStatus
from app.modules.order.repository import OrderRepository
from app.modules.payment.enums import PaymentRecordStatus
from app.modules.payment.models import Payment, PaymentCompensationRefund
from app.modules.payment.repository import PaymentRepository
from app.shared.db.session import session_scope

logger = get_logger(__name__)


class RefundSender(Protocol):
    def __call__(self, payment: Payment, amount: int, *, idempotency_key: str) -> str: ...


@dataclass(frozen=True, slots=True)
class CompensationRefundResult:
    refund: PaymentCompensationRefund
    replayed: bool


class PaymentCompensationService:
    """Execute one refund intent while preserving its single payment effect."""

    def __init__(self, session: Session, sender: RefundSender | None = None) -> None:
        self._session = session
        self._payments = PaymentRepository(session)
        self._orders = OrderRepository(session)
        self._sender = sender

    def execute(
        self, *, compensation_id: int, sender: RefundSender | None = None
    ) -> CompensationRefundResult:
        intent = self._payments.get_compensation_refund_for_update(
            compensation_id=compensation_id
        )
        if intent is None:
            raise ValueError("compensation refund not found")
        if intent.status == "SUCCEEDED":
            return CompensationRefundResult(intent, replayed=True)
        if intent.status == "RECONCILIATION_REQUIRED":
            return CompensationRefundResult(intent, replayed=True)

        payment = self._payments.get(intent.payment_id, for_update=True)
        order = self._orders.get(intent.order_id, for_update=True)
        if payment is None or order is None:
            intent.status = "RECONCILIATION_REQUIRED"
            intent.failure_reason = "payment or order disappeared"
            self._session.commit()
            return CompensationRefundResult(intent, replayed=False)
        if payment.status not in {
            PaymentRecordStatus.SUCCESS.value,
            PaymentRecordStatus.PARTIAL_REFUNDED.value,
        } or payment.refundable_amount < intent.amount:
            intent.status = "RECONCILIATION_REQUIRED"
            intent.failure_reason = "payment is not refundable for the intent amount"
            self._session.commit()
            return CompensationRefundResult(intent, replayed=False)

        intent.status = "RECONCILIATION_REQUIRED"
        intent.failure_reason = "provider dispatch outcome not yet confirmed"
        AuditService(self._session).record(
            merchant_id=order.merchant_id,
            actor_id=None,
            actor_type="SYSTEM",
            action="payment.compensation_refund.dispatch_started",
            resource_type="PAYMENT_COMPENSATION_REFUND",
            resource_id=intent.id,
            result="SUCCESS",
            after={"status": intent.status},
        )
        self._session.commit()
        refund_sender = sender or self._sender
        try:
            if refund_sender is None:
                raise RuntimeError("no payment provider refund sender configured")
            provider_refund_no = refund_sender(
                payment, intent.amount, idempotency_key=f"late-payment-refund:{intent.id}"
            )
            if not provider_refund_no:
                raise ValueError("provider did not return a refund reference")
        except (RuntimeError, ValueError, OSError) as exc:
            logger.warning("compensation_refund_dispatch_failed", compensation_id=compensation_id, error_type=type(exc).__name__)
            intent.status = "RECONCILIATION_REQUIRED"
            intent.failure_reason = f"provider dispatch failed: {type(exc).__name__}"
            self._session.flush()
            AuditService(self._session).record(
                merchant_id=order.merchant_id,
                actor_id=None,
                actor_type="SYSTEM",
                action="payment.compensation_refund.failed",
                resource_type="PAYMENT_COMPENSATION_REFUND",
                resource_id=intent.id,
                result="FAILURE",
                after={"status": intent.status, "failure_reason": intent.failure_reason},
            )
            self._session.commit()
            return CompensationRefundResult(intent, replayed=False)

        self._session.expire_all()
        intent = self._payments.get_compensation_refund_for_update(compensation_id=compensation_id)
        if intent is None:
            raise ValueError("compensation refund disappeared")
        payment = self._payments.get(intent.payment_id, for_update=True)
        order = self._orders.get(intent.order_id, for_update=True)
        if payment is None or order is None:
            raise ValueError("compensation payment or order disappeared")
        payment.refunded_amount += intent.amount
        payment.status = (
            PaymentRecordStatus.REFUNDED.value
            if payment.refunded_amount == payment.paid_amount
            else PaymentRecordStatus.PARTIAL_REFUNDED.value
        )
        order.refunded_amount += intent.amount
        order.payment_status = PaymentStatus.REFUNDED.value
        intent.status = "SUCCEEDED"
        intent.provider_refund_no = provider_refund_no
        intent.failure_reason = None
        self._session.flush()
        AuditService(self._session).record(
            merchant_id=order.merchant_id,
            actor_id=None,
            actor_type="SYSTEM",
            action="payment.compensation_refund.succeeded",
            resource_type="PAYMENT_COMPENSATION_REFUND",
            resource_id=intent.id,
            result="SUCCESS",
            after={"status": intent.status, "provider_refund_no": provider_refund_no},
        )
        self._session.commit()
        return CompensationRefundResult(intent, replayed=False)

    def run_pending(self, *, limit: int = 100, sender: RefundSender | None = None) -> int:
        intent_ids = [intent.id for intent in self._payments.list_compensation_refunds(limit=limit)]
        self._session.commit()
        completed = 0
        for intent_id in intent_ids:
            result = self.execute(compensation_id=intent_id, sender=sender)
            completed += int(result.refund.status == "SUCCEEDED" and not result.replayed)
        return completed


def run_compensation_cycle(*, limit: int = 100, sender: RefundSender | None = None) -> int:
    if limit <= 0:
        raise ValueError("limit must be positive")
    with session_scope() as session:
        return PaymentCompensationService(session, sender=sender).run_pending(limit=limit)


__all__ = ["CompensationRefundResult", "PaymentCompensationService", "RefundSender", "run_compensation_cycle"]
