"""ASGI entry point for the MCP resource server (spec section 86).

Runnable as ``uvicorn app.mcp.app:app``. It is deliberately **not** mounted into
``app.main``: the MCP surface is a separate resource server with its own audience,
its own bearer middleware and its own host/origin allowlist, and mounting it under
the REST app would put two different authentication models on one Starlette
instance - where the first one to run wins and the second silently does nothing.

``app`` is built at import time because that is what an ASGI server requires, and
:func:`create_mcp_app` stays public so a test can build an instance whose settings
differ from the process environment without mutating it.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from fnmatch import fnmatchcase
from typing import Any

from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

from app.mcp.config import MCPSettings
from app.mcp.server import build_mcp_server, transport_security

#: The MCP path. Both the streamable-HTTP route and the RFC 8707 resource
#: indicator have to agree on it, so it is stated once in settings.
DEFAULT_PATH = "/mcp"


class _HostGuardASGI:
    """Return the rebinding response before the MCP transport can proxy an error."""

    def __init__(self, app: ASGIApp, allowed_hosts: list[str]) -> None:
        self.app = app
        self._allowed_hosts = tuple(allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            host = headers.get(b"host", b"").decode("latin-1")
            if not any(fnmatchcase(host, pattern) for pattern in self._allowed_hosts):
                body = b'{"detail":"Host is not allowed"}'
                await send({"type": "http.response.start", "status": 421, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        response_status: int | None = None

        async def rewrite(message: MutableMapping[str, Any]) -> None:
            nonlocal response_status
            if message.get("type") == "http.response.start":
                status = message.get("status", 0)
                response_status = status if isinstance(status, int) else 0
                if response_status == 502:
                    message = {**message, "status": 421}
            elif message.get("type") == "http.response.body" and response_status == 502:
                message = {**message, "body": b'{"detail":"Host is not allowed"}', "more_body": False}
            await send(message)

        await self.app(scope, receive, rewrite)


def create_mcp_app(settings: MCPSettings | None = None) -> Starlette:
    """Build the Streamable-HTTP ASGI application.

    The transport hardening comes from :func:`app.mcp.server.transport_security`
    rather than from the SDK's localhost defaults, so the allowlists are the
    deployment's own values. That matters for the negative case: with SDK defaults
    an arbitrary Host header is still refused, so a test cannot tell a *configured*
    allowlist from an inherited one.
    """
    resolved = settings or MCPSettings.load()
    server = build_mcp_server(resolved)
    app: Starlette = server.streamable_http_app(
        streamable_http_path=resolved.http_path,
        max_request_body_size=resolved.max_request_body_size,
        transport_security=transport_security(resolved),
        host="127.0.0.1",
    )
    return _HostGuardASGI(app, list(resolved.allowed_hosts))  # type: ignore[return-value]


#: Module-level application object for ``uvicorn app.mcp.app:app``.
app = create_mcp_app()

__all__ = ["DEFAULT_PATH", "app", "create_mcp_app"]
