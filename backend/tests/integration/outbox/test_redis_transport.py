"""The delivery channel is a real Redis Stream, with a stable event identity."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from redis import Redis

from app.core.config import get_settings
from app.shared.db.models.outbox import OutboxStatus
from app.shared.db.session import get_session_factory
from app.shared.outbox import (
    OutboxAggregateType,
    OutboxEventType,
    OutboxWriter,
    PublishOutcome,
    RedisStreamTransport,
    publish_due,
)
from tests.integration.outbox.conftest import Shop, rows_for_merchant, whole_second

pytestmark = pytest.mark.integration


def test_published_row_is_durable_in_redis_stream(shop: Shop) -> None:
    factory = get_session_factory()
    stream_name = f"nova:test:outbox:{uuid4().hex}"
    client = Redis.from_url(get_settings().CELERY_BROKER_URL, decode_responses=True)
    try:
        with factory() as session:
            message = OutboxWriter(session).enqueue(
                event_type=OutboxEventType.ORDER_CREATED.value,
                aggregate_type=OutboxAggregateType.ORDER.value,
                aggregate_id=110_001,
                idempotency_key=shop.key("redis-stream"),
                payload={"order_no": "NV110001", "payable_amount": 100, "item_count": 1},
                merchant_id=shop.merchant_id,
            )
            message_id = int(message.id)
            session.commit()

        with factory() as session:
            outcome = publish_due(
                session,
                transport=RedisStreamTransport(client=client, stream_name=stream_name),
                now=whole_second(),
                limit=1,
            )
            session.commit()

        assert outcome == PublishOutcome(published=1)
        entries = client.xrange(stream_name)
        assert len(entries) == 1
        fields = entries[0][1]
        assert fields["event_id"] == str(message_id)
        assert fields["event_type"] == "order.created"
        assert fields["aggregate_id"] == "110001"
        assert fields["merchant_id"] == str(shop.merchant_id)
        assert json.loads(fields["payload"])["payable_amount"] == 100
        rows = rows_for_merchant(merchant_id=shop.merchant_id)
        assert len(rows) == 1
        assert rows[0].status == OutboxStatus.PUBLISHED.value
    finally:
        client.delete(stream_name)
        client.close()
