"""The transactional outbox (spec §49, REQ-CON-003).

    contract.py   the event vocabulary and payload shapes
    writer.py     OutboxWriter - append an event inside the caller's transaction
    publisher.py  publish_due / run_publish_cycle - deliver and retry
    (the model lives in app.shared.db.models.outbox)

## The shape of the pattern

Emit (inside a business transaction):

    OutboxWriter(session).enqueue(
        event_type=OutboxEventType.REFUND_SUCCEEDED,
        aggregate_type=OutboxAggregateType.REFUND,
        aggregate_id=refund.id,
        idempotency_key=<the emitter's own key>,
        payload=refund_succeeded_payload(...),
    )
    # ... the caller's one commit() makes the refund row and this event row
    # visible together, or neither.

Publish (a separate, retryable step):

    run_publish_cycle()

The three marked seams that call ``enqueue`` are ``CreateOrderWorkflow`` step 9,
``PaymentSuccessWorkflow`` step 10 and ``RefundWorkflow`` step 7. A row that
nobody publishes is worse than no row, which is why the writer and the publisher
land together rather than the table first.
"""

from app.shared.db.models.outbox import (
    MAX_PUBLISH_ATTEMPTS,
    OUTBOX_STATUSES,
    OutboxMessage,
    OutboxStatus,
)
from app.shared.outbox.contract import (
    OUTBOX_AGGREGATE_TYPES,
    OUTBOX_EVENT_TYPES,
    OutboxAggregateType,
    OutboxEventType,
    order_created_payload,
    payment_settled_payload,
    refund_succeeded_payload,
)
from app.shared.outbox.publisher import (
    LoggingTransport,
    OutboxTransport,
    PublishOutcome,
    publish_due,
    run_publish_cycle,
)
from app.shared.outbox.redis_transport import RedisStreamTransport
from app.shared.outbox.writer import OutboxWriter

__all__ = [
    "MAX_PUBLISH_ATTEMPTS",
    "OUTBOX_AGGREGATE_TYPES",
    "OUTBOX_EVENT_TYPES",
    "OUTBOX_STATUSES",
    "LoggingTransport",
    "OutboxAggregateType",
    "OutboxEventType",
    "OutboxMessage",
    "OutboxStatus",
    "OutboxTransport",
    "OutboxWriter",
    "PublishOutcome",
    "RedisStreamTransport",
    "order_created_payload",
    "payment_settled_payload",
    "publish_due",
    "refund_succeeded_payload",
    "run_publish_cycle",
]
