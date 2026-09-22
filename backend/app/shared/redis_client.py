"""Redis client, distributed locks and the rate-limit store.

Spec references:
    §27  Redis locks must **never** replace the single-MySQL inventory
         transaction. The lock helper here is therefore scoped to genuinely
         non-transactional concerns (idempotency guards, rate limits, cache
         filling, outbox leader election) and the architecture test asserts the
         inventory module does not import it.
    §50  reconciliation jobs are enqueued/paced through Redis.
    §83  the LangGraph checkpointer uses its own logical database so a cache
         flush cannot corrupt agent session state.
    §109 rate limiting and brute-force protection.
    §130 Redis is *important*, not critical: when it is down the API must keep
         serving traffic, so every helper fails open on connection errors except
         where fail-closed is explicitly required.

Logical database separation matters more than it looks: sharing one Redis
database between the cache, the Celery broker and the checkpointer means a
``FLUSHDB`` during incident response silently destroys in-flight tasks and agent
sessions. Separate indices make that impossible by accident.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from typing import Any

import redis
from redis.client import Redis
from redis.exceptions import RedisError

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_client: Redis | None = None

#: Released only if we still own the lock, so a slow holder cannot delete a lock
#: that a later holder legitimately acquired.
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


def configure_redis(settings: Settings | None = None) -> Redis:
    """Create (or return) the shared Redis client."""
    global _client
    if _client is not None:
        return _client

    resolved = settings or get_settings()
    password = resolved.REDIS_PASSWORD.get_secret_value() or None

    pool = redis.ConnectionPool(
        host=resolved.REDIS_HOST,
        port=resolved.REDIS_PORT,
        db=resolved.REDIS_DB_CACHE,
        password=password,
        decode_responses=True,
        max_connections=50,
        socket_timeout=resolved.REDIS_SOCKET_TIMEOUT_SECONDS,
        socket_connect_timeout=resolved.REDIS_SOCKET_TIMEOUT_SECONDS,
        # Without health checks a pooled connection that the server closed while
        # idle raises on the next use; this makes the pool self-healing.
        health_check_interval=30,
        retry_on_timeout=True,
    )
    _client = Redis(connection_pool=pool)
    logger.info("redis configured", host=resolved.REDIS_HOST, port=resolved.REDIS_PORT)
    return _client


def get_redis() -> Redis:
    if _client is None:
        return configure_redis()
    return _client


def set_redis(client: Redis | None) -> None:
    """Override the client. Used by tests."""
    global _client
    _client = client


def ping_redis() -> tuple[bool, str]:
    """Probe for ``/health/ready``. Redis is *important*: a failure degrades
    features (rate limiting, caching, async work) but must not stop commerce."""
    try:
        get_redis().ping()
        return True, "ok"
    except RedisError as exc:
        return False, type(exc).__name__
    except Exception as exc:
        return False, type(exc).__name__


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------
def cache_get(key: str) -> str | None:
    """Read-through cache get. Fails open: a cache miss on a broken cache is
    indistinguishable from a cold cache, and both are safe."""
    try:
        return get_redis().get(key)
    except RedisError:
        logger.warning("cache get failed; treating as a miss", key=key)
        return None


def cache_set(key: str, value: str, *, ttl_seconds: int | None = None) -> bool:
    try:
        return bool(get_redis().set(key, value, ex=ttl_seconds))
    except RedisError:
        logger.warning("cache set failed", key=key)
        return False


def cache_delete(*keys: str) -> int:
    if not keys:
        return 0
    try:
        return int(get_redis().delete(*keys))
    except RedisError:
        return 0


# ---------------------------------------------------------------------------
# Distributed lock (NOT for inventory - see module docstring)
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def distributed_lock(
    name: str,
    *,
    ttl_seconds: int = 30,
    blocking: bool = False,
    timeout_seconds: float = 5.0,
) -> Iterator[bool]:
    """Best-effort mutual exclusion.

    Yields ``True`` when the lock was acquired and ``False`` when it was not, so
    the caller decides whether contention is an error or a "someone else is
    already doing this" short-circuit. Redis being down yields ``True``: for its
    intended uses (cache stampede suppression, leader election for a scheduled
    sweep) proceeding without the lock is safer than refusing to work, and the
    underlying operation is idempotent by construction.

    This is explicitly **not** the inventory mechanism. Spec §27 forbids that,
    and a lock that can fail open must never guard a stock deduction.
    """
    client = get_redis()
    token = uuid.uuid4().hex
    key = f"nexora:lock:{name}"
    acquired = False

    try:
        deadline = timeout_seconds
        while True:
            try:
                acquired = bool(client.set(key, token, nx=True, px=int(ttl_seconds * 1000)))
            except RedisError:
                logger.warning("lock backend unavailable; proceeding unlocked", lock=name)
                acquired = True
                token = ""
                break
            if acquired or not blocking:
                break
            deadline -= 1.0
            if deadline <= 0:
                break
            import time

            time.sleep(1.0)

        yield acquired
    finally:
        if acquired and token:
            with contextlib.suppress(RedisError, Exception):
                client.eval(_RELEASE_SCRIPT, 1, key, token)


# ---------------------------------------------------------------------------
# Counters used by rate limiting / brute-force protection (§109)
# ---------------------------------------------------------------------------
def increment_counter(key: str, *, ttl_seconds: int) -> int:
    """Increment and set/refresh TTL atomically.

    Returns 0 when Redis is unavailable, which callers treat as "unknown" and
    therefore must fail *open* for ordinary traffic. Login brute-force
    protection is the exception: it fails closed against a local counter, because
    an unavailable limiter must not become an open door to credential stuffing.
    """
    try:
        client = get_redis()
        pipe = client.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl_seconds)
        count, _ = pipe.execute()
        return int(count)
    except RedisError:
        return 0


def counter_value(key: str) -> int:
    try:
        value = get_redis().get(key)
        return int(value) if value else 0
    except (RedisError, ValueError):
        return 0


def reset_counter(key: str) -> None:
    with contextlib.suppress(RedisError):
        get_redis().delete(key)


# ---------------------------------------------------------------------------
# Idempotency / dedupe primitive
# ---------------------------------------------------------------------------
def claim_once(key: str, *, ttl_seconds: int) -> bool:
    """Return ``True`` for the first caller of ``key`` within the TTL.

    Used for "exactly one worker should act on this" cases such as outbox
    publication. Unlike :func:`distributed_lock` this **fails closed**: if the
    dedupe backend is unavailable, returning ``True`` could duplicate a business
    effect, so it raises instead and the caller retries later.
    """
    try:
        return bool(get_redis().set(key, "1", nx=True, ex=ttl_seconds))
    except RedisError as exc:
        msg = f"cannot claim {key!r}: dedupe backend unavailable"
        raise RuntimeError(msg) from exc


def redis_info() -> dict[str, Any]:
    """Diagnostics for the evidence reports."""
    with contextlib.suppress(RedisError):
        info = get_redis().info()
        return {
            "version": info.get("redis_version"),
            "mode": info.get("redis_mode"),
            "appendonly": info.get("aof_enabled"),
            "used_memory_human": info.get("used_memory_human"),
        }
    return {}


__all__ = [
    "cache_delete",
    "cache_get",
    "cache_set",
    "claim_once",
    "configure_redis",
    "counter_value",
    "distributed_lock",
    "get_redis",
    "increment_counter",
    "ping_redis",
    "redis_info",
    "reset_counter",
    "set_redis",
]
