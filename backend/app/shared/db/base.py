"""Declarative base, naming conventions and shared mixins.

Spec references:
    §19  schema conventions (utf8mb4, BIGINT UNSIGNED, DATETIME(3), JSON only
         for snapshots/config/metadata)
    §129 Alembic is the sole schema authority; ``create_all()`` is never used
         to manage a production schema.

Why an explicit naming convention matters more than it looks: MySQL cannot
drop or alter an unnamed constraint/index portably, so a migration that needs
to drop the CHECK constraint backing INV-001 becomes guesswork when the
constraint is auto-named. Declaring the convention up front means every
constraint has a predictable, greppable name from the first migration onwards.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import ForeignKey, MetaData, String, func, text
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column
from sqlalchemy.sql.elements import TextClause

from app.shared.db.types import BigIntUnsigned, DateTimeMS

#: Table options applied to every table: utf8mb4 everywhere, InnoDB for real
#: transactions (the inventory lock of §27 requires row-level locking, which
#: MyISAM does not provide).
MYSQL_TABLE_OPTIONS: dict[str, str] = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Every domain's ``metadata`` is shared so a single Alembic environment can
#: autogenerate across all bounded contexts (spec §13).
metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    """Timezone-aware UTC now. The one place the app is allowed to read a clock.

    Centralised so that tests can freeze time in a single location, and so that
    no module accidentally writes a naive datetime (which
    :class:`~app.shared.db.types.DateTimeMS` would reject at persist time).
    """
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Root declarative class for every ORM model in the project."""

    metadata = metadata

    #: Applied per table so utf8mb4/InnoDB are not repeated in every model.
    __table_args__: ClassVar[dict[str, Any]] = MYSQL_TABLE_OPTIONS

    def __repr__(self) -> str:
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"

    def to_dict(self, *, exclude: set[str] | None = None) -> dict[str, Any]:
        """Shallow column snapshot.

        Used by the audit layer (spec §133) to build before/after snapshots.
        Relationships are deliberately excluded - a snapshot must be flat and
        bounded, and the redaction layer bounds it further.
        """
        excluded = exclude or set()
        return {
            column.key: getattr(self, column.key)
            for column in self.__table__.columns
            if column.key not in excluded
        }


class PkMixin:
    """``id BIGINT UNSIGNED AUTO_INCREMENT`` primary key (spec §19)."""

    id: Mapped[int] = mapped_column(BigIntUnsigned, primary_key=True, autoincrement=True)


class TimestampMixin:
    """``created_at`` / ``updated_at`` as ``DATETIME(3)`` in UTC.

    ``server_default`` is used for ``created_at`` so the database stamps rows
    even if a bulk insert bypasses the ORM, keeping the audit trail honest.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTimeMS,
        nullable=False,
        server_default=func.now(3),
        default=utc_now,
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTimeMS,
        nullable=False,
        server_default=func.now(3),
        default=utc_now,
        onupdate=utc_now,
    )


class MerchantScopedMixin:
    """Carries ``merchant_id`` on commercial data (spec §4, §24).

    V1 runs a single merchant, but the column is present from the first
    migration precisely so that multi-merchant support is not a schema
    migration later. ``nullable=True`` because *consumer* users have no
    merchant, while staff and all commercial rows do.

    ``__merchant_fk_target__`` exists because two different needs collide:

    * **Referential integrity.** A ``merchant_id`` that points at a merchant
      that does not exist is silent data corruption, so by default the column
      carries a real foreign key.
    * **Generality.** A table may legitimately be merchant-scoped without a
      hard FK (for example a high-write table where the constraint cost is not
      worth it). Such a model sets ``__merchant_fk_target__ = None``.

    The default is the safe one: FK on. It is also what lets SQLAlchemy infer
    ``Merchant.users`` - without any foreign key, the relationship cannot be
    configured at all, which is exactly how this was discovered.
    """

    #: Class-level opt-out. Defaults to enforcing the FK.
    __merchant_fk_target__: ClassVar[str | None] = "merchants.id"

    @declared_attr
    def merchant_id(cls) -> Mapped[int | None]:  # noqa: N805
        target = getattr(cls, "__merchant_fk_target__", "merchants.id")
        if target:
            # RESTRICT, not SET NULL, and the reason is a hard MySQL rule rather
            # than a preference: MySQL 8 rejects (errno 3823) a column that
            # participates in BOTH a CHECK constraint and a foreign key whose
            # referential action mutates it. `users.merchant_id` must satisfy
            # `staff_requires_merchant`, so a mutating action is not available.
            #
            # RESTRICT also happens to be the more correct policy here: merchants
            # are soft-deleted (see SoftDeleteMixin), so a hard delete that would
            # orphan staff accounts should be refused rather than silently
            # allowed to null out their tenant.
            return mapped_column(
                BigIntUnsigned,
                ForeignKey(target, ondelete="RESTRICT"),
                nullable=True,
                index=True,
                doc="Owning merchant; NULL for consumer-scoped rows (spec section 24).",
            )
        return mapped_column(BigIntUnsigned, nullable=True, index=True)


class VersionMixin:
    """Optimistic-locking counter (spec §26, §27, §30).

    The inventory row carries ``version`` so low-contention background edits can
    detect a concurrent change; the CreateOrder path still uses a pessimistic
    ``SELECT ... FOR UPDATE`` because a retry-based scheme is the wrong tool for
    a hot row that must never oversell (§27/ADR-004).
    """

    version: Mapped[int] = mapped_column(
        BigIntUnsigned,
        nullable=False,
        default=1,
        server_default=text("1"),
        doc="Optimistic-locking counter; incremented on every mutation.",
    )


class SoftDeleteMixin:
    """Reversible removal without destroying history.

    Commerce data is never hard-deleted: an order that references a product must
    keep resolving after the product is withdrawn (INV-014). Rows are hidden
    from default queries instead.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True, index=True)

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


# ---------------------------------------------------------------------------
# Reusable free-text column helpers
# ---------------------------------------------------------------------------
def short_str(length: int = 64) -> String:
    """Human-facing identifier/name column."""
    return String(length)


def status_column(length: int = 32) -> String:
    """Status column: ``VARCHAR`` + Python Enum (spec §19).

    Stored as a string rather than a MySQL ``ENUM`` so that adding a state is an
    ordinary migration and never a table rewrite, and so the vocabulary stays
    reviewable in Python.
    """
    return String(length)


def stable_ddl_hash() -> str:
    """Fingerprint of the naming convention, used by migration tests."""
    return str(hash(tuple(sorted(NAMING_CONVENTION.items()))))


#: ``SELECT ... FOR UPDATE`` suffix used by the inventory reservation path (§27).
#: Exposed as a constant so the architecture test can assert that no other
#: module invents its own locking clause.
FOR_UPDATE_NOWAIT_HINT: TextClause = text("FOR UPDATE")


__all__ = [
    "FOR_UPDATE_NOWAIT_HINT",
    "MYSQL_TABLE_OPTIONS",
    "NAMING_CONVENTION",
    "Base",
    "MerchantScopedMixin",
    "PkMixin",
    "SoftDeleteMixin",
    "TimestampMixin",
    "VersionMixin",
    "metadata",
    "short_str",
    "status_column",
    "utc_now",
]
