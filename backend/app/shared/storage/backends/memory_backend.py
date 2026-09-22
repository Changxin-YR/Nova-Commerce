"""In-memory storage backend for unit tests.

**Deliberately not a production backend.** It exists so that services which
merely *reference* storage (catalog image metadata, knowledge ingestion
bookkeeping) can be unit-tested without a container, and so that failure paths
such as "object missing" can be triggered deterministically.

Spec §122 / FG-19 explicitly forbids substituting this for real MinIO when
proving the storage gate: a passing in-memory run says nothing about S3
multipart behaviour, presigned URL reachability, or restart durability. The
class therefore refuses the name "fake MinIO" and is only wired up when
``APP_ENV=test``.
"""

from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.shared.storage.port import (
    ALLOWED_CONTENT_TYPES,
    ObjectStat,
    ObjectStorage,
    StorageBucketUnavailableError,
    StorageChecksumMismatchError,
    StorageObjectNotFoundError,
    StorageObjectTooLargeError,
    StorageUnsupportedContentTypeError,
    StoredObject,
    build_object_key,
    guess_content_type,
    validate_object_key,
)


class InMemoryObjectStorage(ObjectStorage):
    """Dictionary-backed storage. Test-only."""

    backend_name = "memory"

    def __init__(self, settings: Settings | None = None) -> None:
        self.bucket_product_images = "nova-product-images"
        self.bucket_knowledge = "nova-knowledge-private"
        self.bucket_reports = "nova-reports"
        self.bucket_evidence = "nova-evidence"
        self._max_bytes = getattr(settings, "S3_MAX_UPLOAD_BYTES", 26_214_400) if settings else 26_214_400
        self._default_ttl = getattr(settings, "S3_SIGNED_URL_TTL_SECONDS", 900) if settings else 900

        self._buckets: set[str] = set()
        #: (bucket, key) -> (bytes, content_type, store_metadata, StoredObject)
        self._objects: dict[tuple[str, str], tuple[bytes, str, dict[str, str], StoredObject]] = {}
        self._lock = threading.Lock()
        #: When True, ``get_object`` raises even for present keys - lets a test
        #: simulate the storage-outage and INV-017 failure paths.
        self.simulate_outage = False

    # -- lifecycle -------------------------------------------------------
    def ensure_buckets(self) -> list[str]:
        created = []
        for bucket in (
            self.bucket_product_images,
            self.bucket_knowledge,
            self.bucket_reports,
            self.bucket_evidence,
        ):
            if bucket not in self._buckets:
                self._buckets.add(bucket)
                created.append(bucket)
        return created

    def _require_bucket(self, bucket: str) -> None:
        if bucket not in self._buckets:
            msg = f"bucket {bucket!r} does not exist"
            raise StorageBucketUnavailableError(msg)

    # -- write -----------------------------------------------------------
    def put_object(
        self,
        *,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        validate_object_key(key)
        if len(data) > self._max_bytes:
            msg = f"object exceeds the {self._max_bytes} byte limit"
            raise StorageObjectTooLargeError(msg)
        self._require_bucket(bucket)

        resolved_type = content_type or guess_content_type(key)
        checksum = hashlib.sha256(data).hexdigest()
        # Mirror the real backend, which persists the digest as object metadata.
        # If the double and MinIO disagreed here, a test could pass against the
        # double and fail in production.
        store_metadata = {"sha256": checksum, **(metadata or {})}
        stored = StoredObject(
            object_key=key,
            bucket=bucket,
            checksum=checksum,
            content_type=resolved_type,
            size=len(data),
            etag=checksum[:32],
            stored_at=datetime.now(UTC),
        )
        with self._lock:
            self._objects[(bucket, key)] = (data, resolved_type, store_metadata, stored)
        return stored

    # -- read ------------------------------------------------------------
    def _entry(self, bucket: str, key: str) -> tuple[bytes, str, dict[str, str], StoredObject]:
        validate_object_key(key)
        if self.simulate_outage:
            msg = "simulated storage outage"
            raise StorageBucketUnavailableError(msg)
        self._require_bucket(bucket)
        try:
            return self._objects[(bucket, key)]
        except KeyError as exc:
            msg = f"object {bucket}/{key} does not exist"
            raise StorageObjectNotFoundError(msg) from exc

    def get_object(self, *, bucket: str, key: str) -> bytes:
        payload, _, _, _ = self._entry(bucket, key)
        return payload

    def stat_object(self, *, bucket: str, key: str) -> ObjectStat:
        payload, content_type, store_metadata, stored = self._entry(bucket, key)
        return ObjectStat(
            object_key=key,
            bucket=bucket,
            size=len(payload),
            content_type=content_type,
            etag=stored.etag,
            last_modified=stored.stored_at,
            metadata=dict(store_metadata),
        )

    def object_exists(self, *, bucket: str, key: str) -> bool:
        try:
            self.stat_object(bucket=bucket, key=key)
            return True
        except (StorageObjectNotFoundError, StorageBucketUnavailableError):
            return False

    def verify_checksum(self, *, bucket: str, key: str, expected: str) -> bool:
        try:
            actual = hashlib.sha256(self.get_object(bucket=bucket, key=key)).hexdigest()
        except StorageObjectNotFoundError as exc:
            msg = f"cannot verify {bucket}/{key}: object is missing"
            raise StorageChecksumMismatchError(msg) from exc
        return actual == expected

    # -- presigned -------------------------------------------------------
    def presigned_get_url(self, *, bucket: str, key: str, ttl_seconds: int | None = None) -> str:
        self._entry(bucket, key)  # 404 for a missing object, like the real thing
        ttl = ttl_seconds or self._default_ttl
        expires = int((datetime.now(UTC) + timedelta(seconds=ttl)).timestamp())
        return f"memory://{bucket}/{key}?expires={expires}"

    def presigned_put_url(
        self,
        *,
        bucket: str,
        key: str,
        ttl_seconds: int | None = None,
        content_type: str | None = None,
    ) -> str:
        self._require_bucket(bucket)
        # Validate the declared type at presign time, matching the real backend:
        # catching a disallowed type before the upload starts is far cheaper than
        # discovering it after the bytes have landed.
        resolved_type = (content_type or guess_content_type(key)).split(";")[0].strip().lower()
        if resolved_type not in ALLOWED_CONTENT_TYPES:
            msg = f"content type {content_type!r} is not allowed"
            raise StorageUnsupportedContentTypeError(msg)
        ttl = ttl_seconds or self._default_ttl
        expires = int((datetime.now(UTC) + timedelta(seconds=ttl)).timestamp())
        return (
            f"memory://{bucket}/{key}"
            f"?method=PUT&content-type={resolved_type}&expires={expires}"
        )

    def delete_object(self, *, bucket: str, key: str) -> None:
        validate_object_key(key)
        with self._lock:
            self._objects.pop((bucket, key), None)

    # -- test affordances -------------------------------------------------
    def put_named(
        self,
        *,
        bucket: str,
        prefix: str,
        filename: str,
        data: bytes,
        owner_id: int | str | None = None,
        unique: str | None = None,
        content_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> StoredObject:
        key = build_object_key(prefix=prefix, filename=filename, owner_id=owner_id, unique=unique)
        return self.put_object(
            bucket=bucket,
            key=key,
            data=data,
            content_type=content_type,
            metadata={k: str(v) for k, v in (metadata or {}).items()},
        )

    def clear(self) -> None:
        with self._lock:
            self._objects.clear()
            self._buckets.clear()


__all__ = ["InMemoryObjectStorage"]
