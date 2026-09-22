"""FastAPI application factory and lifespan.

Spec references:
    §128 observability is optional and must never be a hard startup dependency;
         the application must boot with only MySQL and Redis reachable.
    §129 schema changes are applied by Alembic, never at boot.
    §130 ``/health/live`` and ``/health/ready``.
    §131 trace context propagates from here.

Startup philosophy: **the API starts whenever MySQL is reachable and never
performs migrations.** A failing Qdrant, MinIO or LLM degrades a feature and is
reported through ``/health/ready``; it does not prevent the shop from serving
traffic. That asymmetry is deliberate - the alternative (hard-fail on any
dependency) turns a vector-database restart into a storefront outage.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from app.api.v1 import health
from app.api.v1.router import api_router, register_module_routers
from app.core.config import Settings, get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    BodySizeLimitMiddleware,
    CorrelationMiddleware,
    SecurityHeadersMiddleware,
)

logger = get_logger(__name__)

DESCRIPTION = """
**Nexora Commerce V1** - AI-Native Commerce Operations Platform.

This API is the transactional backbone of the platform. Two properties are
worth stating explicitly, because they shape every endpoint:

1. **The server is the only price and stock authority.** Client-supplied money
   is never trusted; ``CreateOrder`` accepts business inputs (SKU, quantity,
   coupon, address) and recomputes everything.
2. **AI is never a source of business truth.** Order, payment, refund and
   inventory facts originate here. Agent and MCP traffic reaches these services
   only through an audited tool gateway that re-authorises every call.

Every response uses the envelope ``{code, message, data, trace_id}``. Quote
``trace_id`` in a bug report; it correlates the request across the workflow,
agent, task queue and audit log.
""".strip()

TAGS_METADATA = [
    {"name": "health", "description": "Liveness and readiness probes."},
    {"name": "auth", "description": "Login, refresh rotation, logout, sessions."},
    {"name": "users", "description": "Account and address management."},
    {"name": "catalog", "description": "Products, SKUs, categories, brands, images."},
    {"name": "inventory", "description": "Stock levels, movements and adjustments."},
    {"name": "cart", "description": "Server-authoritative cart pricing preview."},
    {"name": "orders", "description": "Order preview, creation, lifecycle."},
    {"name": "payments", "description": "Payment intent, provider callbacks, mock pay."},
    {"name": "fulfillments", "description": "Packages, shipping and delivery."},
    {"name": "after-sales", "description": "After-sale claims and refunds."},
    {"name": "marketing", "description": "Promotions and coupons."},
    {"name": "analytics", "description": "Read-only business metrics."},
    {"name": "knowledge", "description": "Knowledge bases, ingestion and retrieval."},
    {"name": "agent", "description": "Agent threads, runs and SSE streaming."},
    {"name": "governance", "description": "Pending actions, tool registry, policies."},
    {"name": "audit", "description": "Append-only audit trail."},
]


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialise cross-cutting infrastructure, then release it on shutdown."""
    settings: Settings = app.state.settings

    configure_logging(level=settings.LOG_LEVEL, json_output=settings.LOG_JSON)
    logger.info(
        "starting application",
        app=settings.APP_NAME,
        env=settings.APP_ENV,
        rag_mode=settings.RAG_MODE,
        checkpointer=settings.CHECKPOINTER_BACKEND,
    )

    # -- critical ---------------------------------------------------------
    from app.shared.db.session import configure_database, ping_database

    configure_database(settings)
    healthy, detail = ping_database()
    if not healthy:
        # Fail fast and loudly: serving traffic without MySQL means every
        # request fails anyway, and a clear startup error is easier to act on
        # than a stream of 500s.
        msg = f"cannot start: MySQL is not reachable ({detail})"
        logger.error(msg)
        raise RuntimeError(msg)

    # -- important --------------------------------------------------------
    from app.shared.redis_client import configure_redis, ping_redis

    with contextlib.suppress(Exception):
        configure_redis(settings)
    redis_ok, redis_detail = ping_redis()
    if not redis_ok:
        logger.warning(
            "redis is unavailable; rate limiting and caching are degraded",
            detail=redis_detail,
        )

    # -- degradable -------------------------------------------------------
    from app.shared.storage.factory import get_object_storage

    with contextlib.suppress(Exception):
        get_object_storage(ensure=True)

    if settings.OTEL_ENABLED:
        _configure_tracing(app, settings)

    logger.info("application ready", env=settings.APP_ENV)
    try:
        yield
    finally:
        logger.info("shutting down application")
        from app.shared.db.session import dispose_database as _dispose

        _dispose()


def _configure_tracing(app: FastAPI, settings: Settings) -> None:  # pragma: no cover - optional profile
    """Wire OpenTelemetry when explicitly enabled (§128, §131).

    Guarded by ``OTEL_ENABLED`` so the observability profile can stay off; an
    exporter that cannot be reached must not stop the application from booting.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({SERVICE_NAME: settings.OTEL_SERVICE_NAME})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{settings.OTEL_EXPORTER_OTLP_ENDPOINT}/v1/traces"))
        )
        trace.set_tracer_provider(provider)

        FastAPIInstrumentor.instrument_app(app)
        HTTPXClientInstrumentor().instrument()
        from app.shared.db.session import get_engine

        SQLAlchemyInstrumentor().instrument(engine=get_engine())
        logger.info("tracing enabled", endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT)
    except Exception as exc:
        logger.warning("tracing could not be enabled; continuing without it", error=type(exc).__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()

    app = FastAPI(
        title="Nexora Commerce API",
        description=DESCRIPTION,
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
    )
    app.state.settings = resolved

    # Middleware order matters: the outermost layer must bind the correlation
    # context so that everything inner (including error handlers) can log with
    # the right trace_id. Starlette applies them in reverse registration order.
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=resolved.S3_MAX_UPLOAD_BYTES,
        # Multipart uploads declare their own per-part limits in the handler, so
        # the transport-level guard is deliberately generous for them.
        exempt_paths=frozenset({"/api/v1/knowledge/documents", "/api/v1/catalog/images"}),
    )
    app.add_middleware(CorrelationMiddleware)

    if resolved.CORS_ALLOW_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.CORS_ALLOW_ORIGINS,
            allow_credentials=True,  # required for the HttpOnly refresh cookie (§23)
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["X-Trace-Id", "X-Request-Id"],
            max_age=600,
        )

    register_exception_handlers(app)

    # Infra probes at the frozen paths of §130 ...
    app.include_router(health.router, prefix="/health", tags=["health"])
    # ... and the versioned API surface.
    register_module_routers()
    app.include_router(api_router, prefix=resolved.API_V1_PREFIX)

    _tighten_openapi(app)
    return app


def _tighten_openapi(app: FastAPI) -> None:
    """Add envelope documentation and a bearer security scheme to the schema."""

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
            tags=TAGS_METADATA,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {}).update(
            {
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                    "description": "Short-lived access token (spec §23).",
                },
                "IdempotencyKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "Idempotency-Key",
                    "description": (
                        "Required on order creation. Replaying the same key with the same "
                        "body returns the original result instead of creating a duplicate."
                    ),
                },
            }
        )
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


# Module-level app for `uvicorn app.main:app`.
app = create_app()


__all__ = ["app", "create_app", "lifespan"]
