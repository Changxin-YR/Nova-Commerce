"""Object storage port.

Spec §20 requires S3-compatible storage for product images, knowledge source
files, downloadable reports and large evidence artifacts, with four rules:

    * MySQL never stores large BLOBs;
    * the database stores ``object_key``, ``checksum``, ``content_type``, ``size``;
    * knowledge documents default to a **private** bucket;
    * persistent files never live on the container-local filesystem.

This module defines the abstraction and the invariants that hold for *every*
backend. Business modules import :class:`ObjectStorage` and never a vendor SDK,
which is what keeps the ingestion pipeline unit-testable while FG-19 still runs
against real MinIO.

Security note (§109 path traversal, §122 storage gate): object keys are
attacker-influenced - they arrive as user-supplied filenames - so key
construction and validation live here, at the boundary, rather than being
repeated (and eventually forgotten) in each caller.
"""

from __future__ import annotations

import hashlib
import mimetypes
import posixpath
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, BinaryIO, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
#: Conservative allow-list. Anything not listed is rejected outright rather than
#: guessed, because a wrong guess about an upload's type is how a parser gets
#: fed hostile input (spec §52 MIME/extension/signature validation).
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "image/avif",
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)

#: Magic-number prefixes for signature validation (spec §52 step 1).
#: Only formats where a stable prefix exists are listed; formats without a
#: reliable signature are validated structurally by the parser instead.
_SIGNATURES: tuple[tuple[str, tuple[bytes, ...]], ...] = (
    ("application/pdf", (b"%PDF-",)),
    ("image/jpeg", (b"\xff\xd8\xff",)),
    ("image/png", (b"\x89PNG\r\n\x1a\n",)),
    ("image/gif", (b"GIF87a", b"GIF89a")),
    ("application/zip", (b"PK\x03\x04",)),  # docx/xlsx are zip containers
)

# Unicode-aware: a Chinese-language platform must accept 日本語マニュアル.pdf.
# Safety comes from reducing to ONE path segment plus the traversal checks in
# validate_object_key(), not from restricting the alphabet.
_UNSAFE_KEY_CHARS = re.compile(r"[^\w.\-]", re.UNICODE)
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_MULTI_SLASH = re.compile(r"/{2,}")

#: Maximum percent-decoding rounds. Chained encoding (%252e -> %2e -> .) is the
#: standard way past a single-pass filter, so decoding repeats until stable.
_MAX_DECODE_ROUNDS = 6

MAX_KEY_LENGTH = 512
MAX_SEGMENT_LENGTH = 128


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class StorageError(Exception):
    """Base class for storage failures."""


class StorageObjectNotFoundError(StorageError):
    """The requested key does not exist."""


class StorageBucketUnavailableError(StorageError):
    """The bucket does not exist and could not be created."""


class StorageChecksumMismatchError(StorageError):
    """Stored bytes do not match the recorded checksum (INV-017)."""


class StorageUnsafeKeyError(StorageError):
    """The key failed validation (path traversal / illegal characters)."""


class StorageObjectTooLargeError(StorageError):
    """The payload exceeds the configured limit."""


class StorageUnsupportedContentTypeError(StorageError):
    """The content type is not on the allow-list, or contradicts its signature."""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StoredObject:
    """Result of a successful write.

    The four fields below are exactly what spec §20 requires the database to
    persist, so a caller can save this row-for-row without reinterpretation.
    """

    object_key: str
    bucket: str
    checksum: str  # SHA-256 hex digest
    content_type: str
    size: int
    etag: str | None = None
    stored_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_db_columns(self) -> dict[str, Any]:
        return {
            "object_key": self.object_key,
            "checksum": self.checksum,
            "content_type": self.content_type,
            "size": self.size,
        }


@dataclass(frozen=True, slots=True)
class ObjectStat:
    object_key: str
    bucket: str
    size: int
    content_type: str
    etag: str | None = None
    last_modified: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Key construction and validation
# ---------------------------------------------------------------------------
def sanitise_filename(filename: str, *, fallback: str = "upload") -> str:
    """Reduce an untrusted filename to a safe, single path segment.

    Unicode is normalised first: without NFKC, full-width and combining
    characters can survive a naive filter and be re-interpreted by a downstream
    filesystem or parser.
    """
    normalised = unicodedata.normalize("NFKC", filename or "").strip()
    # Strip any directory component the client tried to smuggle in.
    normalised = normalised.replace("\\", "/").split("/")[-1]
    normalised = _WINDOWS_DRIVE.sub("", normalised)
    normalised = normalised.strip(". ")
    normalised = _UNSAFE_KEY_CHARS.sub("_", normalised)
    normalised = re.sub(r"_{2,}", "_", normalised)
    if not normalised or normalised in {".", ".."}:
        return fallback
    stem, dot, suffix = normalised.rpartition(".")
    if not dot:
        stem, suffix = normalised, ""
    stem = stem[: MAX_SEGMENT_LENGTH - len(suffix) - 1]
    return f"{stem}.{suffix}" if suffix else stem


def _fully_decode(value: str, *, max_rounds: int = _MAX_DECODE_ROUNDS) -> str:
    """Percent-decode until the value stops changing.

    A single ``unquote`` pass is not enough: ``%252e%252e`` decodes once to
    ``%2e%2e`` and only on the second pass to ``..``. A filter that decodes once
    therefore sees a harmless-looking string and lets a traversal through.
    """
    previous = value
    for _ in range(max_rounds):
        current = urllib.parse.unquote(previous)
        if current == previous:
            break
        previous = current
    return previous


def validate_object_key(key: str) -> str:
    """Validate a full object key, returning it unchanged when safe.

    Rejects, in order:

    * empty keys and keys over :data:`MAX_KEY_LENGTH`;
    * NUL bytes (which truncate C-string handling in some S3 clients);
    * absolute paths and Windows drive letters;
    * ``..`` traversal in any segment, including URL-encoded forms, because a
      backend that decodes before resolving would otherwise escape the prefix;
    * control characters.

    The check is intentionally *paranoid and local*: FG-19 asserts that a
    traversal attempt is rejected, and that assertion is only meaningful if the
    rejection happens in shared code rather than in one lucky caller.
    """
    if not key:
        msg = "object key must not be empty"
        raise StorageUnsafeKeyError(msg)
    if len(key) > MAX_KEY_LENGTH:
        msg = f"object key exceeds {MAX_KEY_LENGTH} characters"
        raise StorageUnsafeKeyError(msg)
    if "\x00" in key:
        msg = "object key must not contain NUL bytes"
        raise StorageUnsafeKeyError(msg)
    if any(ord(char) < 32 or ord(char) == 127 for char in key):
        msg = "object key must not contain control characters"
        raise StorageUnsafeKeyError(msg)

    normalised = _fully_decode(key).replace("\\", "/")
    if normalised.startswith("/") or _WINDOWS_DRIVE.match(normalised):
        msg = f"object key must be relative, got {key!r}"
        raise StorageUnsafeKeyError(msg)
    if normalised.startswith("~") or normalised.startswith("$"):
        msg = f"object key must not start with a shell/home marker: {key!r}"
        raise StorageUnsafeKeyError(msg)

    segments = normalised.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        msg = f"object key contains an empty or traversal segment: {key!r}"
        raise StorageUnsafeKeyError(msg)

    # posixpath.normpath collapsing to something different means we were
    # relying on normalisation to stay inside the prefix - reject instead.
    if posixpath.normpath(normalised) != normalised:
        msg = f"object key is not in canonical form: {key!r}"
        raise StorageUnsafeKeyError(msg)

    return key


def build_object_key(
    *,
    prefix: str,
    filename: str,
    owner_id: int | str | None = None,
    unique: str | None = None,
) -> str:
    """Compose a safe key under a fixed prefix.

    Layout ``{prefix}/{owner}/{unique}_{filename}`` keeps objects browsable by
    owner while guaranteeing uniqueness, which matters because overwriting a
    knowledge source file would silently invalidate a READY document's checksum.
    """
    safe_prefix = validate_object_key(prefix.rstrip("/")) if prefix else "objects"
    safe_name = sanitise_filename(filename)
    parts = [safe_prefix]
    if owner_id is not None:
        parts.append(str(owner_id))
    if unique:
        safe_name = f"{sanitise_filename(unique, fallback='v')}_{safe_name}"
    parts.append(safe_name)
    return validate_object_key("/".join(parts))


# ---------------------------------------------------------------------------
# Content validation
# ---------------------------------------------------------------------------
def guess_content_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def sniff_content_type(head: bytes) -> str | None:
    """Best-effort signature detection from the leading bytes."""
    for content_type, prefixes in _SIGNATURES:
        if any(head.startswith(prefix) for prefix in prefixes):
            return content_type
    return None


def validate_upload(
    *,
    filename: str,
    declared_content_type: str,
    head: bytes,
    size: int,
    max_bytes: int,
) -> str:
    """Validate an upload and return the canonical content type.

    Implements the three checks spec §52 step 1 and FG-19 require:

    1. **size limit** - refused before anything is persisted;
    2. **allow-list** - an unknown type is rejected, not stored and parsed later;
    3. **signature agreement** - declared type must not contradict the bytes,
       which defeats the classic "``.pdf`` that is really a zip bomb / script"
       spoof. A zip container is accepted for OOXML types because docx/xlsx
       genuinely are zips.
    """
    if size <= 0:
        msg = "empty upload rejected"
        raise StorageUnsupportedContentTypeError(msg)
    if size > max_bytes:
        msg = f"upload of {size} bytes exceeds the {max_bytes} byte limit"
        raise StorageObjectTooLargeError(msg)

    declared = (declared_content_type or "").split(";")[0].strip().lower()
    guessed = guess_content_type(filename)

    if declared not in ALLOWED_CONTENT_TYPES:
        if guessed in ALLOWED_CONTENT_TYPES:
            declared = guessed
        else:
            msg = f"content type {declared_content_type!r} is not allowed"
            raise StorageUnsupportedContentTypeError(msg)

    sniffed = sniff_content_type(head)
    if sniffed is not None:
        ooxml = declared in {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }
        if sniffed == "application/zip" and ooxml:
            return declared
        if sniffed != declared and not (sniffed == "application/zip" and ooxml):
            msg = (
                f"declared content type {declared!r} contradicts the file signature "
                f"({sniffed!r})"
            )
            raise StorageUnsupportedContentTypeError(msg)

    return declared


def sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_of_stream(stream: BinaryIO, *, chunk_size: int = 1024 * 1024) -> tuple[str, int]:
    """Stream-hash without loading the whole object into memory."""
    digest = hashlib.sha256()
    total = 0
    while chunk := stream.read(chunk_size):
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


# ---------------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------------
@runtime_checkable
class ObjectStorage(Protocol):
    """The storage contract every backend must satisfy.

    All methods are synchronous because both the MinIO SDK and boto3 are
    blocking; the async HTTP layer runs them in a threadpool rather than
    pretending they are non-blocking.
    """

    bucket_product_images: str
    bucket_knowledge: str
    bucket_reports: str
    bucket_evidence: str

    def ensure_buckets(self) -> list[str]:
        """Create missing buckets idempotently; return those created."""
        ...

    def put_object(
        self,
        *,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        """Store bytes and return the persisted facts (key, checksum, type, size)."""
        ...

    def get_object(self, *, bucket: str, key: str) -> bytes:
        """Fetch bytes, raising :class:`StorageObjectNotFoundError` when absent."""
        ...

    def stat_object(self, *, bucket: str, key: str) -> ObjectStat:
        """Metadata without downloading the body, raising :class:`StorageObjectNotFoundError`."""
        ...

    def object_exists(self, *, bucket: str, key: str) -> bool:
        """Existence probe used to enforce INV-017 before marking a document READY."""
        ...

    def presigned_get_url(self, *, bucket: str, key: str, ttl_seconds: int | None = None) -> str:
        """Time-limited read URL. The only way a private object is ever served."""
        ...

    def presigned_put_url(
        self,
        *,
        bucket: str,
        key: str,
        ttl_seconds: int | None = None,
        content_type: str | None = None,
    ) -> str:
        ...

    def delete_object(self, *, bucket: str, key: str) -> None:
        ...

    def verify_checksum(self, *, bucket: str, key: str, expected: str) -> bool:
        """Re-read and confirm the digest. Backs INV-017 and FG-19."""
        ...


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_KEY_LENGTH",
    "ObjectStat",
    "ObjectStorage",
    "StorageBucketUnavailableError",
    "StorageChecksumMismatchError",
    "StorageError",
    "StorageObjectNotFoundError",
    "StorageObjectTooLargeError",
    "StorageUnsafeKeyError",
    "StorageUnsupportedContentTypeError",
    "StoredObject",
    "build_object_key",
    "guess_content_type",
    "sanitise_filename",
    "sha256_hex",
    "sha256_of_stream",
    "sniff_content_type",
    "validate_object_key",
    "validate_upload",
]
