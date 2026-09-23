"""Cross-module persistence models.

Models live here, rather than inside one bounded context, when **more than one
context writes the same table**. The rule matters because a table owned by a
context that nobody else may import is a table nobody else can use; the choice is
either a shared model or a duplicated one, and a duplicated one drifts.
"""

from app.shared.db.models.idempotency import IdempotencyRecord, IdempotencyStatus
from app.shared.db.models.outbox import (
    MAX_PUBLISH_ATTEMPTS,
    OUTBOX_STATUSES,
    OutboxMessage,
    OutboxStatus,
)

__all__ = [
    "MAX_PUBLISH_ATTEMPTS",
    "OUTBOX_STATUSES",
    "IdempotencyRecord",
    "IdempotencyStatus",
    "OutboxMessage",
    "OutboxStatus",
]
