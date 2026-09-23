"""Publish outbox events to a Redis Stream with a stable consumer identity.

The outbox row is the durable business fact. Redis is a delivery channel: a
database commit can fail after XADD, so consumers must deduplicate by event_id.
WAITAOF confirms that the local Redis AOF has the XADD before the database marks
the row PUBLISHED. A failed confirmation leaves the row eligible for retry.
"""

from __future__ import annotations

import json
from functools import lru_cache

from redis import Redis
from redis.typing import EncodableT

from app.core.config import get_settings
from app.shared.db.models.outbox import OutboxMessage

__all__ = ["RedisStreamTransport"]


@lru_cache(maxsize=1)
def _broker_client() -> Redis:
    settings = get_settings()
    return Redis.from_url(
        settings.CELERY_BROKER_URL,
        decode_responses=True,
        socket_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
    )


class RedisStreamTransport:
    """Append one event to the environment's stream and await local AOF sync."""

    def __init__(self, *, client: Redis | None = None, stream_name: str | None = None) -> None:
        self._client = client if client is not None else _broker_client()
        self.stream_name = stream_name or f"nova:{get_settings().APP_ENV}:outbox:events:v1"

    def deliver(self, message: OutboxMessage) -> None:
        if message.id is None:
            raise ValueError("an outbox event needs a persisted id before delivery")
        fields: dict[EncodableT, EncodableT] = {
            "event_id": str(message.id),
            "event_type": message.event_type,
            "aggregate_type": message.aggregate_type,
            "aggregate_id": str(message.aggregate_id),
            "merchant_id": "" if message.merchant_id is None else str(message.merchant_id),
            "payload": json.dumps(message.payload, sort_keys=True, separators=(",", ":")),
        }
        # Both commands use the same Redis connection. WAITAOF applies to writes
        # from that connection, which would not be guaranteed with two pool calls.
        with self._client.pipeline(transaction=False) as pipe:
            pipe.xadd(self.stream_name, fields)
            pipe.execute_command("WAITAOF", 1, 0, 5000)
            _stream_id, durability = pipe.execute()
        if not isinstance(durability, (list, tuple)) or int(durability[0]) < 1:
            raise RuntimeError("Redis did not confirm local AOF persistence")
