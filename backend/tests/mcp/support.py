"""Test helpers for the MCP gate: real tokens, real principals, a real HTTP server.

Nothing here fakes authorization. :func:`mint_token` produces a genuinely signed
JWT that the *production* verifier accepts, :func:`principal_for` builds the
platform's own :class:`~app.modules.identity.service.Principal` from claims, and the
HTTP fixture runs the real Starlette application under a real uvicorn server so the
bearer middleware, the transport-security middleware and the JSON-RPC layer all
execute.

## Why the token builder is shared rather than duplicated per module

A second token builder is a second definition of what a valid token looks like, and
the failure it produces is the worst kind: the suite would pass against tokens the
production verifier would reject, or - worse - accept tokens it should refuse. Every
test that needs a token goes through this one function, which builds the payload
from the same claim names :mod:`app.mcp.auth` reads.

## Why HS256 here

``MCPSettings`` supports HS256 for tests and RS*/ES* for production (the brief says
so explicitly). A test key is generated per session rather than committed, because a
committed test secret is a secret-shaped constant that eventually gets copied into
a real deployment.
"""

from __future__ import annotations

import base64
import secrets
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from app.mcp.auth import PRINCIPAL_CLAIM, build_principal
from app.mcp.config import MCPSettings
from app.modules.identity.enums import DataScope, PermissionCode, UserType
from app.modules.identity.service import Principal

#: Issuer and resource indicator used by every test. Kept off the defaults so a test
#: that accidentally relies on a production default fails instead of passing.
TEST_ISSUER = "http://127.0.0.1:18080/realms/nova"
TEST_RESOURCE = "http://127.0.0.1:8020/mcp"

#: The signing key for this test session. Generated, never committed.
#: 48 random bytes, encoded: comfortably above RFC 7518's 32-byte floor for
#: HS256. A shorter key would make PyJWT warn (InsecureKeyLengthWarning) and, more
#: to the point, would not be the shape a real deployment uses.
TEST_HMAC_SECRET = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii")


def settings_for_tests(**overrides: Any) -> MCPSettings:
    """The resource server's view of the canonical settings, wired for tests.

    ## Why this builds ``app.core.config.Settings`` and projects it

    ``MCPSettings`` no longer parses the environment; it is a projection of the canonical
    model (see ``app/mcp/config.py``). Tests therefore configure the *canonical* model
    and project it, which is the same path production takes - so a field this suite
    sets cannot be one that production never reads.

    The override names stay lowercase because they are the projection's names. Each is
    mapped below to its canonical ``MCP_*`` field, explicitly, so a rename on either side
    breaks here rather than silently becoming a no-op - and ``MCPSettings.load(**kwargs)``
    passes them to ``Settings`` as init arguments, which pydantic-settings prefers over
    the environment, so a test never mutates ``os.environ`` or the cached process
    settings.

    ``required_scopes`` defaults to the scope every tool needs, so a test that wants to
    prove a *missing* scope must remove it explicitly - a default that already omitted it
    would make several tests pass for the wrong reason.
    """
    values: dict[str, Any] = {
        "issuer_url": TEST_ISSUER,
        "resource_server_url": TEST_RESOURCE,
        "hmac_secret": TEST_HMAC_SECRET,
        "algorithm": "HS256",
        "required_scopes": ["nova.read"],
        "write_scope": "nova.write",
        "allowed_hosts": ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*"],
        "allowed_origins": ["http://localhost:*", "http://127.0.0.1:*"],
        "enabled": True,
    }
    values.update(overrides)

    core_kwargs: dict[str, Any] = {
        "MCP_ISSUER_URL": values["issuer_url"],
        "MCP_RESOURCE_SERVER_URL": values["resource_server_url"],
        "MCP_ALGORITHM": values["algorithm"],
        "MCP_REQUIRED_SCOPES": values["required_scopes"],
        "MCP_WRITE_SCOPE": values["write_scope"],
        "MCP_ALLOWED_HOSTS": values["allowed_hosts"],
        "MCP_ALLOWED_ORIGINS": values["allowed_origins"],
        "MCP_ENABLED": values["enabled"],
    }
    if values["hmac_secret"] is not None:
        core_kwargs["MCP_HMAC_SECRET"] = values["hmac_secret"]
    # ``hmac_secret=None`` therefore means "leave the canonical key unset", which is the
    # semantics the verifier's fail-closed test needs: with no key material at all the
    # verifier must refuse every token, and the canonical field is a ``SecretStr`` whose
    # unset value is the empty string rather than None. Omitting the keyword (rather than
    # passing None or "") is what keeps the projection's ``has_key_material`` false -
    # passing "" would look configured.
    return MCPSettings.load(**core_kwargs)


def all_tool_scopes() -> tuple[str, ...]:
    """Every scope the frozen surface can require, plus the write scope.

    Tests that assert on the *whole* visible surface need a token whose grant is not
    the reason anything is hidden - otherwise "merchant scope sees 14 tools" fails for
    a scope reason and looks like a permission bug. Tests that are about scopes pass
    their own tuple explicitly instead.
    """
    from app.mcp.policy import TOOL_SPECS

    return tuple(sorted({spec.scope for spec in TOOL_SPECS} | {"nova.read", "nova.write"}))


def mint_token(
    *,
    settings: MCPSettings | None = None,
    user_id: int = 7001,
    merchant_id: int | None = 31,
    user_type: str = UserType.STAFF.value,
    roles: tuple[str, ...] = ("OPERATOR",),
    permissions: frozenset[str] | None = None,
    data_scope: str = DataScope.MERCHANT.value,
    scopes: tuple[str, ...] = ("nova.read",),
    session_id: str = "sess-mcp-test",
    issuer: str | None = None,
    audience: str | None = None,
    expires_in: int = 300,
    not_before_offset: int = -5,
    key: str | None = None,
    algorithm: str = "HS256",
    include_scope_claim: bool = True,
    include_data_scope: bool = True,
) -> str:
    """Mint a genuinely signed access token.

    The parameters exist so a test can produce a *specific* invalid token (wrong
    issuer, wrong audience, expired, tampered signature) without inventing its own
    payload - the point being that the only difference between the valid and invalid
    case is the one under test.
    """
    resolved = settings or settings_for_tests()
    now = datetime.now(UTC)
    granted = permissions if permissions is not None else frozenset(p.value for p in PermissionCode)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "sid": session_id,
        "jti": secrets.token_urlsafe(12),
        "iss": issuer or resolved.issuer_url,
        "aud": audience or resolved.resource_server_url,
        "iat": int(now.timestamp()),
        "nbf": int((now + timedelta(seconds=not_before_offset)).timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        "roles": list(roles),
        "permissions": sorted(granted),
        "merchant_id": merchant_id,
        "user_type": user_type,
    }
    if include_data_scope:
        payload["data_scope"] = data_scope
    if include_scope_claim:
        # ``scope`` is the space-delimited RFC 6749 form; the verifier also accepts
        # a list, and the two spellings are exercised by separate tests.
        payload["scope"] = " ".join(scopes)
    secret = key or (resolved.hmac_secret.get_secret_value() if resolved.hmac_secret else TEST_HMAC_SECRET)
    return jwt.encode(payload, secret, algorithm=algorithm)


def tamper(token: str) -> str:
    """Flip the last character of a signature, keeping the token well formed.

    Re-encoding the payload with a different key would prove the same thing, but this
    also covers "a client edited the bytes it holds", which is the attack shape the
    signature exists to stop.
    """
    head, _, signature = token.rpartition(".")
    flipped = "A" if signature[-1] != "A" else "B"
    return f"{head}.{signature[:-1]}{flipped}"


def principal_for(
    *,
    user_id: int = 7001,
    merchant_id: int | None = 31,
    user_type: str = UserType.STAFF.value,
    roles: tuple[str, ...] = ("OPERATOR",),
    permissions: frozenset[str] | None = None,
    data_scope: DataScope = DataScope.MERCHANT,
    session_id: str = "sess-mcp-test",
) -> Principal:
    """The platform's own principal type, built exactly as the verifier builds it.

    Deliberately *not* a stub object with the same attributes: a duck-typed stand-in
    would let ``tests/mcp`` pass while the real :class:`Principal` gained a required
    field or changed its permission semantics.
    """
    granted = permissions if permissions is not None else frozenset({p.value for p in PermissionCode})
    return Principal(
        user_id=user_id,
        user_type=user_type,
        merchant_id=merchant_id,
        roles=roles,
        permissions=granted,
        data_scope=data_scope,
        session_id=session_id,
        is_staff=user_type == UserType.STAFF.value,
    )


def verified_token_for(principal: Principal, *, scopes: tuple[str, ...] = ("nova.read",)) -> AccessToken:
    """An ``AccessToken`` carrying a resolved principal, as the verifier produces one."""
    return AccessToken(
        token="test-token",
        client_id=principal.session_id,
        scopes=list(scopes),
        resource=TEST_RESOURCE,
        subject=str(principal.user_id),
        claims={PRINCIPAL_CLAIM: principal},
    )


@contextmanager
def authenticated(principal: Principal, *, scopes: tuple[str, ...] = ("nova.read",)) -> Iterator[None]:
    """Present ``principal`` to in-memory code that reads the auth contextvar.

    This is the in-memory counterpart of the real bearer middleware, and it is
    deliberately the *only* thing it shares with it: it sets the same contextvar the
    HTTP path sets, so a tool body's ``current_principal()`` behaves identically on
    both transports. The HTTP tests then exist to prove the real middleware really
    does populate it, which is the part this helper assumes.
    """
    context_token = auth_context_var.set(AuthenticatedUser(verified_token_for(principal, scopes=scopes)))
    try:
        yield
    finally:
        auth_context_var.reset(context_token)


@dataclass(frozen=True, slots=True)
class HttpServer:
    """A running uvicorn server and the base URL it is reachable at."""

    base_url: str
    port: int

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"


def free_port() -> int:
    """Bind port 0 and report what the OS chose.

    An ephemeral port rather than a fixed one: the suite shares a machine with other
    members' runs, and a hardcoded port turns two concurrent test runs into a
    confusing bind failure instead of two passing ones.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def run_http_server(app: Any, *, port: int | None = None, timeout: float = 20.0) -> Iterator[HttpServer]:
    """Run a real ASGI app under uvicorn in a background thread, and wait until ready.

    Why a real server rather than an in-process ASGI transport: the brief requires at
    least one authorization case to travel a genuine HTTP Streamable-HTTP path, and
    the properties that matter here - the bearer middleware, the transport-security
    middleware, session negotiation, the JSON-RPC framing - only exist when a real
    server is driving the app. An ``ASGITransport`` would exercise the app but skip
    the socket-level behaviour the gate is about.
    """
    import uvicorn

    resolved_port = port or free_port()
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=resolved_port,
        log_level="warning",
        access_log=False,
        lifespan="on",
    )
    server = uvicorn.Server(config)
    # uvicorn installs signal handlers on the main thread; in a worker thread it
    # skips them, which is why this can run inside pytest at all.
    thread = threading.Thread(target=server.run, name="mcp-test-server", daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{resolved_port}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            break
        if not thread.is_alive():
            msg = "uvicorn exited before it began serving"
            raise RuntimeError(msg)
        time.sleep(0.05)
    else:
        server.should_exit = True
        thread.join(timeout=5)
        msg = f"uvicorn did not start within {timeout}s"
        raise RuntimeError(msg)

    try:
        yield HttpServer(base_url=base_url, port=resolved_port)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def decode_with_verifier(token: str, settings: MCPSettings) -> dict[str, Any]:
    """Verify ``token`` with the production verifier's own PyJWT call.

    Used where a test needs the claims of a token it minted, without a second decoder
    that could disagree with the server's.
    """
    from app.mcp.auth import NovaTokenVerifier

    return NovaTokenVerifier(settings).decode_for_test(token)


def principal_from_claims(claims: dict[str, Any]) -> Principal:
    """The verifier's own claim-to-principal conversion, for cross-checks."""
    return build_principal(claims)


__all__ = [
    "TEST_HMAC_SECRET",
    "TEST_ISSUER",
    "TEST_RESOURCE",
    "HttpServer",
    "all_tool_scopes",
    "authenticated",
    "decode_with_verifier",
    "free_port",
    "mint_token",
    "principal_for",
    "principal_from_claims",
    "run_http_server",
    "settings_for_tests",
    "tamper",
    "verified_token_for",
]
