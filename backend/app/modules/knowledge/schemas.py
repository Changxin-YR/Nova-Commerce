from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CreateKnowledgeBaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=1000)
    embedding_model: str = Field(default="text-embedding-3-small", max_length=128)


class RetrievalDebugRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    knowledge_base_id: int
    top_k: int = Field(default=5, ge=1, le=20)
    use_rerank: bool = True
    filters: dict[str, str | int | float | bool] = Field(default_factory=dict)


class KnowledgeBaseOut(BaseModel):
    id: str
    name: str
    description: str | None
    document_count: int
    embedding_model: str
    created_at: datetime


class KnowledgeDocumentOut(BaseModel):
    id: str
    knowledge_base_id: str
    file_name: str
    content_type: str
    size_bytes: int
    status: str
    chunk_count: int
    error_message: str | None
    uploaded_by: str
    created_at: datetime
    processed_at: datetime | None


class EvaluationSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    relevant_chunk_ids: set[int] = Field(min_length=1, max_length=100)


class RunEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    knowledge_base_id: int = Field(gt=0)
    dataset_name: str = Field(min_length=1, max_length=200)
    top_k: int = Field(default=5, ge=1, le=20)
    samples: list[EvaluationSample] = Field(min_length=1, max_length=500)


__all__ = ["CreateKnowledgeBaseRequest", "KnowledgeBaseOut", "KnowledgeDocumentOut", "RetrievalDebugRequest"]
