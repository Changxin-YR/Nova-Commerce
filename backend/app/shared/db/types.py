"""Custom SQLAlchemy column types.

Spec §19 freezes the physical schema conventions, and they are unusual enough
that the defaults cannot be trusted:

    MySQL 8.x, utf8mb4
    primary keys      BIGINT UNSIGNED
    money             BIGINT, unit = cents; FLOAT/DOUBLE is FORBIDDEN
    timestamps        DATETIME(3)
    status columns    VARCHAR + Python Enum
    JSON              only for snapshots, rule config, metadata

Each type below degrades gracefully on non-MySQL dialects so the same models can
be used by migration tooling and by SQLite-backed unit tests, while *production
behaviour is MySQL-exact*. The mandatory gates (FG-09/11/12) run on real MySQL
only - a SQLite pass proves nothing about `SELECT ... FOR UPDATE`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, Numeric, TypeDecorator, func
from sqlalchemy.dialects import mysql
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeEngine

__all__ = [
    "BigIntUnsigned",
    "DateTimeMS",
    "MoneyMinor",
    "PercentBasisPoints",
    "UTCDateTimeMS",
    "display_to_money",
    "money_to_display",
]


class BigIntUnsigned(TypeDecorator[int]):
    """``BIGINT UNSIGNED`` primary/foreign keys (spec §19).

    Why unsigned matters: it doubles the id ceiling and, more importantly,
    makes an accidental negative id a database error rather than silent
    corruption. FKs must use this same type or MySQL will reject the constraint
    on a signedness mismatch - which is a feature, because it forces every FK
    column to be declared deliberately.
    """

    impl = BigInteger
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.BIGINT(unsigned=True))
        return dialect.type_descriptor(BigInteger())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        value = int(value)
        if dialect.name == "mysql" and value < 0:
            msg = f"unsigned column received a negative value: {value}"
            raise ValueError(msg)
        return value


class DateTimeMS(TypeDecorator[datetime]):
    """``DATETIME(3)`` - millisecond precision (spec §19).

    MySQL's bare ``DATETIME`` truncates to whole seconds, which is too coarse
    for audit ordering and for `created_at` vs `updated_at` comparisons inside a
    single transaction. ``DATETIME(3)`` is the frozen choice.

    MySQL has no timezone-aware datetime type, so values are stored in **UTC**
    and returned as timezone-aware UTC. Application code therefore never has to
    guess whether a naive datetime is local or UTC.
    """

    impl = DateTime
    cache_ok = True
    #: Precision used by the DDL and by Alembic autogenerate comparison.
    fsp = 3

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.DATETIME(fsp=self.fsp))
        return dialect.type_descriptor(DateTime())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, datetime):
            msg = f"expected datetime, got {type(value).__name__}"
            raise TypeError(msg)
        if value.tzinfo is None:
            # Reject rather than assume: an accidental local-time value written
            # as UTC is a class of bug that is nearly impossible to detect later.
            msg = (
                "naive datetime is not allowed; attach a timezone "
                "(use datetime.now(UTC)) before persisting"
            )
            raise ValueError(msg)
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @property
    def python_type(self) -> type[datetime]:
        return datetime


# Backwards-friendly alias emphasising the UTC contract.
UTCDateTimeMS = DateTimeMS


class MoneyMinor(TypeDecorator[int]):
    """Money in **minor units** (cents) as ``BIGINT`` (spec §19).

    ``FLOAT``/``DOUBLE`` are forbidden for transactional money because binary
    floating point cannot represent 0.1 exactly; summing a cart in floats
    produces totals that fail `sum(items) == order.total`, which is exactly the
    invariant INV-006 protects.

    This decorator's job is to make the unit impossible to forget:
      * it refuses ``float`` outright, so a stray ``19.99`` raises immediately;
      * it accepts ``Decimal`` only if it is an exact whole number of minor
        units, so converting from a decimal source must be explicit;
      * it accepts ``int`` verbatim.
    """

    impl = BigInteger
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "mysql":
            return dialect.type_descriptor(mysql.BIGINT())
        return dialect.type_descriptor(BigInteger())

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if isinstance(value, bool):
            msg = "bool is not a valid money value"
            raise TypeError(msg)
        if isinstance(value, float):
            msg = (
                "float is forbidden for money (spec §19); pass integer minor units "
                "or an exact Decimal"
            )
            raise TypeError(msg)
        if isinstance(value, Decimal):
            if value != value.to_integral_value():
                msg = f"Decimal money must be a whole number of minor units, got {value}"
                raise ValueError(msg)
            return int(value)
        return int(value)

    @property
    def python_type(self) -> type[int]:
        return int


class PercentBasisPoints(TypeDecorator[int]):
    """A percentage stored as **basis points** (1% == 100 bps).

    Discount rates (spec §39 ``PERCENT_DISCOUNT``) must not be floats either,
    for the same reason money must not be: 12.5% is exactly 1250 bps, but
    ``0.125`` is not exactly representable. Integer basis points keep discount
    arithmetic reproducible and keep `sum(items) == order.total` exact.
    """

    impl = Integer
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if isinstance(value, float):
            msg = (
                "float is not allowed for a rate; pass integer basis points "
                "(e.g. 1250 for 12.5%)"
            )
            raise TypeError(msg)
        return int(value)


def money_to_display(amount_minor: int) -> str:
    """Render minor units as a fixed 2-decimal string (never a float).

    Display only. Formatting happens once, at the presentation edge, so no
    business calculation ever touches a decimal string.
    """
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        msg = f"money must be int minor units, got {type(amount_minor).__name__}"
        raise TypeError(msg)
    sign = "-" if amount_minor < 0 else ""
    whole, frac = divmod(abs(amount_minor), 100)
    return f"{sign}{whole}.{frac:02d}"


def display_to_money(value: str | Decimal) -> int:
    """Parse a decimal string into minor units, rejecting sub-cent precision.

    Used at integration boundaries (payment provider responses, CSV imports).
    It deliberately raises on more than two decimal places rather than silently
    rounding, because silently discarding a fraction of a cent is how ledgers
    stop balancing.
    """
    decimal_value = Decimal(value)
    scaled = decimal_value.scaleb(2)
    if scaled != scaled.to_integral_value():
        msg = f"value {value!r} has sub-cent precision and cannot be represented exactly"
        raise ValueError(msg)
    return int(scaled)


# Re-exported for the numeric helper used by analytics aggregations, where a
# float ratio is acceptable because it never touches a ledger.
AggregateRatio = Numeric
_ = func  # kept for future server-side defaults; silences unused import linters
