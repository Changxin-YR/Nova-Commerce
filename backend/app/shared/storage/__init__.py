"""Object storage: one port, pluggable backends (spec section 20).

Business modules import from here and never from ``backends``, which keeps the
vendor SDK out of the domain layer (enforced by an architecture test).
"""

from app.shared.storage.factory import (
    build_object_storage,
    get_object_storage,
    set_object_storage,
    storage_health,
)
from app.shared.storage.port import (
    ALLOWED_CONTENT_TYPES,
    ObjectStat,
    ObjectStorage,
    StorageBucketUnavailableError,
    StorageChecksumMismatchError,
    StorageError,
    StorageObjectNotFoundError,
    StorageObjectTooLargeError,
    StorageUnsafeKeyError,
    StorageUnsupportedContentTypeError,
    StoredObject,
    build_object_key,
    sanitise_filename,
    sha256_hex,
    validate_object_key,
    validate_upload,
)

__all__ = [
    "ALLOWED_CONTENT_TYPES",
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
    "build_object_storage",
    "get_object_storage",
    "sanitise_filename",
    "set_object_storage",
    "sha256_hex",
    "storage_health",
    "validate_object_key",
    "validate_upload",
]
