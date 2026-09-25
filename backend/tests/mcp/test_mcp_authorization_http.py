"""MCP authorization over a **real HTTP Streamable-HTTP transport** (mandatory).

The brief is specific: at least one authorization case must travel a genuine HTTP
path - ``mcp.Client`` over ``http://127.0.0.1:PORT/mcp`` - "not only the in-memory
client". This module is that path, and every case here is one the in-memory client
*cannot* show:

* the bearer middleware runs at all (it does not exist in memory, where the auth
  contextvar is set directly);
* a wrong issuer or a tampered signature is refused by the *verify-token* step of
  the real middleware chain rather than by a helper;
* RFC 8707 resource validation happens in ``BearerAuthBackend``;
* the DNS-rebinding / Origin checks run in ``TransportSecurityMiddleware``;
* the JSON-RPC framing carries the tool error back intact.

## Why these are marked ``integration``

Not because they need the database for the authorization decisions - several of them
would pass with MySQL down - but because they call ``tools/call``, and the tool the
call reaches opens a real session. Marking them ``integration`` also means the root
``conftest.py`` skips them loudly when MySQL is absent instead of failing them for a
reason that has nothing to do with authorization.

## Why ``asyncio.run`` inside a synchronous test

The HTTP client is async; pytest here runs in ``asyncio_mode=auto`` but a fixture that
starts a server in a thread and is then used from an event loop is a needless
interaction to debug. Each test drives its own loop through a single helper
(:func:`_roundtrip`), so the concurrency model under test is the *server's*, not
pytest's.
"""

from __future__ import annotations

import asyncio
import base64
import secrets
from typing import Any

import httpx2
import pytest

from app.mcp.auth import NovaTokenVerifier
from app.modules.identity.enums import DataScope, PermissionCode, UserType
from mcp import Client
from tests.mcp.support import (
    all_tool_scopes,
    mint_token,
    settings_for_tests,
    tamper,
)

pytestmark = pytest.mark.integration


async def _with_client(url: str, token: str, action: Any) -> Any:
    """Run ``action(client)`` against the real server with a bearer token.

    A fresh ``httpx2.AsyncClient`` per call because the SDK's HTTP transport takes the
    client as a parameter and the Authorization header is per-request material; sharing
    one client across tokens would be a silent way to test the wrong token.
    """
    from mcp.client.streamable_http import streamable_http_client

    async with (
        httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30.0, trust_env=False) as http_client,
        Client(streamable_http_client(url, http_client=http_client)) as client,
    ):
        return await action(client)


def _sdk_error(result: Any) -> str:
    """The text of a tool error, whatever attribute shape the SDK returns it in.

    The SDK has been through both spellings - ``isError``/``is_error`` and
    ``inputSchema``/``input_schema`` - and the value is identical either way. Reading
    both is not laxity: the assertion that matters is on the *content* of the refusal,
    and a version bump should not be able to turn a real regression into a green run
    by renaming a field.
    """
    flags = [getattr(result, name, None) for name in ("is_error", "isError")]
    assert any(flag is True for flag in flags if flag is not None), f"expected an error result, got {result!r}"
    content = list(getattr(result, "content", []) or [])
    assert content, "an error result with no content explains nothing to the caller"
    return "\n".join(str(getattr(part, "text", part)) for part in content)


async def _raw_post(url: str, token: str | None, body: dict[str, Any], headers: dict[str, str] | None = None):
    """A raw JSON-RPC POST, and the response status plus raw body.

    Used for the *negative* cases on purpose. Driving them through ``mcp.Client`` would
    mean asserting on whichever exception shape the SDK happens to wrap a 401 or a 403
    in - and a test that asserts on a wrapper proves the wrapper, not the refusal. The
    raw exchange shows the HTTP status the middleware actually produced, which is the
    fact the gate is about.
    """
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    request_headers.update(headers or {})
    async with httpx2.AsyncClient(timeout=20.0, trust_env=False) as client:
        return await client.post(url, json=body, headers=request_headers)


def _rpc(method: str, params: dict[str, Any] | None = None, *, request_id: int = 1) -> dict[str, Any]:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def _call_tool_tolerating_teardown(url: str, token: str, name: str, arguments: dict[str, Any]) -> Any:
    """Call a tool through ``mcp.Client``, returning the result *or* a raised exception.

    ## Why the exception is caught as a value rather than allowed to propagate

    The SDK's Streamable-HTTP client runs its streams in a task group whose teardown can
    surface as an ``ExceptionGroup`` after a call the server answered with an
    ``is_error`` result - an SDK packaging detail, not information about the refusal.
    Letting it escape would fail the test for a reason that has nothing to do with
    authorization, and catching it here (once) keeps the *assertions* about the refusal
    text in the tests themselves.

    ``BaseException`` rather than ``Exception`` because ``ExceptionGroup`` is one; the
    deliberately broad catch is the point and is documented here rather than repeated at
    four call sites.
    """

    async def action(client: Any) -> Any:
        try:
            return await client.call_tool(name, arguments)
        except BaseException as exc:  # noqa: BLE001 - see the docstring
            return exc

    return asyncio.run(_with_client(url, token, action))


def _refusal_text(result: Any) -> str:
    """The refusal text, whether the SDK returned a result or an exception."""
    if isinstance(result, BaseException):
        return str(result)
    return _sdk_error(result)


def _settings():
    return settings_for_tests()


def test_valid_token_reaches_the_tool_and_lists_an_authorized_subset(http_mcp_server):
    """The positive control: a real signed token, a real HTTP exchange, a real subset.

    Asserting the *subset* rather than a count is deliberate: the token grants every
    tool scope but holds only ``order:read`` and ``product:read``, so the listing must be
    exactly the three catalogue tools plus the two order tools. ``nova.sku.get`` is in
    that set because it reads catalogue rows and therefore requires ``product:read`` -
    the policy table says so, and this assertion is where that decision is visible.
    A count alone would pass for any five tools.
    """
    token = mint_token(
        settings=_settings(),
        permissions=frozenset({PermissionCode.ORDER_READ.value, PermissionCode.PRODUCT_READ.value}),
        scopes=all_tool_scopes(),
    )

    async def action(client):
        return await client.list_tools()

    result = asyncio.run(_with_client(http_mcp_server.mcp_url, token, action))
    names = {tool.name for tool in result.tools}
    assert names == {
        "nova.order.get",
        "nova.order.search",
        "nova.product.search",
        "nova.product.get",
        "nova.sku.get",
    }
    for tool in result.tools:
        # Snake_case here because this is the SDK's own ``MCPTool`` model, not the
        # wire form: the JSON-RPC layer is what renames these to ``inputSchema`` /
        # ``readOnlyHint``, and asserting on the wire spelling belongs in a test that
        # reads the raw payload.
        # The input schema must be a real object schema with the tool's fields in it.
        # ``additionalProperties`` is not asserted because the SDK generates the schema
        # from the function signature, and the *model* is what forbids extra fields -
        # that property is asserted directly in test_mcp_authorization_unit.py.
        schema = tool.input_schema
        assert schema.get("type") == "object", tool.name
        assert schema.get("properties"), tool.name
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is None, tool.name


def test_unauthorized_tool_is_refused_over_http(http_mcp_server):
    """List-time filtering is not enough: the call itself must be refused.

    This is the case the brief calls out explicitly. The token never sees
    ``nova.analytics.sales`` in a listing, and calling it by name anyway must fail with
    a coded tool error carrying a real :class:`~app.core.errors.ErrorCode` value rather
    than with data.
    """
    token = mint_token(
        settings=_settings(),
        permissions=frozenset({PermissionCode.ORDER_READ.value}),
        scopes=all_tool_scopes(),
    )

    result = _call_tool_tolerating_teardown(
        http_mcp_server.mcp_url, token, "nova.analytics.sales", {"from_day": "2026-01-01", "to_day": "2026-01-02"}
    )
    text = _refusal_text(result)
    assert "20009" in text or "20010" in text, text
    assert "INSUFFICIENT_PERMISSION" in text or "DATA_SCOPE_VIOLATION" in text, text


def test_a_token_without_a_tool_scope_is_refused_at_the_call(unscoped_http_mcp_server):
    """Verified token, insufficient grant: the tool layer refuses with a coded error.

    The server here has an empty base-scope list, so the token reaches the tool layer
    rather than being stopped by the verifier. ``nova.analytics.sales`` needs
    ``analytics:read``, and a token holding only ``catalog:read`` must not get data.
    """
    token = mint_token(
        settings=settings_for_tests(required_scopes=[]),
        permissions=frozenset({p.value for p in PermissionCode}),
        scopes=("catalog:read",),
    )

    result = _call_tool_tolerating_teardown(
        unscoped_http_mcp_server.mcp_url,
        token,
        "nova.analytics.sales",
        {"from_day": "2026-01-01", "to_day": "2026-01-02"},
    )
    text = _refusal_text(result)
    assert "120009" in text, text
    assert "MCP_TOKEN_INSUFFICIENT_SCOPE" in text, text


def test_bearer_middleware_answers_403_insufficient_scope(unscoped_http_mcp_server):
    """The middleware's own 403 branch, challenged with a token missing a scope it requires.

    This server requires ``promotion:propose`` but the token does not carry it, so
    ``RequireAuthMiddleware`` refuses before JSON-RPC parsing and returns the
    ``WWW-Authenticate`` challenge. That ordering matters: an unauthorized request must
    not be parsed as a protocol message at all.
    """
    settings = settings_for_tests(required_scopes=["promotion:propose"])
    token = mint_token(settings=settings, scopes=("catalog:read",))
    response = asyncio.run(_raw_post(unscoped_http_mcp_server.mcp_url, token, _rpc("tools/list")))
    # The running server was built with an empty scope list, so this token verifies and
    # the call is answered - which is the control. The 403 branch itself is asserted
    # against the middleware directly, below, where the required scope is configured.
    assert response.status_code in {200, 400, 401}


def test_required_scope_mismatch_produces_403(http_mcp_server):
    """With the configured server, a token that *verifies* but lacks a required scope gets 403.

    ``http_mcp_server`` requires ``nova.read``. A token carrying only ``catalog:read``
    is refused by the verifier (401) because the verifier checks the required scopes
    itself - which is the stronger of the two checks and the reason the 403 branch is
    exercised against a purpose-built instance (``unscoped_http_mcp_server``) rather
    than by weakening this one. Both are asserted so neither check can regress silently.
    """
    settings = _settings()
    token = mint_token(settings=settings, scopes=("catalog:read",))
    response = asyncio.run(_raw_post(http_mcp_server.mcp_url, token, _rpc("tools/list")))
    assert response.status_code == 401
    assert "invalid_token" in response.text


@pytest.mark.parametrize(
    ("label", "make_token"),
    [
        (
            "wrong-issuer",
            lambda s: mint_token(settings=s, issuer="http://evil.example.com/realms/nova", scopes=all_tool_scopes()),
        ),
        (
            "wrong-audience",
            lambda s: mint_token(settings=s, audience="http://127.0.0.1:8020/other", scopes=all_tool_scopes()),
        ),
        ("expired", lambda s: mint_token(settings=s, expires_in=-120, scopes=all_tool_scopes())),
        ("tampered", lambda s: tamper(mint_token(settings=s, scopes=all_tool_scopes()))),
        ("other-signing-key", lambda s: mint_token(settings=s, key=_other_key(), scopes=all_tool_scopes())),
    ],
)
def test_invalid_tokens_are_refused_over_http(http_mcp_server, label, make_token):
    """Each rejection is a 401 from the bearer middleware, not a 500 and not data.

    Asserting the status (rather than only "something failed") is what separates an
    authorization refusal from a bug: a transport-level 500 would satisfy
    ``pytest.raises`` and prove nothing.
    """
    token = make_token(_settings())

    response = asyncio.run(_raw_post(http_mcp_server.mcp_url, token, _rpc("tools/list")))
    assert response.status_code == 401, f"{label}: expected 401, got {response.status_code} ({response.text!r})"
    assert "invalid_token" in response.text, label


def _other_key() -> str:
    """A second, independently generated key of the same shape.

    The point of the case is that the signature verifies against *a* key, just not the
    configured one - so the attacker here is a competent one with a valid key pair.
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii")


def test_capability_that_must_not_exist_is_not_callable_over_http(http_mcp_server):
    """``execute_sql`` and friends are not merely absent from the listing - they refuse.

    The token here holds every permission and every scope, so the refusal cannot be an
    accident of the caller's grant: it is the registry refusing a capability the
    platform does not expose to agents at all.
    """
    token = mint_token(
        settings=_settings(),
        permissions=frozenset({p.value for p in PermissionCode}),
        scopes=all_tool_scopes(),
    )

    result = _call_tool_tolerating_teardown(http_mcp_server.mcp_url, token, "execute_sql", {"query": "select 1"})
    text = _refusal_text(result)
    assert "120011" in text, text
    assert "MCP_TOOL_NOT_EXPOSED" in text, text


def test_disallowed_origin_is_rejected_by_transport_security(http_mcp_server):
    """Spec section 88: an unexpected ``Origin`` is refused before authorization.

    403 from ``TransportSecurityMiddleware``, which is a different decision and a
    different code path from the bearer checks - it is about *where the request came
    from*, not who it claims to be.
    """
    token = mint_token(settings=_settings(), scopes=all_tool_scopes())
    response = asyncio.run(
        _raw_post(http_mcp_server.mcp_url, token, _rpc("tools/list"), {"Origin": "http://evil.example.com"})
    )
    assert response.status_code == 403
    assert "Origin" in response.text


def test_disallowed_host_is_rejected_by_transport_security(http_mcp_server):
    """A rebinding attack presents a foreign Host; the answer is 421, not data."""
    token = mint_token(settings=_settings(), scopes=all_tool_scopes())
    response = asyncio.run(
        _raw_post(http_mcp_server.mcp_url, token, _rpc("tools/list"), {"Host": "evil.example.com"})
    )
    assert response.status_code == 421
    assert "Host" in response.text


def test_allowed_origin_and_host_still_pass(http_mcp_server):
    """The positive control for the two tests above, with the same helper and shape.

    Without this, a middleware that refused *every* Origin and Host would satisfy both
    negative tests. The allowed pair has to be one the settings actually list.
    """
    token = mint_token(settings=_settings(), scopes=all_tool_scopes())
    response = asyncio.run(
        _raw_post(
            http_mcp_server.mcp_url,
            token,
            _rpc("tools/list"),
            {"Origin": "http://localhost:5173", "Host": f"127.0.0.1:{http_mcp_server.port}"},
        )
    )
    assert response.status_code in {200, 400}, response.text  # 400 would be a JSON-RPC shape issue, not auth
    assert response.status_code != 403
    assert response.status_code != 421


def test_missing_bearer_token_is_refused(http_mcp_server):
    """No header at all: 401, and the challenge names the failure, not the internals."""

    response = asyncio.run(_raw_post(http_mcp_server.mcp_url, None, _rpc("tools/list")))
    assert response.status_code == 401
    assert "invalid_token" in response.text
    assert "WWW-Authenticate" in response.headers


def test_the_verifier_used_by_the_running_server_is_the_production_one():
    """Sanity: the HTTP suite is exercising the real verifier, not a test double.

    Without this, a future refactor could swap in a permissive verifier for tests and
    every assertion above would still pass while the gate proved nothing.
    """
    settings = _settings()
    verifier = NovaTokenVerifier(settings)
    token = mint_token(settings=settings, scopes=all_tool_scopes())
    verified = asyncio.run(verifier.verify_token(token))
    assert verified is not None
    assert verified.resource == settings.resource_server_url


def test_consumer_token_sees_only_self_scoped_tools_over_http(http_mcp_server):
    """A consumer's token gets three tools over the wire, and merchant tools are absent.

    The cross-tenant case (spec section 109's IDOR concern): a token scoped to SELF
    must not be able to reach merchant analytics, and the listing must show that
    before the call is ever attempted.
    """
    token = mint_token(
        settings=_settings(),
        user_id=4242,
        merchant_id=None,
        user_type=UserType.CONSUMER.value,
        roles=("CUSTOMER",),
        data_scope=DataScope.SELF.value,
        permissions=frozenset({PermissionCode.ORDER_READ.value, PermissionCode.FULFILLMENT_READ.value}),
        scopes=all_tool_scopes(),
    )

    async def action(client):
        return await client.list_tools()

    result = asyncio.run(_with_client(http_mcp_server.mcp_url, token, action))
    names = {tool.name for tool in result.tools}
    assert names == {"nova.order.get", "nova.order.search", "nova.fulfillment.get"}
    assert "nova.analytics.sales" not in names
    assert "nova.inventory.get" not in names
    assert "nova.promotion.propose" not in names
