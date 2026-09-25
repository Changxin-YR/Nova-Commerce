"""Audit read/response schemas.

The audit console returns a *frozen* shape: ids are strings (they are big integers on
the wire and JSON numbers lose precision above 2^53), an absent actor renders as
``"system"`` rather than null, and ``trace_id`` is a string rather than optional. Those
are contract decisions, so they live here rather than inline in the router - the router
now maps each row through :func:`audit_record_data` and wraps the page.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.shared.db.models.audit import AuditRecord


class AuditRecordOut(BaseModel):
    """One audit record on the wire.

    Modelled rather than hand-built so the contract is declared once: ``id`` is a
    string because a big-integer id loses precision in a JavaScript client above
    2^53, and ``trace_id`` is non-null because a record without a correlation id still
    needs to render (empty) rather than break a client's template.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    actor_id: str
    actor_type: str
    action: str
    resource_type: str
    resource_id: str
    trace_id: str
    before_snapshot: dict | None = None
    after_snapshot: dict | None = None
    result: str
    created_at: object


class AuditPageMeta(BaseModel):
    """The section-3 page envelope for the audit list."""

    model_config = ConfigDict(extra="forbid")

    page: int
    page_size: int
    total: int
    total_pages: int


def audit_record_data(row: AuditRecord) -> dict:
    """One audit record as the console renders it.

    ``actor_id`` falls back to ``"system"`` because an audit row written by a scheduled
    job has no human actor, and rendering that as ``null`` invites a UI that shows
    "unknown" - which reads as missing evidence rather than as an automated change
    (``audit.actor_type`` already says which it was).
    """
    out = AuditRecordOut(
        id=str(row.id),
        actor_id=str(row.actor_id) if row.actor_id is not None else "system",
        actor_type=row.actor_type,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        trace_id=row.trace_id or "",
        before_snapshot=row.before_snapshot,
        after_snapshot=row.after_snapshot,
        result=row.result,
        created_at=row.created_at,
    )
    # ``mode="json"`` so the timestamp renders exactly as it did before this projection
    # was modelled; ``model_dump()`` would leave a datetime object in the envelope and
    # let the framework decide the format instead.
    return out.model_dump(mode="json")


def audit_page_meta(*, page: int, page_size: int, total: int) -> dict:
    """Page metadata with ceil division, and 0 pages when there is nothing."""
    return AuditPageMeta(
        page=page,
        page_size=page_size,
        total=total,
        total_pages=(total + page_size - 1) // page_size if total else 0,
    ).model_dump()


__all__ = ["AuditPageMeta", "AuditRecordOut", "audit_page_meta", "audit_record_data"]
