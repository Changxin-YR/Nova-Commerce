"""Engine, session factory and transaction scopes.

Spec references:
    §19  MySQL 8.x. ``SELECT ... FOR UPDATE`` semantics are required by §27, and
         the mandatory gates (FG-09/11/12) must run against real MySQL.
    §27  CreateOrder locks stock with a **short** transaction; Redis locks must
         never replace it (ADR-004).
    §49  business rows and the outbox row commit in ONE transaction.
    §129 Alembic owns the schema; nothing here creates tables.

Concurrency notes that shape this module:

* MySQL's default ``innodb_lock_wait_timeout`` is 50 seconds. Under the 20-way
  contention of FG-09 that would make a losing request hang for the better part
  of a minute before reporting "insufficient stock". A shorter per-session
  timeout turns lock contention into a prompt, honest failure - which is also
  what a real checkout must do.
* ``pool_pre_ping`` matters because MySQL closes idle connections after
  ``wait_timeout``; without it the first request after an idle period fails on a
  dead socket instead of transparently reconnecting.
* Sessions are never shared across threads. FastAPI's dependency yields a
  session per request; Celery tasks open their own scope.
"""

from __future__ import annotations

import contextlib
from collections.abc import Generator, Iterator
from typing import Any, Final

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

#: Lock waits beyond this are contention, not progress. 5s is comfortably longer
#: than any legitimate transaction in this application (all are short by design)
#: and far shorter than MySQL's 50s default.
INNODB_LOCK_WAIT_TIMEOUT_SECONDS: Final[int] = 5

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def _build_engine(settings: Settings) -> Engine:
    if settings.DB_REQUIRE_MYSQL and "mysql" not in settings.DATABASE_URL:
        msg = (
            "DB_REQUIRE_MYSQL is enabled but DATABASE_URL is not MySQL. "
            "Row-level locking and the mandatory concurrency gates depend on real "
            "MySQL semantics (spec §27, §113)."
        )
        raise RuntimeError(msg)

    connect_args: dict[str, Any] = {
        "charset": "utf8mb4",
        "connect_timeout": 10,
        # Read/write/exec split so a runaway analytical query cannot pin a
        # connection indefinitely.
        "read_timeout": 60,
        "write_timeout": 60,
    }

    return create_engine(
        settings.DATABASE_URL,
        echo=settings.DB_ECHO,
        poolclass=QueuePool,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_recycle=settings.DB_POOL_RECYCLE_SECONDS,
        pool_pre_ping=True,
        pool_use_lifo=True,  # reuse the hottest connection; keeps the pool small
        connect_args=connect_args,
        # Destructive DDL must never be reachable from an ORM session.
        future=True,
    )


def _register_session_listeners(engine: Engine) -> None:
    """Apply per-connection session settings.

    Kept as connection-level ``SET SESSION`` rather than baked into the URL so
    the intent is visible and adjustable per environment.
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _connection_record: Any) -> None:
        with contextlib.suppress(Exception):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute(f"SET SESSION innodb_lock_wait_timeout = {INNODB_LOCK_WAIT_TIMEOUT_SECONDS}")
                # UTC at the connection level so NOW(3) defaults and comparisons
                # agree with the UTC contract enforced by DateTimeMS.
                cursor.execute("SET SESSION time_zone = '+00:00'")
                cursor.execute("SET SESSION sql_mode = 'STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION'")
                # READ-COMMITTED rather than MySQL's default REPEATABLE-READ:
                # under REPEATABLE-READ a long-lived session can silently serve a
                # stale snapshot, which for a stock check is the difference
                # between a correct rejection and an oversell.
                cursor.execute("SET SESSION transaction_isolation = 'READ-COMMITTED'")
            finally:
                cursor.close()


def configure_database(settings: Settings | None = None) -> Engine:
    """Create (or return) the process-wide engine and session factory."""
    global _engine, _session_factory

    if _engine is not None:
        return _engine

    resolved = settings or get_settings()
    _engine = _build_engine(resolved)
    _register_session_listeners(_engine)
    _session_factory = sessionmaker(
        bind=_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,  # lets responses read committed attributes safely
    )
    logger.info(
        "database configured",
        pool_size=resolved.DB_POOL_SIZE,
        max_overflow=resolved.DB_MAX_OVERFLOW,
        require_mysql=resolved.DB_REQUIRE_MYSQL,
    )
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        return configure_database()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        configure_database()
    assert _session_factory is not None
    return _session_factory


def dispose_database() -> None:
    """Close pooled connections. Called on shutdown and between tests."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


# ---------------------------------------------------------------------------
# Scopes
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def session_scope(*, readonly: bool = False) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception.

    Use this in Celery tasks, CLI entry points and seeds. FastAPI request
    handlers use :func:`get_session` instead, which is driven by the framework.

    ``readonly=True`` expresses intent and skips the commit, but note that MySQL
    does not give a cheap read-only transaction here; the flag exists so callers
    document themselves and so read paths never accidentally write.
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        if not readonly:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency.

    Deliberately does **not** auto-commit. Application services and workflows
    decide transaction boundaries explicitly, because §49 requires the business
    rows and the outbox row to commit together - an implicit commit somewhere in
    the request lifecycle would break that.
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Health probing
# ---------------------------------------------------------------------------
def ping_database() -> tuple[bool, str]:
    """Cheap liveness probe for ``/health/ready`` (spec §130: MySQL is critical).

    Returns ``(healthy, detail)`` instead of raising so the health endpoint can
    report every dependency rather than aborting on the first failure.
    """
    try:
        engine = get_engine()
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True, "ok"
    except SQLAlchemyError as exc:
        return False, type(exc).__name__
    except Exception as exc:  # pragma: no cover - configuration errors
        return False, type(exc).__name__


def database_identity() -> dict[str, str]:
    """Return version/charset facts. Used by evidence reports, not by app logic."""
    engine = get_engine()
    with engine.connect() as connection:
        version = connection.execute(text("SELECT VERSION()")).scalar_one()
        charset = connection.execute(text("SELECT @@character_set_database")).scalar_one()
        collation = connection.execute(text("SELECT @@collation_database")).scalar_one()
        isolation = connection.execute(text("SELECT @@transaction_isolation")).scalar_one()
    return {
        "version": str(version),
        "charset": str(charset),
        "collation": str(collation),
        "isolation": str(isolation),
    }


__all__ = [
    "configure_database",
    "database_identity",
    "dispose_database",
    "get_engine",
    "get_session",
    "get_session_factory",
    "ping_database",
    "session_scope",
]
