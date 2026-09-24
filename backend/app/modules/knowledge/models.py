from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db.base import Base, MerchantScopedMixin, PkMixin, TimestampMixin, status_column
from app.shared.db.types import BigIntUnsigned, DateTimeMS


class KnowledgeBase(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("merchant_id", "name", name="uq_knowledge_bases_merchant_name"),)

    merchant_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False, default="text-embedding-3-small")


class KnowledgeDocument(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint("status IN ('UPLOADED','PROCESSING','READY','FAILED','ARCHIVED')", name="status_valid"),
        Index("ix_knowledge_documents_base_status", "knowledge_base_id", "status"),
    )

    merchant_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    knowledge_base_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("knowledge_bases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(status_column(16), nullable=False, default="UPLOADED")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    uploaded_by: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)


class KnowledgeChunk(Base, PkMixin, TimestampMixin):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("ix_knowledge_chunks_document", "document_id"),)

    document_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class KnowledgeEvaluation(Base, PkMixin, TimestampMixin):
    __tablename__ = "knowledge_evaluations"

    knowledge_base_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("knowledge_bases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    dataset_name: Mapped[str] = mapped_column(String(200), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    results: Mapped[dict] = mapped_column(JSON, nullable=False)


__all__ = ["KnowledgeBase", "KnowledgeChunk", "KnowledgeDocument", "KnowledgeEvaluation"]
