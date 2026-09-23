"""Shared pytest fixtures.

Design rule for this project: fixtures that represent *infrastructure* are
opt-in and skip loudly when the dependency is missing, while fixtures that
represent *pure logic* are always available. The mandatory gates (inventory
concurrency, payment idempotency, refund caps) are marked `integration` or
`concurrency` precisely so that nobody can accidentally "pass" them on a mock -
spec §113 is explicit that a mocked database does not count.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"


@pytest.fixture(scope="session", autouse=True)
def _stable_test_environment() -> Iterator[None]:
    """Pin the environment so tests never touch a real deployment.

    ``APP_ENV=test`` additionally switches object storage to the in-memory
    backend, which is why the storage *unit* tests can run without MinIO while
    FG-19 still demands the real thing.
    """
    previous = dict(os.environ)
    os.environ.update(
        {
            "APP_ENV": "test",
            "APP_DEBUG": "false",
            "LOG_JSON": "false",
            "LOG_LEVEL": "WARNING",
            "AI_USE_FAKE_PROVIDERS": "true",
            "RATE_LIMIT_ENABLED": "false",
            # Deterministic seed so snapshot-style assertions cannot flake.
            "SEED_DETERMINISTIC": "true",
        }
    )
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    os.environ.clear()
    os.environ.update(previous)
    get_settings.cache_clear()


@pytest.fixture
def settings():
    """Fresh settings object for the current test environment."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    return get_settings()


@pytest.fixture
def storage(settings):
    """In-memory object storage with buckets pre-created."""
    from app.shared.storage.backends.memory_backend import InMemoryObjectStorage

    backend = InMemoryObjectStorage(settings)
    backend.ensure_buckets()
    return backend


@pytest.fixture
def client(settings):
    """ASGI test client that actually works, via ``fastapi.testclient``.

    **This fixture was broken from Phase 1 until Phase 5** and no test used it, so
    the defect was latent: ``httpx.Client(transport=httpx.ASGITransport(app=app))``
    cannot work with httpx 0.28, because ``ASGITransport`` implements only
    ``handle_async_request``. A *synchronous* ``httpx.Client`` therefore raises
    ``AttributeError: 'ASGITransport' object has no attribute 'handle_request'`` on
    its first call. Phase 4 worked around it with a module-local fixture in
    ``tests/integration/order/conftest.py`` and reported it rather than silently
    patching a file it did not own; Phase 5 fixes it at source (HANDOFF section
    17.5, obligation 4).

    ``fastapi.testclient.TestClient`` drives the app through its own anyio portal,
    so no async test plumbing is needed at the call site, and the real middleware
    stack (correlation id, body limit, security headers, error handlers) still
    runs - which is the property the original fixture was reaching for.

    A caller that needs async (an SSE stream, say) must use
    ``httpx.AsyncClient(transport=httpx.ASGITransport(app))`` explicitly; keeping
    both modes in one fixture is how the sync one became broken in the first
    place.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(settings)) as http_client:
        yield http_client


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip infrastructure-backed tests when the service is absent.

    The skip is *loud and named* rather than silent: a gate that quietly skipped
    would look identical to a gate that passed, which is the failure mode spec
    §149 exists to prevent.
    """
    if config.getoption("-m") and "integration" not in str(config.getoption("-m")):
        return

    from app.shared.db.session import ping_database
    from app.shared.redis_client import ping_redis

    mysql_ok, _ = ping_database()
    redis_ok, _ = ping_redis()

    skip_integration = pytest.mark.skip(
        reason="requires real MySQL/Redis - start `docker compose -f ops/docker-compose.yml up -d`"
    )
    for item in items:
        needs_infrastructure = "integration" in item.keywords or "concurrency" in item.keywords
        if needs_infrastructure and not (mysql_ok and redis_ok):
            item.add_marker(skip_integration)
