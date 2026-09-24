from __future__ import annotations

import hashlib
import time
from math import log2
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.errors import (
    DocumentNotFoundError,
    DocumentStateInvalidError,
    DocumentUnsupportedTypeError,
    KnowledgeBaseNotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.modules.identity.enums import DataScope, PermissionCode
from app.modules.identity.service import Principal
from app.modules.knowledge.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument, KnowledgeEvaluation
from app.modules.knowledge.repository import KnowledgeRepository
from app.modules.knowledge.schemas import (
    CreateKnowledgeBaseRequest,
    RetrievalDebugRequest,
    RunEvaluationRequest,
)
from app.shared.db.base import utc_now
from app.shared.storage.factory import get_object_storage
from app.shared.storage.port import build_object_key


class KnowledgeService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._repository = KnowledgeRepository(session)

    @staticmethod
    def _chunks(data: bytes, content_type: str) -> list[str]:
        if content_type.split(";", 1)[0].strip() not in {"text/plain", "text/markdown"}:
            raise DocumentUnsupportedTypeError("only UTF-8 plain text and Markdown ingestion is configured")
        try:
            content = data.decode("utf-8-sig").strip()
        except UnicodeDecodeError as exc:
            raise ValidationError("document must contain valid UTF-8 text") from exc
        if not content or "\x00" in content:
            raise ValidationError("document must contain nonempty text")
        return [content[offset:offset + 1000] for offset in range(0, len(content), 900)]

    @staticmethod
    def _merchant(principal: Principal, permission: str) -> int:
        principal.require_permission(permission)
        if principal.merchant_id is None or principal.data_scope not in {DataScope.MERCHANT, DataScope.ALL}:
            raise PermissionDeniedError("knowledge access requires merchant scope")
        return principal.merchant_id

    @staticmethod
    def serialize_base(base: KnowledgeBase, document_count: int = 0) -> dict:
        return {"id": str(base.id), "name": base.name, "description": base.description, "document_count": document_count, "embedding_model": base.embedding_model, "created_at": base.created_at}

    @staticmethod
    def serialize_document(document: KnowledgeDocument) -> dict:
        return {"id": str(document.id), "knowledge_base_id": str(document.knowledge_base_id), "file_name": document.file_name, "content_type": document.content_type, "size_bytes": document.size_bytes, "status": document.status, "chunk_count": document.chunk_count, "error_message": document.error_message, "uploaded_by": str(document.uploaded_by), "created_at": document.created_at, "processed_at": document.processed_at}

    def list_bases(self, *, principal: Principal) -> list[dict]:
        merchant_id = self._merchant(principal, PermissionCode.KNOWLEDGE_READ.value)
        return [self.serialize_base(base, int(document_count)) for base, document_count in self._repository.list_bases(merchant_id=merchant_id)]

    def create_base(self, *, principal: Principal, request: CreateKnowledgeBaseRequest) -> dict:
        merchant_id = self._merchant(principal, PermissionCode.KNOWLEDGE_WRITE.value)
        base = KnowledgeBase(merchant_id=merchant_id, name=request.name, description=request.description, embedding_model=request.embedding_model)
        self._session.add(base)
        self._session.commit()
        return self.serialize_base(base)

    def upload(self, *, principal: Principal, base_id: int, file_name: str, content_type: str, data: bytes) -> dict:
        merchant_id = self._merchant(principal, PermissionCode.KNOWLEDGE_INGEST.value)
        base = self._repository.get_base(base_id=base_id, merchant_id=merchant_id)
        if base is None:
            raise KnowledgeBaseNotFoundError("knowledge base not found")
        if not data:
            raise ValidationError("knowledge document is empty")
        chunks = self._chunks(data, content_type)
        checksum = hashlib.sha256(data).hexdigest()
        object_key = build_object_key(prefix="knowledge", owner_id=merchant_id, unique=uuid4().hex, filename=file_name)
        storage = get_object_storage()
        stored = storage.put_object(bucket=storage.bucket_knowledge, key=object_key, data=data, content_type=content_type)
        document = KnowledgeDocument(merchant_id=merchant_id, knowledge_base_id=base.id, file_name=file_name[:255], object_key=stored.object_key, content_type=content_type, size_bytes=len(data), checksum=checksum, status="READY", chunk_count=len(chunks), uploaded_by=principal.user_id, processed_at=utc_now())
        self._session.add(document)
        self._session.flush()
        for index, content in enumerate(chunks):
            self._session.add(KnowledgeChunk(document_id=document.id, text=content, metadata_json={"chunk_index": index, "checksum": checksum}))
        self._session.commit()
        return self.serialize_document(document)

    def list_documents(self, *, principal: Principal, base_id: int, page: int, page_size: int) -> tuple[list[dict], int]:
        merchant_id = self._merchant(principal, PermissionCode.KNOWLEDGE_READ.value)
        if self._repository.get_base(base_id=base_id, merchant_id=merchant_id) is None:
            raise KnowledgeBaseNotFoundError("knowledge base not found")
        rows, total = self._repository.list_documents(base_id=base_id, merchant_id=merchant_id, page=page, page_size=page_size)
        return [self.serialize_document(row) for row in rows], total

    def transition(self, *, principal: Principal, document_id: int, status: str) -> dict:
        merchant_id = self._merchant(principal, PermissionCode.KNOWLEDGE_WRITE.value)
        document = self._repository.get_document(document_id=document_id, merchant_id=merchant_id, for_update=True)
        if document is None:
            raise DocumentNotFoundError("knowledge document not found")
        if document.status == "ARCHIVED":
            raise DocumentStateInvalidError("archived knowledge document cannot transition")
        if status == "ARCHIVED":
            document.status = status
        elif status == "PROCESSING":
            storage = get_object_storage()
            data = storage.get_object(bucket=storage.bucket_knowledge, key=document.object_key)
            if hashlib.sha256(data).hexdigest() != document.checksum:
                raise ValidationError("stored document checksum changed")
            chunks = self._chunks(data, document.content_type)
            self._session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.document_id == document.id))
            for index, content in enumerate(chunks):
                self._session.add(KnowledgeChunk(document_id=document.id, text=content, metadata_json={"chunk_index": index, "checksum": document.checksum}))
            document.chunk_count = len(chunks)
            document.status = "READY"
            document.error_message = None
            document.processed_at = utc_now()
        else:
            raise DocumentStateInvalidError("unsupported knowledge document transition")
        self._session.commit()
        return self.serialize_document(document)

    def debug_retrieval(self, *, principal: Principal, request: RetrievalDebugRequest) -> dict:
        merchant_id = self._merchant(principal, PermissionCode.KNOWLEDGE_READ.value)
        started = time.perf_counter()
        if self._repository.get_base(base_id=request.knowledge_base_id, merchant_id=merchant_id) is None:
            raise KnowledgeBaseNotFoundError("knowledge base not found")
        hits = self._repository.search_chunks(base_id=request.knowledge_base_id, merchant_id=merchant_id, query=request.query, limit=request.top_k)
        final = [{"index": index, "doc_id": str(document.id), "doc_name": document.file_name, "chunk_id": str(chunk.id), "score": 1.0, "snippet": chunk.text} for index, (chunk, document) in enumerate(hits, 1)]
        stages: list[dict[str, object]] = [{"stage": "sparse", "duration_ms": round((time.perf_counter() - started) * 1000, 2), "candidates": final}]
        return {"rewritten_query": request.query, "stages": stages, "final_evidence": final, "total_duration_ms": round((time.perf_counter() - started) * 1000, 2)}

    @staticmethod
    def serialize_evaluation(row: KnowledgeEvaluation) -> dict:
        return {"id": str(row.id), "knowledge_base_id": str(row.knowledge_base_id),
                "dataset_name": row.dataset_name, "sample_count": row.sample_count,
                "created_at": row.created_at, **row.results["metrics"]}

    def evaluations(self, *, principal: Principal, base_id: int) -> list[dict]:
        merchant_id = self._merchant(principal, PermissionCode.KNOWLEDGE_READ.value)
        if self._repository.get_base(base_id=base_id, merchant_id=merchant_id) is None:
            raise KnowledgeBaseNotFoundError("knowledge base not found")
        return [self.serialize_evaluation(row) for row in self._repository.evaluations(base_id=base_id, merchant_id=merchant_id)]

    def run_evaluation(self, *, principal: Principal, request: RunEvaluationRequest) -> dict:
        merchant_id = self._merchant(principal, PermissionCode.KNOWLEDGE_WRITE.value)
        principal.require_permission(PermissionCode.KNOWLEDGE_READ.value)
        if self._repository.get_base(base_id=request.knowledge_base_id, merchant_id=merchant_id) is None:
            raise KnowledgeBaseNotFoundError("knowledge base not found")
        ready_ids = self._repository.ready_chunk_ids(base_id=request.knowledge_base_id, merchant_id=merchant_id)
        scores: dict[str, float] = {"recall_at_k": 0.0, "precision_at_k": 0.0, "mrr": 0.0, "ndcg": 0.0}
        samples = []
        for sample in request.samples:
            if not sample.relevant_chunk_ids.issubset(ready_ids):
                raise ValidationError("evaluation labels must refer to ready chunks in this knowledge base")
            hits = self._repository.search_chunks(base_id=request.knowledge_base_id, merchant_id=merchant_id, query=sample.query, limit=request.top_k)
            retrieved = [chunk.id for chunk, _ in hits]
            ranks = [rank for rank, chunk_id in enumerate(retrieved, 1) if chunk_id in sample.relevant_chunk_ids]
            scores["recall_at_k"] += len(ranks) / len(sample.relevant_chunk_ids)
            scores["precision_at_k"] += len(ranks) / request.top_k
            scores["mrr"] += 1 / ranks[0] if ranks else 0
            ideal = sum(1 / log2(rank + 1) for rank in range(1, min(request.top_k, len(sample.relevant_chunk_ids)) + 1))
            scores["ndcg"] += sum(1 / log2(rank + 1) for rank in ranks) / ideal
            samples.append({"query": sample.query, "relevant_chunk_ids": sorted(sample.relevant_chunk_ids), "retrieved_chunk_ids": retrieved})
        metrics = {name: score / len(request.samples) for name, score in scores.items()}
        row = KnowledgeEvaluation(knowledge_base_id=request.knowledge_base_id, dataset_name=request.dataset_name,
                                  sample_count=len(samples), results={"metrics": metrics, "top_k": request.top_k, "samples": samples})
        self._session.add(row)
        self._session.commit()
        self._session.refresh(row)
        return self.serialize_evaluation(row)


__all__ = ["KnowledgeService"]
