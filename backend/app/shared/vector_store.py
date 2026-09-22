"""Qdrant vector store client and collection conventions.

Spec references:
    §11  embedding dimension must come from configuration or model metadata and
         must never be hardcoded in business code.
    §53  archiving a document must be reflected in the Qdrant payload so the
         active filter excludes it.
    §55  the payload carries the full RAG metadata field set.
    §60  Qdrant being unavailable must degrade RAG, not commerce.
    §130 Qdrant is *degradable*: the readiness probe reports it but does not
         fail on it.

Why the payload schema lives here rather than in the knowledge module: the
payload is the *retrieval contract*. If the writer and the filter disagree about
a field name or type, an archived document silently stays retrievable, which is
exactly the class of bug §53 exists to prevent. Keeping one definition used by
both sides removes that failure mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.config import Settings, get_settings
from app.core.errors import EmbeddingDimensionMismatchError
from app.core.logging import get_logger

logger = get_logger(__name__)

_client: Any | None = None


class VectorDistance(StrEnum):
    COSINE = "Cosine"
    DOT = "Dot"
    EUCLID = "Euclid"


#: Qdrant payload keys. Centralised so the writer (ingestion) and the reader
#: (retrieval filter) cannot drift apart.
class PayloadKey(StrEnum):
    CHUNK_ID = "chunk_id"
    DOCUMENT_ID = "document_id"
    KNOWLEDGE_BASE_ID = "knowledge_base_id"
    MERCHANT_ID = "merchant_id"
    DOCUMENT_TYPE = "document_type"
    TITLE = "title"
    SECTION = "section"
    PAGE_NUMBER = "page_number"
    PRODUCT_ID = "product_id"
    BRAND_ID = "brand_id"
    CATEGORY_ID = "category_id"
    VERSION = "version"
    LANGUAGE = "language"
    VALID_FROM = "valid_from"
    VALID_TO = "valid_to"
    CHECKSUM = "checksum"
    SOURCE_URI = "source_uri"
    #: ``UPLOADED`` | ``PROCESSING`` | ``READY`` | ``FAILED`` | ``ARCHIVED``.
    #: Archived documents must be excluded by an active filter (§53).
    DOCUMENT_STATUS = "document_status"
    #: ``PUBLIC`` | ``CUSTOMER`` | ``STAFF`` | ``ADMIN`` (§59).
    VISIBILITY = "visibility"


#: Payload fields that must be indexed for filtering to be cheap. Qdrant can
#: filter on unindexed fields too, but only by scanning, which turns a permission
#: filter into a latency problem at scale.
INDEXED_PAYLOAD_FIELDS: tuple[tuple[PayloadKey, str], ...] = (
    (PayloadKey.KNOWLEDGE_BASE_ID, "integer"),
    (PayloadKey.MERCHANT_ID, "integer"),
    (PayloadKey.DOCUMENT_ID, "integer"),
    (PayloadKey.DOCUMENT_TYPE, "keyword"),
    (PayloadKey.VISIBILITY, "keyword"),
    (PayloadKey.DOCUMENT_STATUS, "keyword"),
    (PayloadKey.LANGUAGE, "keyword"),
    (PayloadKey.PRODUCT_ID, "integer"),
    (PayloadKey.BRAND_ID, "integer"),
    (PayloadKey.CATEGORY_ID, "integer"),
)


@dataclass(frozen=True, slots=True)
class CollectionSpec:
    """A named vector collection together with its dense/sparse configuration."""

    name: str
    dense_size: int
    distance: VectorDistance = VectorDistance.COSINE
    #: Sparse (BM25/SPLADE-style) vectors power the lexical half of hybrid
    #: retrieval (§56). Enabled in ``full`` mode.
    sparse_enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def dense_vector_name(self) -> str:
        return "dense"

    @property
    def sparse_vector_name(self) -> str:
        return "sparse"


def collection_name(settings: Settings, suffix: str) -> str:
    """Deterministic, namespaced collection name."""
    return f"{settings.QDRANT_COLLECTION_PREFIX}_{suffix}"


def knowledge_collection_name(settings: Settings | None = None) -> str:
    resolved = settings or get_settings()
    return collection_name(resolved, "knowledge_chunks")


def configure_vector_store(settings: Settings | None = None) -> Any:
    """Create (or return) the shared Qdrant client.

    Imported lazily so that a deployment without the optional vector stack can
    still import the application and serve commerce traffic (§60).
    """
    global _client
    if _client is not None:
        return _client

    from qdrant_client import QdrantClient

    resolved = settings or get_settings()
    api_key = resolved.QDRANT_API_KEY.get_secret_value() or None
    _client = QdrantClient(
        url=resolved.QDRANT_URL,
        api_key=api_key,
        timeout=int(resolved.QDRANT_TIMEOUT_SECONDS),
        prefer_grpc=False,
    )
    logger.info("vector store configured", url=resolved.QDRANT_URL, mode=resolved.RAG_MODE)
    return _client


def get_vector_store() -> Any:
    if _client is None:
        return configure_vector_store()
    return _client


def set_vector_store(client: Any | None) -> None:
    """Override the client. Used by tests and by the in-memory RAG evaluator."""
    global _client
    _client = client


def ping_qdrant() -> tuple[bool, str]:
    """Probe used by ``/health/ready``. Degradable, never critical (§130)."""
    try:
        client = get_vector_store()
        client.get_collections()
        return True, "ok"
    except Exception as exc:
        return False, type(exc).__name__


def resolve_embedding_dimension(settings: Settings, *, provider_dimension: int | None) -> int:
    """Decide the vector size, refusing to guess (§11).

    Order of precedence:
      1. an explicitly configured ``EMBEDDING_DIMENSION``;
      2. the dimension reported by the embedding provider's metadata;
      3. otherwise fail loudly.

    A mismatch between the configured value and the provider is an error rather
    than a silent truncation: a truncated embedding produces results that look
    plausible and are wrong, which is the worst possible failure for retrieval.
    """
    configured = settings.EMBEDDING_DIMENSION

    if configured and provider_dimension and configured != provider_dimension:
        raise EmbeddingDimensionMismatchError(
            f"EMBEDDING_DIMENSION={configured} does not match the provider's "
            f"reported dimension {provider_dimension}",
            context={"configured": configured, "provider": provider_dimension},
        )

    resolved = configured or provider_dimension
    if not resolved:
        raise EmbeddingDimensionMismatchError(
            "embedding dimension is unknown: set EMBEDDING_DIMENSION or use a "
            "provider that reports its dimension"
        )
    return int(resolved)


__all__ = [
    "INDEXED_PAYLOAD_FIELDS",
    "CollectionSpec",
    "PayloadKey",
    "VectorDistance",
    "collection_name",
    "configure_vector_store",
    "get_vector_store",
    "knowledge_collection_name",
    "ping_qdrant",
    "resolve_embedding_dimension",
    "set_vector_store",
]
