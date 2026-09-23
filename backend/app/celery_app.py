"""Celery worker and beat entry points for database-backed reconciliation."""

from __future__ import annotations

from celery import Celery

from app.core.config import get_settings
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
