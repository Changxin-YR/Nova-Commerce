from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.knowledge.models import KnowledgeBase, KnowledgeChunk, KnowledgeDocument, KnowledgeEvaluation


class KnowledgeRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_bases(self, *, merchant_id: int) -> list[tuple[KnowledgeBase, int]]:
        rows = self._session.execute(
                select(KnowledgeBase, func.count(KnowledgeDocument.id))
                .outerjoin(KnowledgeDocument, KnowledgeDocument.knowledge_base_id == KnowledgeBase.id)
                .where(KnowledgeBase.merchant_id == merchant_id)
                .group_by(KnowledgeBase.id)
                .order_by(KnowledgeBase.created_at.desc())
            ).all()
        return [(base, int(count)) for base, count in rows]

    def get_base(self, *, base_id: int, merchant_id: int) -> KnowledgeBase | None:
        return self._session.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == base_id, KnowledgeBase.merchant_id == merchant_id)
        ).scalar_one_or_none()

    def evaluations(self, *, base_id: int, merchant_id: int) -> list[KnowledgeEvaluation]:
        return list(self._session.scalars(
            select(KnowledgeEvaluation).join(KnowledgeBase)
            .where(KnowledgeBase.id == base_id, KnowledgeBase.merchant_id == merchant_id)
            .order_by(KnowledgeEvaluation.id.desc()).limit(100)
        ))

    def ready_chunk_ids(self, *, base_id: int, merchant_id: int) -> set[int]:
        return set(self._session.scalars(
            select(KnowledgeChunk.id).join(KnowledgeDocument)
            .where(KnowledgeDocument.knowledge_base_id == base_id,
                   KnowledgeDocument.merchant_id == merchant_id,
                   KnowledgeDocument.status == "READY")
        ))

    def list_documents(self, *, base_id: int, merchant_id: int, page: int, page_size: int) -> tuple[list[KnowledgeDocument], int]:
        stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.knowledge_base_id == base_id,
            KnowledgeDocument.merchant_id == merchant_id,
        )
        total = int(self._session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
        rows = list(
            self._session.execute(
                stmt.order_by(KnowledgeDocument.created_at.desc(), KnowledgeDocument.id.desc())
                .limit(page_size).offset((page - 1) * page_size)
            ).scalars()
        )
        return rows, total

    def get_document(self, *, document_id: int, merchant_id: int, for_update: bool = False) -> KnowledgeDocument | None:
        stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.id == document_id, KnowledgeDocument.merchant_id == merchant_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self._session.execute(stmt).scalar_one_or_none()

    def search_chunks(self, *, base_id: int, merchant_id: int, query: str, limit: int) -> list[tuple[KnowledgeChunk, KnowledgeDocument]]:
        stmt = (
            select(KnowledgeChunk, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeChunk.document_id)
            .where(
                KnowledgeDocument.knowledge_base_id == base_id,
                KnowledgeDocument.merchant_id == merchant_id,
                KnowledgeDocument.status == "READY",
                KnowledgeChunk.text.contains(query, autoescape=True),
            )
            .order_by(KnowledgeChunk.id)
            .limit(limit)
        )
        return [(row[0], row[1]) for row in self._session.execute(stmt).all()]


__all__ = ["KnowledgeRepository"]
