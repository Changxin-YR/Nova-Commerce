"""Object storage factory.

Selects the backend from configuration and memoises it, so that
``ensure_buckets()`` is called once per process rather than on every request.

Selection rule: the in-memory backend is reachable **only** when
``APP_ENV=test``. That is intentional - spec §122 forbids proving the storage
gate against anything but real MinIO, and the cheapest way to make that rule
hard to break is to make the fake unreachable in any other environment.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.shared.storage.port import ObjectStorage

logger = get_logger(__name__)

_storage: ObjectStorage | None = None


def build_object_storage(settings: Settings | None = None) -> ObjectStorage:
    resolved = settings or get_settings()

    if resolved.is_testing:
        from app.shared.storage.backends.memory_backend import InMemoryObjectStorage

        logger.debug("using in-memory object storage (test environment)")
        return InMemoryObjectStorage(resolved)

    from app.shared.storage.backends.minio_backend import MinioObjectStorage

    return MinioObjectStorage(resolved)


def get_object_storage(*, ensure: bool = False) -> ObjectStorage:
    """Return the process-wide storage backend.

    ``ensure=True`` idempotently creates the buckets and is used at startup;
    request paths should not pay that round-trip.
    """
    global _storage
    if _storage is None:
        _storage = build_object_storage()
    if ensure:
        created = _storage.ensure_buckets()
        if created:
            logger.info("object storage initialised", created_buckets=created)
    return _storage


def set_object_storage(storage: ObjectStorage | None) -> None:
    """Override the backend. Used by tests."""
    global _storage
    _storage = storage


def storage_health() -> tuple[bool, str]:
    """Probe for ``/health/ready``. Storage is degradable, not critical (§130)."""
    try:
        storage = get_object_storage()
        health = getattr(storage, "health", None)
        if callable(health):
            return health()  # type: ignore[no-any-return]
        storage.ensure_buckets()
        return True, "ok"
    except Exception as exc:
        return False, type(exc).__name__


__all__ = [
    "build_object_storage",
    "get_object_storage",
    "set_object_storage",
    "storage_health",
]
