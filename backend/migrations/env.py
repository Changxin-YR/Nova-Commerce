"""Alembic environment.

Spec §129: Alembic owns the schema. Two rules are implemented here rather than
left to discipline:

1. **The URL comes from application settings**, never from ``alembic.ini``. If
   the URL were duplicated, the app and the migration tool could point at
   different databases and the mismatch would only surface in production.
2. **Every model must be imported before autogenerate runs.** SQLAlchemy can
   only diff what is present in ``Base.metadata``; a model that nobody imports
   is invisible, and autogenerate would cheerfully emit a migration that DROPS
   its table. ``_import_all_models()`` below therefore fails loudly if it finds
   a model module it could not load.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context

# Make `app` importable when alembic is invoked from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402
from app.shared.db.base import metadata  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()
DATABASE_URL = settings.DATABASE_URL


def _import_all_models() -> list[str]:
    """Import every bounded context's models so autogenerate can see them.

    Modules are discovered rather than hard-coded: a new ``modules/<ctx>/models.py``
    is picked up automatically, which prevents the classic failure where a
    contributor adds a model, forgets to register it, and autogenerate proposes
    dropping the table.
    """
    import importlib
    from pathlib import Path as _Path

    modules_root = _Path(__file__).resolve().parents[1] / "app" / "modules"
    imported: list[str] = []
    if not modules_root.is_dir():
        return imported

    for ctx_dir in sorted(p for p in modules_root.iterdir() if p.is_dir()):
        if ctx_dir.name.startswith(("_", ".")):
            continue
        for candidate in ("models", "model"):
            module_path = f"app.modules.{ctx_dir.name}.{candidate}"
            try:
                importlib.import_module(module_path)
            except ModuleNotFoundError as exc:
                # Distinguish "this context has no models module" from "the
                # models module exists but failed to import". The latter must be
                # fatal: continuing would let autogenerate drop real tables.
                if exc.name == module_path:
                    continue
                raise
            imported.append(module_path)
            break

    # Shared/infrastructure models (idempotency records, outbox, audit) live
    # outside the domain modules because several contexts write to them.
    for shared_module in (
        "app.shared.db.models.audit",
        "app.shared.db.models.idempotency",
        "app.shared.db.models.outbox",
    ):
        try:
            importlib.import_module(shared_module)
            imported.append(shared_module)
        except ModuleNotFoundError as exc:
            if exc.name == shared_module:
                continue
            raise
    return imported


IMPORTED_MODEL_MODULES = _import_all_models()

target_metadata = metadata


def include_object(
    obj: object,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: object,
) -> bool:
    """Exclude objects Alembic should not manage.

    ``alembic_version`` is created by Alembic itself; diffing it would produce
    spurious drop statements on every autogenerate run.
    """
    if type_ == "table" and name == "alembic_version":
        return False
    return not (reflected and type_ == "table" and name is not None and name.startswith("_tmp_"))


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (used for review and for CI diffs)."""
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
        # Render the CHECK constraints that back INV-001/INV-002 so the generated
        # SQL is a complete, reviewable statement of intent.
        render_as_batch=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live connection."""
    from sqlalchemy import create_engine, pool

    connectable = create_engine(DATABASE_URL, poolclass=pool.NullPool, future=True)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
            # MySQL DDL is not transactional; batch mode rewrites ALTERs into
            # safer create-copy-swap sequences where supported.
            render_as_batch=False,
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
