"""Fixtures for the MCP gate (FG-18).

Two things live here and nothing else: the marker declaration that tells pytest which
of these tests talk to real infrastructure, and re-exports of the shared commerce
seed. The seed itself is imported rather than re-derived because a second
merchant/SKU/stock fixture would be a second shop, and the first symptom of two shops
is a test that passes alone and fails in the suite because it resolved somebody
else's warehouse (``PHASE5_DESIGN`` section 12).

## Why most of this suite is not marked ``integration``

The authorization tests are *pure*: a token, a policy table and a verifier. They are
marked ``unit`` because they genuinely need no database, and marking them
``integration`` would mean the mandatory gate could only run where MySQL is up - for
tests that never open a connection. The tool-behaviour tests, which do read and write
real rows, are marked ``integration`` and are skipped loudly when MySQL is absent by
the root ``conftest.py``.
"""

from __future__ import annotations

import pytest

from tests.integration.commerce.seed import engine, shop
from tests.mcp.support import HttpServer, run_http_server, settings_for_tests

__all__ = [
    "engine",
    "http_mcp_server",
    "mcp_settings",
    "shop",
    "unscoped_http_mcp_server",
]


@pytest.fixture
def mcp_settings():
    """Test settings for one test, so a mutation cannot leak into the next."""
    return settings_for_tests()


@pytest.fixture
def unscoped_http_mcp_server() -> HttpServer:
    """A second real server whose ``required_scopes`` is empty.

    Exists for exactly one test: the bearer middleware's own 403
    ``insufficient_scope`` path. With the configured server, ``required_scopes``
    contains ``nova.read``, and a token lacking it is refused by the *verifier* with a
    401 before the middleware ever compares scopes - so the 403 branch is unreachable
    from the outside.

    The alternative would be to weaken the configured server's ``required_scopes`` for
    the benefit of a test, which is the wrong trade: a mandatory gate must not require
    the deployment to be configured less strictly than production. A second instance
    with its own settings object proves the middleware branch without touching the
    first one's configuration.
    """
    from app.mcp.app import create_mcp_app

    with run_http_server(create_mcp_app(settings_for_tests(required_scopes=[]))) as server:
        yield server


@pytest.fixture
def http_mcp_server(mcp_settings) -> HttpServer:
    """A real uvicorn server hosting the real Streamable-HTTP MCP app.

    Only the authorization module uses this, because starting a server is the
    expensive part: one fixture per test that needs a genuine HTTP exchange, rather
    than a session-wide server whose state could leak between tests.

    The app is built from ``mcp_settings`` rather than from the process environment,
    so the test's expected issuer/audience/scopes are the ones the server actually
    enforces. Building it from ``get_settings()`` would make the test depend on
    whatever ``.env`` happens to contain, which is exactly the class of accidental
    pass the frozen rules forbid.
    """
    from app.mcp.app import create_mcp_app

    with run_http_server(create_mcp_app(mcp_settings)) as server:
        yield server
