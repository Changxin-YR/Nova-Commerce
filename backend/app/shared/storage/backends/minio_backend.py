"""MinIO / S3-compatible backend.

Spec §20 + §122. This is the only module in the project allowed to import the
MinIO SDK; everything else depends on
:class:`~app.shared.storage.port.ObjectStorage`.

Two endpoints are configured on purpose (ADR-011): ``S3_ENDPOINT`` is used for
server-side operations, while presigned URLs are built against
``S3_PUBLIC_ENDPOINT`` because the browser cannot resolve a docker-internal
hostname. Collapsing them into one variable produces URLs that work in tests and
404 in the browser.
"""

from __future__ import annotations

import io
from datetime import timedelta
from typing import Any

from minio import Minio
from minio.error import S3Error
from urllib3 import PoolManager, Retry
from urllib3.util import Timeout

from app.core.config import Settings
from app.core.logging import get_logger
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
    sha256_hex,
    validate_object_key,
)

logger = get_logger(__name__)

#: MinIO error codes that mean "no such object/bucket" rather than a real fault.
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "NoSuchBucket", "NoSuchObject", "NotFound"})
_CONFLICT_CODES = frozenset({"BucketAlreadyOwnedByYou", "BucketAlreadyExists"})


def _is_not_found(exc: S3Error) -> bool:
    return getattr(exc, "code", "") in _NOT_FOUND_CODES


class MinioObjectStorage(ObjectStorage):
    """S3-compatible storage backed by MinIO."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.bucket_product_images = settings.S3_BUCKET_PRODUCT_IMAGES
        self.bucket_knowledge = settings.S3_BUCKET_KNOWLEDGE
        self.bucket_reports = settings.S3_BUCKET_REPORTS
        self.bucket_evidence = settings.S3_BUCKET_EVIDENCE
        self._max_bytes = settings.S3_MAX_UPLOAD_BYTES
        self._default_ttl = settings.S3_SIGNED_URL_TTL_SECONDS

        http_client = PoolManager(
            timeout=Timeout(
                connect=settings.S3_CONNECT_TIMEOUT_SECONDS,
                read=settings.S3_READ_TIMEOUT_SECONDS,
            ),
            retries=Retry(total=3, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504]),
            maxsize=10,
        )

        self._client = Minio(
            endpoint=self._endpoint_host(settings.S3_ENDPOINT),
            access_key=settings.S3_ACCESS_KEY.get_secret_value(),
            secret_key=settings.S3_SECRET_KEY.get_secret_value(),
            secure=settings.S3_USE_SSL,
            region=settings.S3_REGION,
            http_client=http_client,
        )
        self._public_client = (
            self._client
            if settings.S3_PUBLIC_ENDPOINT == settings.S3_ENDPOINT
            else Minio(
                endpoint=self._endpoint_host(settings.S3_PUBLIC_ENDPOINT),
                access_key=settings.S3_ACCESS_KEY.get_secret_value(),
                secret_key=settings.S3_SECRET_KEY.get_secret_value(),
                secure=settings.S3_PUBLIC_ENDPOINT.startswith("https"),
                region=settings.S3_REGION,
                http_client=http_client,
            )
        )

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _endpoint_host(url: str) -> str:
        """MinIO's client wants ``host:port`` without a scheme."""
        return url.replace("https://", "").replace("http://", "").rstrip("/")

    def _client_for(self, *, presign: bool) -> Minio:
        return self._public_client if presign else self._client

    def _require_bucket(self, bucket: str) -> None:
        try:
            if not self._client.bucket_exists(bucket):
                msg = f"bucket {bucket!r} does not exist"
                raise StorageBucketUnavailableError(msg)
        except S3Error as exc:
            msg = f"cannot reach bucket {bucket!r}: {exc.code}"
            raise StorageBucketUnavailableError(msg) from exc

    # -- lifecycle -------------------------------------------------------
    def ensure_buckets(self) -> list[str]:
        created: list[str] = []
        for bucket in (
            self.bucket_product_images,
            self.bucket_knowledge,
            self.bucket_reports,
            self.bucket_evidence,
        ):
            try:
                if self._client.bucket_exists(bucket):
                    continue
                self._client.make_bucket(bucket, location=self._settings.S3_REGION)
                created.append(bucket)
                logger.info("object storage bucket created", bucket=bucket)
            except S3Error as exc:
                if getattr(exc, "code", "") in _CONFLICT_CODES:
                    continue  # a concurrent replica won the race; that is fine
                msg = f"failed to ensure bucket {bucket!r}: {exc.code}"
                raise StorageBucketUnavailableError(msg) from exc
        return created

    def health(self) -> tuple[bool, str]:
        """Liveness probe for ``/health/ready`` (§130: storage is degradable)."""
        try:
            self._client.bucket_exists(self.bucket_product_images)
            return True, "ok"
        except Exception as exc:
            return False, type(exc).__name__

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
            msg = f"object of {len(data)} bytes exceeds the {self._max_bytes} byte limit"
            raise StorageObjectTooLargeError(msg)
        self._require_bucket(bucket)

        resolved_type = content_type or guess_content_type(key)
        checksum = sha256_hex(data)
        merged_metadata = {"sha256": checksum, **(metadata or {})}

        try:
            result = self._client.put_object(
                bucket_name=bucket,
                object_name=key,
                data=io.BytesIO(data),
                length=len(data),
                content_type=resolved_type,
                metadata=merged_metadata,
            )
        except S3Error as exc:
            msg = f"put_object failed for {bucket}/{key}: {exc.code}"
            raise StorageBucketUnavailableError(msg) from exc

        return StoredObject(
            object_key=key,
            bucket=bucket,
            checksum=checksum,
            content_type=resolved_type,
            size=len(data),
            etag=getattr(result, "etag", None),
        )

    # -- read ------------------------------------------------------------
    def get_object(self, *, bucket: str, key: str) -> bytes:
        validate_object_key(key)
        response = None
        try:
            response = self._client.get_object(bucket, key)
            return response.read()
        except S3Error as exc:
            if _is_not_found(exc):
                msg = f"object {bucket}/{key} does not exist"
                raise StorageObjectNotFoundError(msg) from exc
            msg = f"get_object failed for {bucket}/{key}: {exc.code}"
            raise StorageBucketUnavailableError(msg) from exc
        finally:
            if response is not None:
                response.close()
                response.release_conn()

    def stat_object(self, *, bucket: str, key: str) -> ObjectStat:
        validate_object_key(key)
        try:
            info = self._client.stat_object(bucket, key)
        except S3Error as exc:
            if _is_not_found(exc):
                msg = f"object {bucket}/{key} does not exist"
                raise StorageObjectNotFoundError(msg) from exc
            msg = f"stat_object failed for {bucket}/{key}: {exc.code}"
            raise StorageBucketUnavailableError(msg) from exc

        return ObjectStat(
            object_key=key,
            bucket=bucket,
            size=int(getattr(info, "size", 0) or 0),
            content_type=str(getattr(info, "content_type", "") or "application/octet-stream"),
            etag=getattr(info, "etag", None),
            last_modified=getattr(info, "last_modified", None),
            metadata={str(k): str(v) for k, v in (getattr(info, "metadata", None) or {}).items()},
        )

    def object_exists(self, *, bucket: str, key: str) -> bool:
        try:
            self.stat_object(bucket=bucket, key=key)
            return True
        except StorageObjectNotFoundError:
            return False

    def verify_checksum(self, *, bucket: str, key: str, expected: str) -> bool:
        """Re-read the object and compare digests.

        This is the check behind INV-017: a document must never be marked READY
        on the strength of a database row alone, because the bytes may have been
        lost (spec §122 "Missing Object", "Storage Restart").
        """
        try:
            actual = sha256_hex(self.get_object(bucket=bucket, key=key))
        except StorageObjectNotFoundError as exc:
            msg = f"cannot verify {bucket}/{key}: object is missing"
            raise StorageChecksumMismatchError(msg) from exc
        return actual == expected

    # -- presigned -------------------------------------------------------
    def presigned_get_url(self, *, bucket: str, key: str, ttl_seconds: int | None = None) -> str:
        validate_object_key(key)
        ttl = ttl_seconds or self._default_ttl
        try:
            return self._client_for(presign=True).presigned_get_object(
                bucket, key, expires=timedelta(seconds=ttl)
            )
        except S3Error as exc:
            msg = f"presign failed for {bucket}/{key}: {exc.code}"
            raise StorageBucketUnavailableError(msg) from exc

    def presigned_put_url(
        self,
        *,
        bucket: str,
        key: str,
        ttl_seconds: int | None = None,
        content_type: str | None = None,
    ) -> str:
        validate_object_key(key)
        # MinIO's presigned PUT does not sign a Content-Type, so the header is not
        # enforced by the signature itself. Validating it here still helps: a
        # disallowed type is rejected *before* the client uploads, rather than
        # after the bytes have already landed in the bucket.
        if content_type is not None:
            declared = content_type.split(";")[0].strip().lower()
            if declared not in ALLOWED_CONTENT_TYPES:
                msg = f"content type {content_type!r} is not allowed"
                raise StorageUnsupportedContentTypeError(msg)
        ttl = ttl_seconds or self._default_ttl
        try:
            return self._client_for(presign=True).presigned_put_object(
                bucket, key, expires=timedelta(seconds=ttl)
            )
        except S3Error as exc:
            msg = f"presign failed for {bucket}/{key}: {exc.code}"
            raise StorageBucketUnavailableError(msg) from exc

    def delete_object(self, *, bucket: str, key: str) -> None:
        validate_object_key(key)
        try:
            self._client.remove_object(bucket, key)
        except S3Error as exc:
            if _is_not_found(exc):
                return  # deleting an absent object is a no-op, not an error
            msg = f"delete failed for {bucket}/{key}: {exc.code}"
            raise StorageBucketUnavailableError(msg) from exc

    # -- convenience used by seeds / evidence -----------------------------
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


__all__ = ["MinioObjectStorage"]
