"""Celery worker and beat entry points for database-backed reconciliation."""

from __future__ import annotations

from celery import Celery

from app.core.config import get_settings
from app.modules.governance.service import PendingActionService
from app.modules.marketing.reconciliation import run_coupon_expiry_cycle
from app.modules.order.reconciliation import run_expired_order_cycle
from app.modules.payment.compensation import run_compensation_cycle
from app.shared.db.session import session_scope
from app.shared.outbox.publisher import run_publish_cycle

settings = get_settings()
celery_app = Celery("nova", broker=settings.CELERY_BROKER_URL)
celery_app.conf.update(
    timezone="UTC",
    enable_utc=True,
    task_always_eager=settings.CELERY_TASK_ALWAYS_EAGER,
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
    task_ignore_result=True,
    beat_schedule={
        "publish-outbox": {
            "task": "nova.outbox.publish_due",
            "schedule": 5.0,
        },
        "close-expired-orders": {
            "task": "nova.orders.close_expired",
            "schedule": 30.0,
        },
        "expire-coupons": {
            "task": "nova.coupons.expire_due",
            "schedule": 60.0,
        },
        "compensate-late-payments": {
            "task": "nova.payments.compensate_late",
            "schedule": 30.0,
        },
        "expire-pending-actions": {
            "task": "nova.governance.expire_pending",
            "schedule": 30.0,
        },
    },
)


@celery_app.task(name="nova.outbox.publish_due", ignore_result=True)
def publish_outbox() -> dict[str, int]:
    """One bounded scan; MySQL rows and retry clocks remain the source of truth."""
    outcome = run_publish_cycle()
    return {
        "published": outcome.published,
        "failed": outcome.failed,
        "dead": outcome.dead,
    }


@celery_app.task(name="nova.orders.close_expired", ignore_result=True)
def close_expired_orders() -> int:
    """Close due unpaid orders, with MySQL as the source of truth."""
    return run_expired_order_cycle()


@celery_app.task(name="nova.coupons.expire_due", ignore_result=True)
def expire_coupons() -> int:
    """Expire unused coupons whose validity window has closed."""
    return run_coupon_expiry_cycle()


@celery_app.task(name="nova.payments.compensate_late", ignore_result=True)
def compensate_late_payments() -> int:
    return run_compensation_cycle()


@celery_app.task(name="nova.governance.expire_pending", ignore_result=True)
def expire_pending_actions() -> int:
    with session_scope() as session:
        return PendingActionService(session).expire()
