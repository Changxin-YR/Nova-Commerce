"""ASGI middleware: correlation, request logging, body limits, security headers.

Spec references:
    §88  origin/host protection and request-size limits for the MCP surface;
         the same treatment is applied to the REST surface.
    §109 security headers, oversized-upload rejection.
    §131 ``trace_id`` must thread through HTTP -> workflow -> agent -> tool ->
         Celery -> MCP -> audit. The middleware here is where a request's
         correlation identity is born.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.context import (
    REQUEST_ID_HEADER,
    TRACE_ID_HEADER,
    RequestContext,
    new_request_context,
    reset_context,
    set_context,
)
from app.core.errors import ErrorCode, envelope
from app.core.logging import get_logger

logger = get_logger(__name__)

Message = dict[str, object]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]

#: Paths excluded from access logging so the log stream stays readable.
_QUIET_PATHS = frozenset({"/health/live", "/health/ready", "/metrics", "/favicon.ico"})


class CorrelationMiddleware(BaseHTTPMiddleware):
    """Bind a trusted :class:`RequestContext` for the duration of the request.

    The inbound ``X-Trace-Id`` is *validated*, not echoed (see
    :func:`~app.core.context.normalise_trace_id`): an attacker-supplied header
    must not be able to inject newlines into log records or grow without bound.
    A caller that supplies a sane id keeps end-to-end correlation across
    services, which is the point.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        ctx = new_request_context(
            trace_id=request.headers.get(TRACE_ID_HEADER),
            source="HTTP",
        )
        token = set_context(ctx)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "request failed with an unhandled exception",
                method=request.method,
                path=request.url.path,
                duration_ms=round(elapsed_ms, 2),
            )
            raise
        finally:
            reset_context(token)

        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers[TRACE_ID_HEADER] = ctx.trace_id
        response.headers[REQUEST_ID_HEADER] = ctx.request_id

        if request.url.path not in _QUIET_PATHS:
            logger.info(
                "request completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round(elapsed_ms, 2),
                # query strings are deliberately omitted: they routinely carry
                # tokens, emails and search terms (§94, §132)
            )

        # Expose the trace id to browser JS so a support ticket can quote it.
        response.headers.setdefault("Access-Control-Expose-Headers", TRACE_ID_HEADER)
        return response


class BodySizeLimitMiddleware:
    """Reject oversized request bodies before they are buffered (§109, §122).

    A pure ASGI middleware rather than a ``BaseHTTPMiddleware`` so the check
    happens on the ``Content-Length`` header and on the streamed chunks, without
    first materialising the body in memory - which is the whole point of having
    a limit.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int, exempt_paths: frozenset[str] = frozenset()) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.exempt_paths = exempt_paths

    async def __call__(self, scope: Message, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        if path in self.exempt_paths:
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                declared = int(raw_length)
            except (TypeError, ValueError):
                declared = 0
            if declared > self.max_bytes:
                await self._reject(send, declared)
                return

        received = 0
        exceeded = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                received += len(body) if isinstance(body, bytes | bytearray) else 0
                if received > self.max_bytes:
                    exceeded = True
            return message

        # A chunked upload can lie about (or omit) Content-Length, so the
        # streaming counter above is the real enforcement point.
        sender = send

        async def guarded_send(message: Message) -> None:
            if exceeded and message.get("type") == "http.response.start":
                await self._reject(sender, received)
                return
            await sender(message)

        await self.app(scope, limited_receive, guarded_send)

    async def _reject(self, send: Send, size: int) -> None:
        payload = envelope(
            data={"limit_bytes": self.max_bytes, "received_bytes": size},
            code=ErrorCode.PAYLOAD_TOO_LARGE,
            message="Request body is too large",
        )
        import json

        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach conservative security headers to every response (spec §109).

    The CSP is intentionally strict and does not permit inline scripts, because
    the API surface serves JSON only; a strict policy costs nothing here and
    mitigates XSS if a response is ever rendered directly.
    """

    _CSP = (
        "default-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'none'"
    )

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
        )
        # Only meaningful for responses a browser might render directly.
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            response.headers.setdefault("Content-Security-Policy", self._CSP)
        return response


def current_context() -> RequestContext:
    """Convenience re-export for handlers that need the whole context."""
    from app.core.context import get_context

    return get_context()


__all__ = [
    "BodySizeLimitMiddleware",
    "CorrelationMiddleware",
    "SecurityHeadersMiddleware",
    "current_context",
]
