"""Health endpoints.

Spec §130 defines a deliberate dependency hierarchy, and the point of the
hierarchy is that a *degraded* dependency must not take the commerce system
down with it:

    Critical      MySQL      - without it nothing can be served -> 503
    Important     Redis      - features degrade -> 200 with status=degraded
    Degradable    Qdrant     - RAG unavailable, commerce unaffected
                  LLM        - agent chat unavailable, commerce unaffected
                  Reranker   - retrieval quality drops
                  MCP        - external integration unavailable
                  Storage    - image/knowledge upload unavailable

A single endpoint that returns 503 whenever *anything* is unhealthy is a common
mistake: it turns "the vector database is restarting" into "the shop is down".
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.errors import ApiEnvelope, ErrorCode, envelope
from app.core.logging import get_logger
from app.shared.db.session import ping_database
from app.shared.redis_client import ping_redis
from app.shared.storage.factory import storage_health

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

Criticality = Literal["critical", "important", "degradable"]
CheckStatus = Literal["up", "down", "skipped"]


class DependencyCheck(BaseModel):
    name: str
    criticality: Criticality
    status: CheckStatus
    detail: str = ""
    duration_ms: float = 0.0


class HealthReport(BaseModel):
    status: Literal["ok", "degraded", "unhealthy"]
    service: str
    version: str
    environment: str
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    checks: list[DependencyCheck] = Field(default_factory=list)


def _timed(name: str, criticality: Criticality, probe: Any) -> DependencyCheck:
    import time

    started = time.perf_counter()
    try:
        healthy, detail = probe()
    except Exception as exc:  # a probe must never take the health endpoint down
        healthy, detail = False, type(exc).__name__
    elapsed_ms = (time.perf_counter() - started) * 1000
    return DependencyCheck(
        name=name,
        criticality=criticality,
        status="up" if healthy else "down",
        detail=detail,
        duration_ms=round(elapsed_ms, 2),
    )


def _probe_qdrant() -> tuple[bool, str]:
    from app.shared.vector_store import ping_qdrant

    return ping_qdrant()


def _probe_llm() -> tuple[bool, str]:
    """Agent capability probe.

    Deliberately does **not** call the provider: a health check that burns a
    paid API request on every poll is a self-inflicted outage. It verifies the
    provider is configured and constructible, which is what readiness means.
    """
    settings = get_settings()
    if settings.AI_USE_FAKE_PROVIDERS:
        return True, "fake-provider"
    if not settings.LLM_PROVIDER or settings.LLM_PROVIDER == "none":
        return False, "not-configured"
    if settings.LLM_PROVIDER != "ollama" and not settings.LLM_API_KEY.get_secret_value():
        return False, "missing-api-key"
    return True, "configured"


def _probe_reranker() -> tuple[bool, str]:
    settings = get_settings()
    if settings.RERANKER_PROVIDER in {"", "none"}:
        return False, "disabled"
    return True, "configured"


@router.get(
    "/live",
    summary="Liveness probe",
    response_model=ApiEnvelope,
    description="Returns 200 whenever the process can serve HTTP. Never touches dependencies.",
)
def live() -> dict[str, Any]:
    settings = get_settings()
    return envelope(
        data={
            "status": "ok",
            "service": settings.APP_NAME,
            "version": "1.0.0",
            "environment": settings.APP_ENV,
        }
    )


@router.get(
    "/ready",
    summary="Readiness probe",
    response_model=ApiEnvelope,
    responses={503: {"description": "A critical dependency (MySQL) is unavailable"}},
    description=(
        "Critical dependencies gate the HTTP status. Important and degradable "
        "dependencies are reported but do not fail the probe (spec §130)."
    ),
)
def ready(response: Response) -> dict[str, Any]:
    settings = get_settings()

    checks: list[DependencyCheck] = [
        _timed("mysql", "critical", ping_database),
        _timed("redis", "important", ping_redis),
        _timed("object_storage", "degradable", storage_health),
        _timed("qdrant", "degradable", _probe_qdrant),
        _timed("llm", "degradable", _probe_llm),
        _timed("reranker", "degradable", _probe_reranker),
    ]

    critical_down = [c for c in checks if c.criticality == "critical" and c.status == "down"]
    important_down = [c for c in checks if c.criticality == "important" and c.status == "down"]
    degradable_down = [c for c in checks if c.criticality == "degradable" and c.status != "up"]

    if critical_down:
        overall: Literal["ok", "degraded", "unhealthy"] = "unhealthy"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif important_down or degradable_down:
        overall = "degraded"
    else:
        overall = "ok"

    report = HealthReport(
        status=overall,
        service=settings.APP_NAME,
        version="1.0.0",
        environment=settings.APP_ENV,
        checks=checks,
    )
    if overall == "unhealthy":
        logger.error("readiness probe failed", down=[c.name for c in critical_down])

    payload = envelope(
        data=report.model_dump(mode="json"),
        code=ErrorCode.OK if overall != "unhealthy" else ErrorCode.DEPENDENCY_UNAVAILABLE,
        message="healthy" if overall == "ok" else overall,
    )
    response.headers["X-Nova-Health"] = overall
    return payload


__all__ = ["router"]
