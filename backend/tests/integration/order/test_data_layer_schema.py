"""Phase 4 data layer - the DDL is verified by reading it back, not by trust.

Marker: ``integration``. Real MySQL, because every property under test is a
*database* property: a ``CHECK`` constraint, a unique index, a column's
signedness. A SQLite pass would prove none of it - SQLite would not even have
created the constraints the models declare, and it has no unsigned integers to
get wrong.

## Why this file exists separately from the order workflow tests

``HANDOFF.md`` section 6 records two MySQL/Alembic traps that both hide in the
gap between "the model says X" and "the database does X":

* **Alembic does not autogenerate CHECK-constraint changes on MySQL.** A
  constraint edit produces an empty migration; the database silently keeps the old
  rule while the models describe the new one.
* **``compare_type`` does not distinguish ``BIGINT`` from ``BIGINT UNSIGNED``.**
  Same class of silence, one layer down.

So the assertions here are made against ``information_schema`` - the same place
the design (``docs/architecture/PHASE4_DESIGN.md`` section 1) requires the
migration to be hand-verified - and against real ``INSERT`` attempts that must be
*rejected*. "The migration exited 0" is not evidence that the schema is right.

## The signedness assertions are not pedantry

``ck_orders_payable_consistent`` performs subtraction
(``payable = original - promotion - coupon + shipping``). MySQL promotes a mixed
signed/unsigned comparison to UNSIGNED, so had any operand been
``BIGINT UNSIGNED`` the constraint would evaluate in unsigned arithmetic and
reject rows that are arithmetically correct - which is exactly how FG-09 lost an
hour on the stock ledger. ``test_money_columns_are_signed_bigint`` is the guard
that keeps the trap out of the order tables.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.modules.catalog.models import Product, ProductSku
from app.modules.identity.models import User, UserAddress
from app.modules.identity.security import hash_password
from app.modules.inventory.models import Inventory, Warehouse
from app.modules.order.enums import (
    AFTER_SALE_STATUSES,
    FULFILLMENT_STATUSES,
    OPERATOR_TYPES,
    ORDER_STATUSES,
    PAYMENT_STATUSES,
)
from app.modules.order.models import Order, OrderItem, OrderStatusLog
from app.shared.db.session import configure_database, get_session_factory

pytestmark = pytest.mark.integration

PHASE4_TABLES = ("orders", "order_items", "order_status_logs", "idempotency_records")

#: Every CHECK constraint the design freezes, by table. Written out as literals
#: rather than derived from the models, because the point of the assertion is to
#: catch a disagreement between the models and the database - deriving both sides
#: from the same source would make it vacuous.
EXPECTED_CHECK_CONSTRAINTS: dict[str, set[str]] = {
    "orders": {
        "ck_orders_amounts_non_negative",
        "ck_orders_payable_consistent",
        "ck_orders_status_valid",
        "ck_orders_payment_status_valid",
        "ck_orders_fulfillment_status_valid",
        "ck_orders_after_sale_status_valid",
    },
    "order_items": {
        "ck_order_items_quantity_positive",
        "ck_order_items_amounts_non_negative",
        "ck_order_items_allocated_consistent",
        "ck_order_items_payable_consistent",
        "ck_order_items_after_sale_status_valid",
    },
    "order_status_logs": {
        "ck_order_status_logs_from_status_valid",
        "ck_order_status_logs_to_status_valid",
        "ck_order_status_logs_operator_valid",
    },
    "idempotency_records": {"ck_idempotency_records_status_valid"},
}

#: Money columns that the payable-consistency CHECK subtracts. All must be signed.
MONEY_COLUMNS = (
    "original_amount",
    "promotion_discount_amount",
    "coupon_discount_amount",
    "shipping_amount",
    "payable_amount",
    "paid_amount",
    "refunded_amount",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def _engine():
    return configure_database()


@pytest.fixture
def db(_engine):
    """A session inside a transaction that is rolled back afterwards.

    A real connection, so MySQL's own constraints decide the outcome, and a
    rollback so the suite is re-runnable without reseeding.
    """
    connection = _engine.connect()
    transaction = connection.begin()
    session = get_session_factory()(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def seeded(db) -> dict[str, int]:
    """The parent rows an order needs: seller, buyer, product, SKU, warehouse, stock.

    Built through the ORM rather than raw SQL deliberately. The columns under test
    are the *order* tables' constraints, so the parents should be created the way
    the application creates them - a hand-written ``INSERT`` for ``users`` that
    forgot a Python-side default would fail for a reason that has nothing to do
    with Phase 4, and the failure would be indistinguishable from a real defect.
    """
    merchant_id = int(
        db.execute(
            text(
                "INSERT INTO merchants (code, name, status, commission_bps, created_at, updated_at) "
                "VALUES ('T-DATA', 'Data Layer Merchant', 'ACTIVE', 0, NOW(3), NOW(3))"
            )
        ).lastrowid
    )

    user = User(
        merchant_id=merchant_id,
        username="data-layer-buyer",
        password_hash=hash_password("Correct-Horse-Battery-9"),
        display_name="Data Layer Buyer",
        user_type="CONSUMER",
        status="ACTIVE",
    )
    db.add(user)
    db.flush()

    address = UserAddress(
        merchant_id=merchant_id,
        user_id=user.id,
        receiver_name="Test Receiver",
        receiver_phone="13800000000",
        province="Guangdong",
        city="Shenzhen",
        district="Nanshan",
        detail="1 Test Road",
        tag="HOME",
    )
    db.add(address)

    product = Product(
        merchant_id=merchant_id,
        product_no="P-DATA-1",
        slug="data-layer-product",
        name="Data Layer Product",
        status="PUBLISHED",
    )
    db.add(product)
    db.flush()

    sku = ProductSku(
        merchant_id=merchant_id,
        product_id=product.id,
        sku_no="S-DATA-1",
        sku_code="data-layer-sku",
        name="Data Layer SKU",
        price_amount=299900,
    )
    db.add(sku)

    warehouse = Warehouse(merchant_id=merchant_id, code="MAIN", name="Main", status="ACTIVE")
    db.add(warehouse)
    db.flush()

    inventory = Inventory(
        merchant_id=merchant_id,
        warehouse_id=warehouse.id,
        sku_id=sku.id,
        available_qty=100,
        locked_qty=0,
    )
    db.add(inventory)
    db.flush()

    return {
        "merchant_id": merchant_id,
        "user_id": int(user.id),
        "address_id": int(address.id),
        "product_id": int(product.id),
        "sku_id": int(sku.id),
        "warehouse_id": int(warehouse.id),
    }


# ---------------------------------------------------------------------------
# Raw-SQL helpers
#
# Raw SQL on purpose (the project's own convention for integration tests): the
# constraints must hold against a client that does not go through the ORM, which
# is the only version of "the database enforces this" worth asserting.
# ---------------------------------------------------------------------------
_ORDER_COLUMNS = (
    "user_id, merchant_id, order_no, client_request_id, request_hash, "
    "order_status, payment_status, fulfillment_status, after_sale_status, "
    "original_amount, promotion_discount_amount, coupon_discount_amount, "
    "shipping_amount, payable_amount, paid_amount, refunded_amount, "
    "address_id, receiver_name, receiver_phone, address_snapshot, "
    "item_count, first_item_name, created_at, updated_at"
)


def insert_order(db, seeded: dict[str, int], **overrides: Any) -> int:
    """Insert one order with valid defaults; the caller overrides what it tests.

    ``payable_amount`` is *derived* from the other amounts unless the caller
    overrides it explicitly, so the default row satisfies
    ``ck_orders_payable_consistent`` without every test having to restate the
    arithmetic. Passing ``payable_amount=...`` is how the consistency tests
    deliberately break it.

    Values are bound parameters, never interpolated, so a test can put arbitrary
    text into a column without turning the suite into an injection demo.
    """
    values: dict[str, Any] = {
        "user_id": seeded["user_id"],
        "merchant_id": seeded["merchant_id"],
        "order_no": "NV20260923000001",
        "client_request_id": "crid-data-layer-1",
        "request_hash": "0" * 64,
        "order_status": "PENDING_PAYMENT",
        "payment_status": "UNPAID",
        "fulfillment_status": "UNFULFILLED",
        "after_sale_status": "NONE",
        "original_amount": 299900,
        "promotion_discount_amount": 0,
        "coupon_discount_amount": 0,
        "shipping_amount": 0,
        "paid_amount": 0,
        "refunded_amount": 0,
        "address_id": seeded["address_id"],
        "receiver_name": "Test Receiver",
        "receiver_phone": "13800000000",
        "address_snapshot": '{"province": "Guangdong"}',
        "item_count": 1,
        "first_item_name": "Data Layer Product",
    }
    values.update(overrides)
    values.setdefault(
        "payable_amount",
        values["original_amount"]
        - values["promotion_discount_amount"]
        - values["coupon_discount_amount"]
        + values["shipping_amount"],
    )

    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    result = db.execute(
        text(
            f"INSERT INTO orders ({columns}, created_at, updated_at) "
            f"VALUES ({placeholders}, NOW(3), NOW(3))"
        ),
        values,
    )
    return int(result.lastrowid)


def insert_item(db, seeded: dict[str, int], order_id: int, **overrides: Any) -> int:
    """Insert one order line with valid defaults (INV-006-consistent by default)."""
    original = overrides.get("original_amount", 299900)
    promotion = overrides.get("promotion_discount_amount", 0)
    coupon = overrides.get("coupon_discount_amount", 0)
    allocated = overrides.get("allocated_discount_amount", promotion + coupon)
    values: dict[str, Any] = {
        "order_id": order_id,
        "warehouse_id": seeded["warehouse_id"],
        "product_id": seeded["product_id"],
        "sku_id": seeded["sku_id"],
        "product_name": "Data Layer Product",
        "sku_name": "Data Layer SKU",
        "unit_price": 299900,
        "quantity": 1,
        "original_amount": original,
        "promotion_discount_amount": promotion,
        "coupon_discount_amount": coupon,
        "allocated_discount_amount": allocated,
        "payable_amount": overrides.get("payable_amount", original - allocated),
        "refunded_amount": 0,
        "after_sale_status": "NONE",
    }
    values.update(overrides)

    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    result = db.execute(
        text(
            f"INSERT INTO order_items ({columns}, created_at, updated_at) "
            f"VALUES ({placeholders}, NOW(3), NOW(3))"
        ),
        values,
    )
    return int(result.lastrowid)


def insert_status_log(db, order_id: int, **overrides: Any) -> int:
    values: dict[str, Any] = {
        "order_id": order_id,
        "order_no": "NV20260923000001",
        "from_status": None,
        "to_status": "PENDING_PAYMENT",
        "reason": None,
        "operator_type": "SYSTEM",
        "operator_id": None,
        "trace_id": None,
    }
    values.update(overrides)
    columns = ", ".join(values)
    placeholders = ", ".join(f":{name}" for name in values)
    result = db.execute(
        text(
            f"INSERT INTO order_status_logs ({columns}, created_at, updated_at) "
            f"VALUES ({placeholders}, NOW(3), NOW(3))"
        ),
        values,
    )
    return int(result.lastrowid)


def _raises_rejection(db, statement: str, params: dict[str, Any] | None = None) -> str:
    """Run a statement the database must reject; return the error text.

    ## Why ``OperationalError`` and not just ``IntegrityError``

    MySQL 8.4 reports a violated ``CHECK`` as **errno 3819**, which SQLAlchemy
    maps to ``OperationalError`` - *not* to ``IntegrityError``, which is what a
    unique-key violation produces (errno 1062). Catching only ``IntegrityError``
    made twelve of these tests fail for the wrong reason: the constraint *did*
    fire, and the test still went red. Both classes are caught here, and the
    caller asserts on the constraint name in the message, so the specific rule
    is still what is being verified.

    ## Why a SAVEPOINT

    The failed statement must not take the fixture's transaction with it. Once any
    statement on a connection has failed, *subsequent* statements on MySQL/
    SQLAlchemy raise until the transaction is cleared - so a helper that called
    ``rollback()`` would end the outer transaction the ``db`` fixture owns and
    every later assertion in the test would fail with "transaction already
    deassociated from connection" rather than testing anything.

    Wrapping the attempt in ``begin_nested()`` confines the failure to a savepoint
    that is rolled back on its own, leaving the outer transaction healthy. This is
    the same mechanism ``IdempotencyRepository.insert_in_progress`` uses to
    tolerate a lost race, so both paths are exercised by this suite.
    """
    with pytest.raises((IntegrityError, OperationalError)) as caught, db.begin_nested():
        db.execute(text(statement), params or {})
    # Return both: the message carries the constraint NAME, the number carries the
    # MySQL errno - and the number is what differs between a CHECK and a unique key.
    orig = getattr(caught.value, 'orig', None)
    errno = orig.args[0] if orig is not None and orig.args else None
    return f'[errno {errno}] {caught.value}'


MYSQL_CHECK_VIOLATION = 3819
MYSQL_DUPLICATE_KEY = 1062
MYSQL_FOREIGN_KEY_VIOLATION = 1451


def _assert_check_violation(message: str, constraint: str) -> None:
    """Assert the rejection was a CHECK (errno 3819) naming `constraint`.

    Two separate assertions on purpose, because they can fail independently:

    * the **errno** proves *which* database mechanism refused the row. A
      violated CHECK is errno 3819 and arrives as `OperationalError`; a
      duplicate unique key is 1062 and a violated foreign key is 1451, both as
      `IntegrityError`. Asserting only on the message would let a test that
      means "the CHECK refused this" pass because a *different* mechanism did.
    * the **constraint name** proves the *specific rule* that fired, since a
      row can violate more than one and MySQL reports whichever it evaluates
      first.
    """
    assert f"[errno {MYSQL_CHECK_VIOLATION}]" in message, message
    assert constraint in message, message
    assert "Check constraint" in message, message


_PHASE4_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "20260923_1340_a7c4e91b2d63_phase4_orders_and_idempotency.py"
)


def _load_phase4_migration():
    """Import this migration file by path.

    By path rather than by module name because the filename starts with a digit
    and so is not a valid dotted module reference.
    """
    spec = importlib.util.spec_from_file_location("phase4_orders_migration", _PHASE4_MIGRATION_PATH)
    assert spec is not None and spec.loader is not None, f"cannot load {_PHASE4_MIGRATION_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The schema itself
# ---------------------------------------------------------------------------
class TestSchemaLanded:
    """Read ``information_schema`` back - the design's hand-verification requirement."""

    def test_all_four_tables_exist(self, db) -> None:
        found = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT TABLE_NAME FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() "
                    f"AND TABLE_NAME IN {PHASE4_TABLES}"
                )
            ).all()
        }
        assert found == set(PHASE4_TABLES)

    @pytest.mark.parametrize(("table", "expected"), sorted(EXPECTED_CHECK_CONSTRAINTS.items()))
    def test_check_constraints_present_with_frozen_names(
        self, db, table: str, expected: set[str]
    ) -> None:
        """Alembic does not autogenerate CHECK changes on MySQL, so this is the check.

        ``information_schema.CHECK_CONSTRAINTS`` has no ``TABLE_NAME`` column in
        MySQL 8.4, so the join through ``TABLE_CONSTRAINTS`` is required - another
        entry in the ``HANDOFF.md`` section 6 list.
        """
        found = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT tc.CONSTRAINT_NAME "
                    "FROM information_schema.TABLE_CONSTRAINTS tc "
                    "JOIN information_schema.CHECK_CONSTRAINTS cc "
                    "  ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA "
                    " AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME "
                    "WHERE tc.TABLE_SCHEMA = DATABASE() AND tc.CONSTRAINT_TYPE = 'CHECK' "
                    "  AND tc.TABLE_NAME = :table"
                ),
                {"table": table},
            ).all()
        }
        assert found == expected, f"{table}: missing={expected - found} unexpected={found - expected}"

    @pytest.mark.parametrize("column", MONEY_COLUMNS)
    def test_money_columns_are_signed_bigint(self, db, column: str) -> None:
        """Signed, so the subtraction inside ``ck_orders_payable_consistent`` is valid.

        Unsigned here would make ``0 - 1`` overflow and reject a correct row (the
        FG-09 trap). ``COLUMN_TYPE`` is asserted exactly - ``bigint`` means signed
        and ``bigint unsigned`` does not.
        """
        column_type = db.execute(
            text(
                "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders' "
                "AND COLUMN_NAME = :column"
            ),
            {"column": column},
        ).scalar_one()
        assert column_type == "bigint"

    def test_order_item_money_columns_are_signed_bigint(self, db) -> None:
        rows = db.execute(
            text(
                "SELECT COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'order_items' "
                "AND (COLUMN_NAME LIKE '%amount%' OR COLUMN_NAME = 'unit_price')"
            )
        ).all()
        assert rows, "order_items has no amount columns - did the table get created?"
        for name, column_type in rows:
            assert column_type == "bigint", f"order_items.{name} is {column_type}, expected bigint"

    def test_money_columns_are_never_float_or_double(self, db) -> None:
        """Spec section 19: FLOAT/DOUBLE for transactional money is forbidden.

        Asserted separately from the exact-type check above so a future column
        added as ``DOUBLE`` fails with a message about money rather than with a
        message about signedness.
        """
        offenders = db.execute(
            text(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                f"AND TABLE_NAME IN {PHASE4_TABLES} "
                "AND (COLUMN_NAME LIKE '%amount%' OR COLUMN_NAME = 'unit_price') "
                "AND DATA_TYPE IN ('float', 'double', 'decimal')"
            )
        ).all()
        assert offenders == []

    def test_primary_keys_are_unsigned(self, db) -> None:
        for table in PHASE4_TABLES:
            column_type = db.execute(
                text(
                    "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table AND COLUMN_NAME = 'id'"
                ),
                {"table": table},
            ).scalar_one()
            assert column_type == "bigint unsigned", f"{table}.id is {column_type}"

    def test_timestamps_have_millisecond_precision(self, db) -> None:
        """``DATETIME(3)``, because whole-second precision cannot order audit rows.

        ``created_at`` and ``updated_at`` on a row written in one transaction would
        otherwise be identical to the second, and "which came first" is exactly
        what the status log is read to answer.
        """
        rows = db.execute(
            text(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() "
                f"AND TABLE_NAME IN {PHASE4_TABLES} "
                "AND COLUMN_NAME IN ('created_at','updated_at','paid_at','completed_at',"
                "'cancelled_at','closed_at','expires_at') "
                "AND DATA_TYPE = 'datetime'"
            )
        ).all()
        assert rows
        for table, column, column_type in rows:
            assert column_type == "datetime(3)", f"{table}.{column} is {column_type}"

    def test_status_columns_are_varchar_not_enum(self, db) -> None:
        """``VARCHAR`` + Python enum (spec section 19).

        A MySQL ``ENUM`` would make adding a state a table rewrite, and would put
        the vocabulary in the database rather than in reviewable Python.
        """
        for table, column in (
            ("orders", "order_status"),
            ("orders", "payment_status"),
            ("orders", "fulfillment_status"),
            ("orders", "after_sale_status"),
            ("order_items", "after_sale_status"),
            ("order_status_logs", "to_status"),
            ("idempotency_records", "status"),
        ):
            data_type = db.execute(
                text(
                    "SELECT DATA_TYPE FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
                    "AND COLUMN_NAME = :column"
                ),
                {"table": table, "column": column},
            ).scalar_one()
            assert data_type == "varchar", f"{table}.{column} is {data_type}"

    def test_unique_indexes_exist(self, db) -> None:
        """The two idempotency guards and the per-SKU line, as real unique indexes."""
        rows = db.execute(
            text(
                "SELECT TABLE_NAME, INDEX_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) "
                "FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() AND NON_UNIQUE = 0 "
                f"AND TABLE_NAME IN {PHASE4_TABLES} "
                "GROUP BY TABLE_NAME, INDEX_NAME"
            )
        ).all()
        found = {(table, tuple(columns.split(","))) for table, _name, columns in rows}
        assert ("orders", ("user_id", "client_request_id")) in found
        assert ("orders", ("merchant_id", "order_no")) in found
        assert ("order_items", ("order_id", "sku_id")) in found
        assert ("idempotency_records", ("scope", "idempotency_key")) in found

    def test_foreign_keys_are_restrict(self, db) -> None:
        """RESTRICT everywhere.

        Not a preference: MySQL errno 3823 rejects a column that carries a CHECK
        and sits in a foreign key with a *mutating* referential action. RESTRICT is
        also the correct policy - deleting a buyer must not erase their orders.
        """
        rows = db.execute(
            text(
                "SELECT k.TABLE_NAME, k.CONSTRAINT_NAME, k.REFERENCED_TABLE_NAME, r.DELETE_RULE "
                "FROM information_schema.KEY_COLUMN_USAGE k "
                "JOIN information_schema.REFERENTIAL_CONSTRAINTS r "
                "  ON r.CONSTRAINT_NAME = k.CONSTRAINT_NAME "
                " AND r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA "
                "WHERE k.TABLE_SCHEMA = DATABASE() AND k.REFERENCED_TABLE_NAME IS NOT NULL "
                f"AND k.TABLE_NAME IN {PHASE4_TABLES}"
            )
        ).all()
        assert rows, "no foreign keys found on the Phase 4 tables"
        for table, name, referenced, delete_rule in rows:
            assert delete_rule == "RESTRICT", f"{table}.{name} -> {referenced} is ON DELETE {delete_rule}"

    def test_coupon_id_has_no_foreign_key_yet(self, db) -> None:
        """Deliberate (design section 4.1): Phase 6 creates ``coupons``.

        Asserted so that the deliberate omission is not "fixed" by someone
        assuming it was an oversight, and so Phase 6 knows the constraint is what
        changes.
        """
        count = db.execute(
            text(
                "SELECT COUNT(*) FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders' "
                "AND COLUMN_NAME = 'coupon_id' AND REFERENCED_TABLE_NAME IS NOT NULL"
            )
        ).scalar_one()
        assert count == 0


# ---------------------------------------------------------------------------
# The constraints actually bite
# ---------------------------------------------------------------------------
class TestOrderCheckConstraintsEnforced:
    def test_valid_order_inserts(self, db, seeded) -> None:
        """Positive control.

        Without this, every negative assertion below would also pass against a
        table whose constraints reject *everything*, which would be a very
        confident way to ship a broken schema.
        """
        order_id = insert_order(db, seeded)
        assert order_id > 0

    def test_payable_must_equal_the_arithmetic(self, db, seeded) -> None:
        message = _raises_rejection(
            db,
            f"INSERT INTO orders ({_ORDER_COLUMNS}) VALUES ("
            ":user_id, :merchant_id, 'NV-BAD-1', 'crid-bad-1', :hash, "
            "'PENDING_PAYMENT', 'UNPAID', 'UNFULFILLED', 'NONE', "
            "299900, 10000, 0, 0, 299900, 0, 0, NULL, 'R', '13800000000', '{}', 1, 'N', NOW(3), NOW(3))",
            {
                "user_id": seeded["user_id"],
                "merchant_id": seeded["merchant_id"],
                "hash": "0" * 64,
            },
        )
        _assert_check_violation(message, "ck_orders_payable_consistent")

    def test_negative_amount_is_rejected(self, db, seeded) -> None:
        """A negative money column is refused by ``ck_orders_amounts_non_negative``.

        Asserted with an ``UPDATE`` on a row that *does* exist, because the
        interesting case is a later phase trying to write a negative amount onto a
        real order (a bad refund, a mis-signed adjustment) rather than a bad
        insert.
        """
        order_id = insert_order(db, seeded)
        message = _raises_rejection(
            db,
            "UPDATE orders SET payable_amount = -1 WHERE id = :order_id",
            {"order_id": order_id},
        )
        _assert_check_violation(message, "ck_orders_amounts_non_negative")

    def test_unknown_order_status_is_rejected(self, db, seeded) -> None:
        message = _raises_rejection(
            db,
            "INSERT INTO orders (user_id, merchant_id, order_no, client_request_id, request_hash, "
            "order_status, payment_status, fulfillment_status, after_sale_status, original_amount, "
            "payable_amount, receiver_name, receiver_phone, address_snapshot, item_count, "
            "first_item_name, created_at, updated_at) VALUES ("
            ":user_id, :merchant_id, 'NV-BAD-2', 'crid-bad-2', :hash, "
            "'SHIPPED', 'UNPAID', 'UNFULFILLED', 'NONE', 100, 100, 'R', '13800000000', '{}', 1, 'N', "
            "NOW(3), NOW(3))",
            {
                "user_id": seeded["user_id"],
                "merchant_id": seeded["merchant_id"],
                "hash": "0" * 64,
            },
        )
        _assert_check_violation(message, "ck_orders_status_valid")

    def test_shipped_is_not_a_valid_order_status(self, db, seeded) -> None:
        """``SHIPPED`` belongs to ``fulfillment_status``, not to ``order_status``.

        Spec section 31: shipping never changes the order status. That is not only
        a code rule - the vocabulary makes it unrepresentable, which is the version
        of the rule that survives a careless ``UPDATE``.
        """
        assert "SHIPPED" not in ORDER_STATUSES
        assert "SHIPPED" in FULFILLMENT_STATUSES

    def test_unknown_payment_status_is_rejected(self, db, seeded) -> None:
        message = _raises_rejection(
            db,
            "INSERT INTO orders (user_id, merchant_id, order_no, client_request_id, request_hash, "
            "order_status, payment_status, fulfillment_status, after_sale_status, original_amount, "
            "payable_amount, receiver_name, receiver_phone, address_snapshot, item_count, "
            "first_item_name, created_at, updated_at) VALUES ("
            ":user_id, :merchant_id, 'NV-BAD-3', 'crid-bad-3', :hash, "
            "'PENDING_PAYMENT', 'SETTLED', 'UNFULFILLED', 'NONE', 100, 100, 'R', '13800000000', '{}', 1, "
            "'N', NOW(3), NOW(3))",
            {
                "user_id": seeded["user_id"],
                "merchant_id": seeded["merchant_id"],
                "hash": "0" * 64,
            },
        )
        _assert_check_violation(message, "ck_orders_payment_status_valid")

    def test_duplicate_client_request_id_for_same_user_is_rejected(self, db, seeded) -> None:
        """The second idempotency guard is a constraint, not a convention."""
        insert_order(db, seeded)
        message = _raises_rejection(
            db,
            "INSERT INTO orders (user_id, merchant_id, order_no, client_request_id, request_hash, "
            "order_status, payment_status, fulfillment_status, after_sale_status, original_amount, "
            "payable_amount, receiver_name, receiver_phone, address_snapshot, item_count, "
            "first_item_name, created_at, updated_at) VALUES ("
            ":user_id, :merchant_id, 'NV-DIFFERENT', 'crid-data-layer-1', :hash, "
            "'PENDING_PAYMENT', 'UNPAID', 'UNFULFILLED', 'NONE', 100, 100, 'R', '13800000000', '{}', 1, "
            "'N', NOW(3), NOW(3))",
            {
                "user_id": seeded["user_id"],
                "merchant_id": seeded["merchant_id"],
                "hash": "0" * 64,
            },
        )
        assert f"[errno {MYSQL_DUPLICATE_KEY}]" in message, message
        assert "uq_orders_user_client_request" in message

    def test_duplicate_order_no_for_same_merchant_is_rejected(self, db, seeded) -> None:
        insert_order(db, seeded)
        message = _raises_rejection(
            db,
            "INSERT INTO orders (user_id, merchant_id, order_no, client_request_id, request_hash, "
            "order_status, payment_status, fulfillment_status, after_sale_status, original_amount, "
            "payable_amount, receiver_name, receiver_phone, address_snapshot, item_count, "
            "first_item_name, created_at, updated_at) VALUES ("
            ":user_id, :merchant_id, 'NV20260923000001', 'crid-different', :hash, "
            "'PENDING_PAYMENT', 'UNPAID', 'UNFULFILLED', 'NONE', 100, 100, 'R', '13800000000', '{}', 1, "
            "'N', NOW(3), NOW(3))",
            {
                "user_id": seeded["user_id"],
                "merchant_id": seeded["merchant_id"],
                "hash": "0" * 64,
            },
        )
        assert f"[errno {MYSQL_DUPLICATE_KEY}]" in message, message
        assert "uq_orders_merchant_order_no" in message

    def test_orders_are_immutable_snapshots_by_column_set(self, db) -> None:
        """No column on ``orders`` is a pointer to live catalogue data.

        INV-014 as a structural property: the snapshot columns are *values*
        (names, prices, an address dict), and the only catalogue-identifying
        columns are ``product_id``/``sku_id``-style ids which are never rendered.
        A future ``product_slug`` or ``current_price`` column would be a regression
        and this test is where it should be noticed.
        """
        columns = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders'"
                )
            ).all()
        }
        assert {"receiver_name", "receiver_phone", "address_snapshot"} <= columns
        forbidden = {"product_name", "sku_name", "unit_price", "product_slug", "current_price"}
        assert not (forbidden & columns), f"orders must not denormalise catalogue data: {forbidden & columns}"


class TestOrderItemCheckConstraintsEnforced:
    def test_valid_item_inserts(self, db, seeded) -> None:
        order_id = insert_order(db, seeded)
        assert insert_item(db, seeded, order_id) > 0

    def test_quantity_must_be_positive(self, db, seeded) -> None:
        order_id = insert_order(db, seeded)
        message = _raises_rejection(
            db,
            "INSERT INTO order_items (order_id, warehouse_id, product_id, sku_id, product_name, "
            "sku_name, unit_price, quantity, original_amount, payable_amount, created_at, updated_at) "
            "VALUES (:order_id, :warehouse_id, :product_id, :sku_id, 'P', 'S', 100, 0, 0, 0, NOW(3), NOW(3))",
            {
                "order_id": order_id,
                "warehouse_id": seeded["warehouse_id"],
                "product_id": seeded["product_id"],
                "sku_id": seeded["sku_id"],
            },
        )
        _assert_check_violation(message, "ck_order_items_quantity_positive")

    def test_allocated_discount_must_equal_promotion_plus_coupon(self, db, seeded) -> None:
        """One leg of INV-006, pinned per row."""
        order_id = insert_order(db, seeded)
        message = _raises_rejection(
            db,
            "INSERT INTO order_items (order_id, warehouse_id, product_id, sku_id, product_name, "
            "sku_name, unit_price, quantity, original_amount, promotion_discount_amount, "
            "coupon_discount_amount, allocated_discount_amount, payable_amount, created_at, updated_at) "
            "VALUES (:order_id, :warehouse_id, :product_id, :sku_id, 'P', 'S', 100, 1, 100, 10, 5, 99, 1, "
            "NOW(3), NOW(3))",
            {
                "order_id": order_id,
                "warehouse_id": seeded["warehouse_id"],
                "product_id": seeded["product_id"],
                "sku_id": seeded["sku_id"],
            },
        )
        _assert_check_violation(message, "ck_order_items_allocated_consistent")

    def test_item_payable_must_equal_original_minus_allocated(self, db, seeded) -> None:
        """Only ``ck_order_items_payable_consistent`` is violated here.

        ``allocated = promotion + coupon`` is satisfied (10 = 10 + 0) so that the
        *right* constraint fires: MySQL names whichever violated CHECK it evaluates
        first, and a row that breaks two rules tests whichever one the engine
        happens to pick rather than the rule the test is named after.
        """
        order_id = insert_order(db, seeded)
        message = _raises_rejection(
            db,
            "INSERT INTO order_items (order_id, warehouse_id, product_id, sku_id, product_name, "
            "sku_name, unit_price, quantity, original_amount, promotion_discount_amount, "
            "allocated_discount_amount, payable_amount, created_at, updated_at) "
            "VALUES (:order_id, :warehouse_id, :product_id, :sku_id, 'P', 'S', 100, 1, 100, 10, 10, 95, "
            "NOW(3), NOW(3))",
            {
                "order_id": order_id,
                "warehouse_id": seeded["warehouse_id"],
                "product_id": seeded["product_id"],
                "sku_id": seeded["sku_id"],
            },
        )
        _assert_check_violation(message, "ck_order_items_payable_consistent")

    def test_duplicate_sku_in_one_order_is_rejected(self, db, seeded) -> None:
        """Lines are merged before pricing, so per-SKU uniqueness is an invariant."""
        order_id = insert_order(db, seeded)
        insert_item(db, seeded, order_id)
        message = _raises_rejection(
            db,
            "INSERT INTO order_items (order_id, warehouse_id, product_id, sku_id, product_name, "
            "sku_name, unit_price, quantity, original_amount, allocated_discount_amount, "
            "payable_amount, created_at, updated_at) "
            "VALUES (:order_id, :warehouse_id, :product_id, :sku_id, 'P', 'S', 100, 1, 100, 0, 100, "
            "NOW(3), NOW(3))",
            {
                "order_id": order_id,
                "warehouse_id": seeded["warehouse_id"],
                "product_id": seeded["product_id"],
                "sku_id": seeded["sku_id"],
            },
        )
        assert f"[errno {MYSQL_DUPLICATE_KEY}]" in message, message
        assert "uq_order_items_order_sku" in message

    def test_negative_allocated_discount_is_rejected(self, db, seeded) -> None:
        """A discount cannot be negative - that would be a surcharge in disguise.

        ``allocated = promotion + coupon`` holds (-10 = -10 + 0) and
        ``payable = original - allocated`` holds (110 = 100 - -10), so the only
        rule this row breaks is ``ck_order_items_amounts_non_negative``.
        """
        order_id = insert_order(db, seeded)
        message = _raises_rejection(
            db,
            "INSERT INTO order_items (order_id, warehouse_id, product_id, sku_id, product_name, "
            "sku_name, unit_price, quantity, original_amount, promotion_discount_amount, "
            "allocated_discount_amount, payable_amount, created_at, updated_at) "
            "VALUES (:order_id, :warehouse_id, :product_id, :sku_id, 'P', 'S', 100, 1, 100, -10, -10, 110, "
            "NOW(3), NOW(3))",
            {
                "order_id": order_id,
                "warehouse_id": seeded["warehouse_id"],
                "product_id": seeded["product_id"],
                "sku_id": seeded["sku_id"],
            },
        )
        _assert_check_violation(message, "ck_order_items_amounts_non_negative")


class TestStatusLogCheckConstraintsEnforced:
    def test_creation_log_with_null_from_status_is_allowed(self, db, seeded) -> None:
        """``None -> PENDING_PAYMENT`` is the one legal NULL ``from_status``."""
        order_id = insert_order(db, seeded)
        assert insert_status_log(db, order_id, from_status=None, to_status="PENDING_PAYMENT") > 0

    def test_unknown_to_status_is_rejected(self, db, seeded) -> None:
        order_id = insert_order(db, seeded)
        message = _raises_rejection(
            db,
            "INSERT INTO order_status_logs (order_id, order_no, to_status, operator_type, created_at, "
            "updated_at) VALUES (:order_id, 'NV-X', 'SHIPPED', 'SYSTEM', NOW(3), NOW(3))",
            {"order_id": order_id},
        )
        _assert_check_violation(message, "ck_order_status_logs_to_status_valid")

    def test_unknown_from_status_is_rejected(self, db, seeded) -> None:
        order_id = insert_order(db, seeded)
        message = _raises_rejection(
            db,
            "INSERT INTO order_status_logs (order_id, order_no, from_status, to_status, operator_type, "
            "created_at, updated_at) VALUES (:order_id, 'NV-X', 'REFUNDED', 'CANCELLED', 'SYSTEM', "
            "NOW(3), NOW(3))",
            {"order_id": order_id},
        )
        _assert_check_violation(message, "ck_order_status_logs_from_status_valid")

    def test_unknown_operator_type_is_rejected(self, db, seeded) -> None:
        order_id = insert_order(db, seeded)
        message = _raises_rejection(
            db,
            "INSERT INTO order_status_logs (order_id, order_no, to_status, operator_type, created_at, "
            "updated_at) VALUES (:order_id, 'NV-X', 'PENDING_PAYMENT', 'ROBOT', NOW(3), NOW(3))",
            {"order_id": order_id},
        )
        _assert_check_violation(message, "ck_order_status_logs_operator_valid")


class TestIdempotencyTableConstraints:
    def test_valid_record_inserts(self, db) -> None:
        db.execute(
            text(
                "INSERT INTO idempotency_records (scope, idempotency_key, request_hash, status, "
                "created_at, updated_at) VALUES ('order:create', 'key-1', :hash, 'IN_PROGRESS', "
                "NOW(3), NOW(3))"
            ),
            {"hash": "0" * 64},
        )

    def test_duplicate_key_within_scope_is_rejected(self, db) -> None:
        """The unique index *is* the concurrency guard for create.

        Two concurrent creates with the same key both attempt this insert; exactly
        one succeeds, and the loser re-reads the winner's record and replays it
        instead of creating a second order.
        """
        params = {"hash": "0" * 64, "key": "key-duplicate"}
        db.execute(
            text(
                "INSERT INTO idempotency_records (scope, idempotency_key, request_hash, status, "
                "created_at, updated_at) VALUES ('order:create', :key, :hash, 'IN_PROGRESS', NOW(3), NOW(3))"
            ),
            params,
        )
        message = _raises_rejection(
            db,
            "INSERT INTO idempotency_records (scope, idempotency_key, request_hash, status, "
            "created_at, updated_at) VALUES ('order:create', :key, :hash, 'IN_PROGRESS', NOW(3), NOW(3))",
            params,
        )
        assert f"[errno {MYSQL_DUPLICATE_KEY}]" in message, message
        assert "uq_scope_idempotency_key" in message

    def test_same_key_in_a_different_scope_is_allowed(self, db) -> None:
        """Scopes are namespaces: a client may reuse one UUID for order and payment."""
        db.execute(
            text(
                "INSERT INTO idempotency_records (scope, idempotency_key, request_hash, status, "
                "created_at, updated_at) VALUES ('order:create', 'shared-key', :hash, 'COMPLETED', "
                "NOW(3), NOW(3))"
            ),
            {"hash": "0" * 64},
        )
        db.execute(
            text(
                "INSERT INTO idempotency_records (scope, idempotency_key, request_hash, status, "
                "created_at, updated_at) VALUES ('payment:callback', 'shared-key', :hash, 'COMPLETED', "
                "NOW(3), NOW(3))"
            ),
            {"hash": "0" * 64},
        )

    def test_unknown_status_is_rejected(self, db) -> None:
        message = _raises_rejection(
            db,
            "INSERT INTO idempotency_records (scope, idempotency_key, request_hash, status, "
            "created_at, updated_at) VALUES ('order:create', 'key-bad-status', :hash, 'PENDING', "
            "NOW(3), NOW(3))",
            {"hash": "0" * 64},
        )
        _assert_check_violation(message, "ck_idempotency_records_status_valid")


# ---------------------------------------------------------------------------
# The vocabularies cannot drift between Python and the migration
# ---------------------------------------------------------------------------
class TestMigrationVocabularyMatchesEnums:
    """The migration hard-codes its vocabularies; this is what keeps that honest.

    A migration must describe the schema *as of its revision*, so it cannot
    import the live enum (a Phase 6 member would retroactively appear in a Phase 4
    migration). The trade-off is duplication, and the price of duplication is
    these assertions.
    """

    def test_migration_vocabularies_equal_the_enums(self) -> None:
        """Load the migration file directly.

        Loaded by path with ``importlib`` rather than by module name: the filename
        begins with a digit, so it is not a valid dotted module reference. Loading
        the file this way also imports nothing from ``migrations`` itself, which
        keeps the test independent of how Alembic is wired.
        """
        module = _load_phase4_migration()
        assert tuple(module.ORDER_STATUSES) == ORDER_STATUSES
        assert tuple(module.PAYMENT_STATUSES) == PAYMENT_STATUSES
        assert tuple(module.FULFILLMENT_STATUSES) == FULFILLMENT_STATUSES
        assert tuple(module.AFTER_SALE_STATUSES) == AFTER_SALE_STATUSES
        assert tuple(module.OPERATOR_TYPES) == OPERATOR_TYPES

    def test_migration_declares_the_expected_revision(self) -> None:
        module = _load_phase4_migration()
        assert module.revision == "a7c4e91b2d63"
        # Chained onto the Phase 3 ledger fix; a wrong parent here would let two
        # heads exist, and `alembic upgrade head` would then be ambiguous.
        assert module.down_revision == "5cbebbc7b364"
        assert callable(module.upgrade)
        assert callable(module.downgrade)

    def test_migration_downgrade_does_not_predrop_indexes(self) -> None:
        """The regression this suite already caught once.

        ``downgrade()`` originally dropped each index before its table and failed
        with MySQL error 1553 ("Cannot drop index ... needed in a foreign key
        constraint"), because ``ix_order_status_logs_order_created`` serves
        ``fk_order_status_logs_order_id_orders``. ``DROP TABLE`` removes a table's
        indexes with it, so explicit drops are both unnecessary and wrong. MySQL's
        DDL is non-transactional, so that failure left the database
        half-downgraded - worth a cheap source-level guard, not just the
        round-trip run.

        The docstring is stripped before the check: it *quotes* the error and
        explains the rule, so matching the raw source would fail on its own
        explanation.
        """
        import inspect
        import re

        module = _load_phase4_migration()
        source = inspect.getsource(module.downgrade)
        body = re.sub(r'"""[\s\S]*?"""', "", source, count=1)
        assert "drop_index" not in body, (
            "downgrade() must not drop indexes separately: MySQL refuses to drop an "
            "index that backs a foreign key (errno 1553). DROP TABLE handles them."
        )
        for table in PHASE4_TABLES:
            assert f'op.drop_table("{table}")' in body, f"downgrade() does not drop {table}"
        # Children before parents, or the DROP itself fails on the foreign key.
        assert body.index('op.drop_table("order_items")') < body.index('op.drop_table("orders")')


# ---------------------------------------------------------------------------
# ORM defaults agree with the frozen create-time state (API contract section 14.5)
# ---------------------------------------------------------------------------
class TestOrmDefaults:
    """"An order is born in ``PENDING_PAYMENT``" must hold without passing statuses.

    The workflow sets these explicitly, but the defaults are the second line of
    defence: Phase 5's ``PaymentSuccessWorkflow`` and Phase 6's reconciliation both
    write orders, and neither should have to remember the birth state.

    Asserted after a real flush, not on a freshly constructed object. SQLAlchemy
    applies ``default=`` **at flush time**, so the first version of this test
    asserted ``OrderItem(...).after_sale_status == "NONE"`` against ``None`` and
    failed - which is the correct behaviour being misread, and exactly the sort of
    thing that only a run catches.
    """

    def test_new_order_defaults_to_the_frozen_birth_state(self, db, seeded) -> None:
        """Only the required columns are passed; everything else must default."""
        order = Order(
            user_id=seeded["user_id"],
            merchant_id=seeded["merchant_id"],
            order_no="NV20260923000009",
            client_request_id="crid-defaults",
            request_hash="0" * 64,
            receiver_name="R",
            receiver_phone="13800000000",
            address_snapshot={"province": "Guangdong"},
        )
        order.items = [
            OrderItem(
                warehouse_id=seeded["warehouse_id"],
                product_id=seeded["product_id"],
                sku_id=seeded["sku_id"],
                product_name="P",
                sku_name="S",
                unit_price=100,
                quantity=1,
                original_amount=100,
                payable_amount=100,
            )
        ]
        db.add(order)
        db.flush()

        assert order.order_status == "PENDING_PAYMENT"
        assert order.payment_status == "UNPAID"
        assert order.fulfillment_status == "UNFULFILLED"
        assert order.after_sale_status == "NONE"
        assert order.original_amount == 0
        assert order.payable_amount == 0
        assert order.paid_amount == 0
        assert order.refunded_amount == 0
        assert order.item_count == 0
        assert order.version == 1
        assert order.refundable_amount == 0

        item = order.items[0]
        assert item.after_sale_status == "NONE"
        assert item.allocated_discount_amount == 0
        assert item.refunded_amount == 0

    def test_server_defaults_apply_without_the_orm(self, db, seeded) -> None:
        """The same birth state from a raw ``INSERT`` that names none of them.

        This is the half that matters for a future migration or a bulk job: a row
        written without going through the ORM must still satisfy the status CHECKs
        on its own.
        """
        result = db.execute(
            text(
                "INSERT INTO orders (user_id, merchant_id, order_no, client_request_id, "
                "request_hash, original_amount, payable_amount, receiver_name, receiver_phone, "
                "address_snapshot, first_item_name, created_at, updated_at) VALUES "
                "(:user_id, :merchant_id, 'NV20260923000010', 'crid-server-defaults', :hash, "
                "0, 0, 'R', '13800000000', '{}', 'N', NOW(3), NOW(3))"
            ),
            {
                "user_id": seeded["user_id"],
                "merchant_id": seeded["merchant_id"],
                "hash": "0" * 64,
            },
        )
        row = db.execute(
            text(
                "SELECT order_status, payment_status, fulfillment_status, after_sale_status, "
                "item_count, version, paid_amount, refunded_amount "
                "FROM orders WHERE id = :order_id"
            ),
            {"order_id": int(result.lastrowid)},
        ).one()
        assert row[0] == "PENDING_PAYMENT"
        assert row[1] == "UNPAID"
        assert row[2] == "UNFULFILLED"
        assert row[3] == "NONE"
        assert row[4] == 0
        assert row[5] == 1
        assert row[6] == 0
        assert row[7] == 0

    def test_idempotency_record_server_default_is_in_progress(self, db) -> None:
        """A claim written without a status must not be silently replayable."""
        result = db.execute(
            text(
                "INSERT INTO idempotency_records (scope, idempotency_key, request_hash, "
                "created_at, updated_at) VALUES ('order:create', 'key-default-status', :hash, "
                "NOW(3), NOW(3))"
            ),
            {"hash": "0" * 64},
        )
        status = db.execute(
            text("SELECT status FROM idempotency_records WHERE id = :id"),
            {"id": int(result.lastrowid)},
        ).scalar_one()
        assert status == "IN_PROGRESS"


class TestIdempotencyRepositoryClaim:
    """The claim decision, including the loser of a race.

    The unique-index rejection above proves the *database* serialises concurrent
    claims. These tests prove the repository converts that rejection into the
    answer the workflow needs ("somebody else owns this key") instead of
    propagating an exception and losing the ability to replay.
    """

    def test_first_claim_succeeds_and_is_in_progress(self, db) -> None:
        from app.modules.order.repository import ORDER_CREATE_SCOPE, IdempotencyRepository

        record = IdempotencyRepository(db).insert_in_progress(
            scope=ORDER_CREATE_SCOPE,
            idempotency_key="claim-1",
            request_hash="a" * 64,
            resource_type="order",
        )
        assert record is not None
        assert record.id == int(record.id)
        assert record.is_in_progress
        assert not record.is_completed

    def test_second_claim_for_the_same_key_returns_none(self, db) -> None:
        """The loser of the race gets ``None``, not an exception.

        This is the behaviour the whole ``begin_nested`` dance exists for. Without
        the savepoint the ``IntegrityError`` would leave the session needing a
        rollback, and the workflow could not simply re-read the winner's record and
        replay it - which is the entire point of an idempotency key.
        """
        from app.modules.order.repository import ORDER_CREATE_SCOPE, IdempotencyRepository

        repository = IdempotencyRepository(db)
        first = repository.insert_in_progress(
            scope=ORDER_CREATE_SCOPE,
            idempotency_key="claim-loser",
            request_hash="a" * 64,
        )
        assert first is not None

        second = repository.insert_in_progress(
            scope=ORDER_CREATE_SCOPE,
            idempotency_key="claim-loser",
            request_hash="a" * 64,
        )
        assert second is None

    def test_the_outcome_of_a_lost_race_is_a_usable_session(self, db, seeded) -> None:
        """After losing the race, the same session must still be able to work.

        A ``SELECT`` is not enough to prove this - a session left in the
        "needs rollback" state fails on any statement at all. Writing an unrelated
        order afterwards is the assertion that the savepoint really confined the
        failure, which is what lets the workflow continue in the same transaction.
        """
        from app.modules.order.repository import ORDER_CREATE_SCOPE, IdempotencyRepository

        repository = IdempotencyRepository(db)
        repository.insert_in_progress(
            scope=ORDER_CREATE_SCOPE,
            idempotency_key="claim-after-loss",
            request_hash="a" * 64,
        )
        assert (
            repository.insert_in_progress(
                scope=ORDER_CREATE_SCOPE,
                idempotency_key="claim-after-loss",
                request_hash="a" * 64,
            )
            is None
        )

        # Still writable: the outer transaction survived the unique violation.
        assert insert_order(db, seeded) > 0

    def test_marking_completed_records_a_non_sensitive_summary(self, db) -> None:
        """Spec section 48: the snapshot is redacted before it is stored.

        This JSON lives outside the order's own redaction path, so whatever is put
        here is returned verbatim on a replay. The test pins the shape the design
        allows - identifiers and amounts only, never a receiver name or phone.
        """
        from app.modules.order.repository import ORDER_CREATE_SCOPE, IdempotencyRepository

        repository = IdempotencyRepository(db)
        record = repository.insert_in_progress(
            scope=ORDER_CREATE_SCOPE,
            idempotency_key="claim-complete",
            request_hash="a" * 64,
            resource_type="order",
        )
        assert record is not None

        repository.mark_completed(
            record,
            resource_type="order",
            resource_id=4242,
            response_code=0,
            response_snapshot={
                "order_no": "NV20260923000042",
                "payable_amount": 299900,
                "created_at": "2026-09-23T13:40:00.000Z",
            },
        )
        assert record.is_completed
        assert record.resource_id == 4242
        assert record.response_snapshot is not None

        forbidden = {"receiver_name", "receiver_phone", "address", "full_address", "detail"}
        assert not (forbidden & set(record.response_snapshot))

    def test_get_returns_the_stored_claim(self, db) -> None:
        from app.modules.order.repository import ORDER_CREATE_SCOPE, IdempotencyRepository

        repository = IdempotencyRepository(db)
        repository.insert_in_progress(
            scope=ORDER_CREATE_SCOPE,
            idempotency_key="claim-read-back",
            request_hash="b" * 64,
        )
        found = repository.get(scope=ORDER_CREATE_SCOPE, idempotency_key="claim-read-back")
        assert found is not None
        assert found.request_hash == "b" * 64
        # A different scope is a different key space.
        assert repository.get(scope="payment:callback", idempotency_key="claim-read-back") is None


class TestOrderRepositoryLookups:
    """Ownership is applied *in the query*, not after loading (IDOR defence)."""

    def test_lookup_by_order_no_scoped_to_the_owner(self, db, seeded) -> None:
        from app.modules.order.repository import OrderRepository

        order_id = insert_order(db, seeded)
        repository = OrderRepository(db)

        found = repository.get_by_order_no("NV20260923000001", user_id=seeded["user_id"])
        assert found is not None
        assert found.id == order_id

        # A different user asking for the same order number gets nothing at all -
        # which is how the API layer turns it into ORDER_NOT_FOUND (50003) rather
        # than 403. An existence oracle is a security defect, not a UX choice.
        assert repository.get_by_order_no("NV20260923000001", user_id=999999) is None

    def test_lookup_by_client_request_id(self, db, seeded) -> None:
        from app.modules.order.repository import OrderRepository

        order_id = insert_order(db, seeded)
        found = OrderRepository(db).get_by_client_request_id(
            user_id=seeded["user_id"], client_request_id="crid-data-layer-1"
        )
        assert found is not None
        assert found.id == order_id

    def test_for_update_locks_the_row_it_returns(self, db, seeded) -> None:
        """``SELECT ... FOR UPDATE`` compiles and returns the row on real MySQL.

        A green result here does not prove serialisation on its own (that needs two
        connections and is the workflow's concurrency test); it proves the lock
        clause is valid against this schema, which is the part that would fail
        loudly had a non-lockable construct been used.
        """
        from app.modules.order.repository import OrderRepository

        insert_order(db, seeded)
        locked = OrderRepository(db).get_by_order_no_for_update(
            "NV20260923000001", user_id=seeded["user_id"]
        )
        assert locked is not None

    def test_pagination_reports_total_beyond_the_page(self, db, seeded) -> None:
        """``total`` counts the filter, not the page.

        An envelope whose ``total`` equals ``page_size`` makes a "next page"
        control impossible to build correctly.
        """
        from app.modules.order.repository import OrderRepository

        for index in range(3):
            insert_order(
                db,
                seeded,
                order_no=f"NV2026092300010{index}",
                client_request_id=f"crid-page-{index}",
            )

        rows, total = OrderRepository(db).list_customer_orders(
            user_id=seeded["user_id"], page=1, page_size=2
        )
        assert len(rows) == 2
        assert total == 3

    def test_status_filter_is_applied(self, db, seeded) -> None:
        from app.modules.order.repository import OrderRepository

        insert_order(db, seeded, order_no="NV20260923000021", client_request_id="crid-f-pending")
        insert_order(
            db,
            seeded,
            order_no="NV20260923000022",
            client_request_id="crid-f-cancelled",
            order_status="CANCELLED",
            cancel_reason="test",
        )
        rows, total = OrderRepository(db).list_customer_orders(
            user_id=seeded["user_id"], order_status="CANCELLED"
        )
        assert [row.order_no for row in rows] == ["NV20260923000022"]
        assert total == 1


class TestOrderStatusLogRepository:
    """Append-only, and the ``order_no`` is copied rather than trusted."""

    def test_append_copies_the_order_number_from_the_order(self, db, seeded) -> None:
        from app.modules.order.repository import OrderRepository, OrderStatusLogRepository

        insert_order(db, seeded)
        order = OrderRepository(db).get_by_order_no("NV20260923000001", user_id=seeded["user_id"])
        assert order is not None

        log = OrderStatusLogRepository(db).append(
            order=order,
            from_status=None,
            to_status="PENDING_PAYMENT",
            operator_type="SYSTEM",
        )
        assert log.order_no == order.order_no
        assert log.from_status is None
        assert log.to_status == "PENDING_PAYMENT"
        assert log.operator_type == "SYSTEM"

    def test_history_is_ordered_oldest_first(self, db, seeded) -> None:
        """Insertion order within one transaction, which millisecond ties would hide.

        All three rows can share a ``created_at`` millisecond, so ordering by the
        timestamp alone is not enough - the repository breaks the tie by id, and
        this is the assertion that keeps that tiebreak in place.
        """
        from app.modules.order.repository import OrderRepository, OrderStatusLogRepository

        insert_order(db, seeded)
        order = OrderRepository(db).get_by_order_no("NV20260923000001", user_id=seeded["user_id"])
        assert order is not None
        logs = OrderStatusLogRepository(db)
        logs.append(order=order, to_status="PENDING_PAYMENT", operator_type="SYSTEM")
        logs.append(
            order=order,
            from_status="PENDING_PAYMENT",
            to_status="CANCELLED",
            operator_type="CUSTOMER",
            reason="user changed their mind",
        )
        logs.append(
            order=order,
            from_status="CANCELLED",
            to_status="CLOSED",
            operator_type="SYSTEM",
        )

        history = logs.list_for_order(int(order.id))
        assert [entry.to_status for entry in history] == [
            "PENDING_PAYMENT",
            "CANCELLED",
            "CLOSED",
        ]
        assert logs.count_for_order(int(order.id)) == 3


class TestOrderNumberPlaceholderScheme:
    """The insert-placeholder / flush / stamp scheme from design section 7.

    ``order_no`` embeds the auto-increment id (``NV<YYYYMMDD><id:06d>``), which
    cannot be known before the row is flushed - so the workflow writes a temporary
    unique value first and overwrites it. These tests pin the properties that
    scheme depends on, because they are easy to get wrong in a way that only shows
    up under concurrency.
    """

    def test_a_32_char_placeholder_fits_and_is_accepted(self, db, seeded) -> None:
        """``uuid4().hex`` is exactly 32 chars; the column is ``VARCHAR(32)``.

        There is no format CHECK on ``order_no`` (asserted below), so a placeholder
        is accepted. It sits exactly at the column limit rather than comfortably
        inside it, which is why the length is asserted rather than assumed - a
        longer placeholder would be rejected or truncated under strict mode, and
        that would surface later as a confusing duplicate-key error.
        """
        placeholder = "a" * 32
        assert len(placeholder) == 32
        assert insert_order(db, seeded, order_no=placeholder) > 0

    def test_no_format_constraint_exists_on_order_no(self, db) -> None:
        """No ``CHECK`` constrains the shape of ``order_no``.

        Worth testing explicitly: if one existed, the placeholder scheme would be
        rejected and the workflow would fail for a reason unrelated to the order.
        The only constraints on the identifier are ``NOT NULL`` and
        ``UNIQUE (merchant_id, order_no)``.
        """
        clauses = [
            row[0]
            for row in db.execute(
                text(
                    "SELECT cc.CHECK_CLAUSE FROM information_schema.TABLE_CONSTRAINTS tc "
                    "JOIN information_schema.CHECK_CONSTRAINTS cc "
                    "  ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA "
                    " AND cc.CONSTRAINT_NAME = tc.CONSTRAINT_NAME "
                    "WHERE tc.TABLE_SCHEMA = DATABASE() AND tc.CONSTRAINT_TYPE = 'CHECK' "
                    "  AND tc.TABLE_NAME = 'orders'"
                )
            ).all()
        ]
        assert not [clause for clause in clauses if "order_no" in clause]

    def test_stamping_over_the_placeholder_is_a_plain_update(self, db, seeded) -> None:
        """Overwriting the placeholder keeps the row valid and unique.

        This is the step the workflow performs after ``flush()``: the row is dirty
        but uncommitted, so nothing else can observe the placeholder, and the
        unique index is satisfied by the final value.

        Note that ``{id:06d}`` is a **minimum** width, not a maximum. The stamped
        value is 16 characters only while the id has at most 6 digits; at 7 digits
        it is 17, and still well inside ``VARCHAR(32)``. So the format degrades
        gracefully rather than truncating - but it is worth knowing, because a test
        that pinned ``len == 17`` against a *real* auto-increment id would fail
        purely because this test database has a lot of history in it.
        """
        order_id = insert_order(db, seeded, order_no="b" * 32)
        stamped = "NV20260923000042"
        # 2 (prefix) + 8 (YYYYMMDD) + 6 (padded id) = 16 characters.
        assert len(stamped) == 16
        db.execute(
            text("UPDATE orders SET order_no = :order_no WHERE id = :order_id"),
            {"order_no": stamped, "order_id": order_id},
        )
        persisted = db.execute(
            text("SELECT order_no FROM orders WHERE id = :order_id"),
            {"order_id": order_id},
        ).scalar_one()
        assert persisted == stamped

        # The real format must fit the column for any id this database can hand out.
        assert len(f"NV20260923{order_id:06d}") <= 32

    def test_placeholder_uniqueness_is_scoped_to_the_merchant(self, db) -> None:
        """The unique key is ``(merchant_id, order_no)``, not ``order_no`` alone.

        Recorded because it is why a temporary value is safe at all: two concurrent
        creates in *different* merchants may hold the same placeholder without
        colliding, while two in the same merchant cannot.
        """
        index_columns = db.execute(
            text(
                "SELECT GROUP_CONCAT(COLUMN_NAME ORDER BY SEQ_IN_INDEX) "
                "FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'orders' "
                "AND INDEX_NAME = 'uq_orders_merchant_order_no'"
            )
        ).scalar_one()
        assert index_columns == "merchant_id,order_no"


class TestOrderItemsRelationship:
    """``Order.items`` is ``selectin``, so reading it is not an N+1 surprise."""

    def test_items_are_loaded_with_the_order_not_lazily(self, db, seeded) -> None:
        """Accessing ``order.items`` after a repository read issues no extra query.

        ``lazy="selectin"`` loads the collection as part of the read - one bounded
        extra ``SELECT ... WHERE order_id IN (...)`` - as opposed to
        ``lazy="select"``, where a 20-row order list becomes 21 queries. Asserted by
        counting statements rather than by reading the model, because what matters
        is behaviour inside a workflow transaction.
        """
        from sqlalchemy import event

        from app.modules.order.repository import OrderRepository

        order_id = insert_order(db, seeded)
        insert_item(db, seeded, order_id)

        statements: list[str] = []
        engine = db.get_bind()

        def _listener(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _listener)
        try:
            order = OrderRepository(db).get_by_order_no(
                "NV20260923000001", user_id=seeded["user_id"]
            )
            assert order is not None
            before = len(statements)
            loaded = order.items
            after = len(statements)
        finally:
            event.remove(engine, "before_cursor_execute", _listener)

        assert len(loaded) == 1
        assert after == before, "reading order.items issued an extra query"

    def test_items_for_returns_the_snapshot_rows_only(self, db, seeded) -> None:
        """``items_for`` selects ``order_items`` and never joins the catalogue."""
        from app.modules.order.repository import OrderRepository

        order_id = insert_order(db, seeded)
        insert_item(db, seeded, order_id, product_name="Frozen Product Name", unit_price=299900)

        items = OrderRepository(db).items_for(order_id)
        assert len(items) == 1
        assert items[0].product_name == "Frozen Product Name"
        assert items[0].unit_price == 299900

    def test_add_status_log_persists_a_built_row(self, db, seeded) -> None:
        """``add_status_log`` accepts an already-constructed log row."""
        from app.modules.order.repository import OrderRepository

        insert_order(db, seeded)
        order = OrderRepository(db).get_by_order_no(
            "NV20260923000001", user_id=seeded["user_id"]
        )
        assert order is not None

        log = OrderStatusLog(
            order_id=order.id,
            order_no=order.order_no,
            from_status=None,
            to_status="PENDING_PAYMENT",
            operator_type="SYSTEM",
        )
        saved = OrderRepository(db).add_status_log(log)
        assert saved.id is not None


class TestOrderDeletionPolicy:
    """``session.delete(order)`` works; a raw ``DELETE FROM orders`` does not.

    That asymmetry is deliberate, and it is what the relationships' cascade
    settings have to deliver:

    * ``order_items.order_id`` and ``order_status_logs.order_id`` are
      ``ON DELETE RESTRICT``, because an order is a financial record - history must
      not vanish by accident, and a raw statement that would orphan it is refused;
    * ``Order.items`` / ``Order.status_logs`` carry ``cascade="all, delete-orphan"``
      and **no** ``passive_deletes``, so an explicit ORM delete removes the children
      first and then the parent.

    These tests exist because ``passive_deletes=True`` was the original setting and
    it does not work against ``RESTRICT``: it suppresses exactly the child deletes
    that the database is *not* going to perform on its own, so whether a delete
    succeeded depended on whether the collections happened to be loaded. In real
    use it raised ``IntegrityError 1451``. See the note on ``Order.items``.
    """

    def test_orm_delete_removes_children_before_the_parent(self, db, seeded) -> None:
        """Children are deleted explicitly, then the parent.

        The statement order is asserted rather than merely "it succeeded": a
        passing delete could also come from a database cascade, and there is no
        cascade rule here - so the order of statements is the actual evidence that
        the ORM did the work.
        """
        from sqlalchemy import event

        from app.modules.order.repository import OrderRepository

        order_id = insert_order(db, seeded)
        insert_item(db, seeded, order_id)
        insert_status_log(db, order_id)

        order = OrderRepository(db).get(order_id)
        assert order is not None
        assert len(order.items) == 1
        assert len(order.status_logs) == 1

        statements: list[str] = []
        engine = db.get_bind()

        def _listener(_conn, _cursor, statement, _params, _context, _many):
            statements.append(" ".join(statement.split()))

        event.listen(engine, "before_cursor_execute", _listener)
        try:
            db.delete(order)
            db.flush()
        finally:
            event.remove(engine, "before_cursor_execute", _listener)

        deletes = [s for s in statements if s.upper().startswith("DELETE")]
        assert len(deletes) == 3, deletes
        assert deletes[0].startswith("DELETE FROM order_items")
        assert deletes[1].startswith("DELETE FROM order_status_logs")
        assert deletes[2].startswith("DELETE FROM orders")

        assert db.get(Order, order_id) is None

    def test_orm_delete_works_when_children_were_never_loaded(self, db, seeded) -> None:
        """The regression case: an order whose collections are not in the session.

        This is the scenario that made ``passive_deletes=True`` fail. The children
        are written directly, so the identity map never holds them; the delete must
        still succeed. With ``passive_deletes=True`` this is where
        `IntegrityError 1451` was raised - on whichever FK the engine reached
        first.
        """
        order_id = insert_order(db, seeded)
        insert_item(db, seeded, order_id)
        insert_status_log(db, order_id)

        order = db.get(Order, order_id)
        assert order is not None
        # The selectin loader brings both collections in, so drop them again: the
        # fixture must be exactly "an order the session knows, without its children".
        order.__dict__.pop("items", None)
        order.__dict__.pop("status_logs", None)
        assert "items" not in order.__dict__
        assert "status_logs" not in order.__dict__

        db.delete(order)
        db.flush()

        assert db.get(Order, order_id) is None
        for table in ("order_items", "order_status_logs"):
            remaining = db.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE order_id = :order_id"),
                {"order_id": order_id},
            ).scalar_one()
            assert remaining == 0, f"{table} rows were orphaned"

    def test_raw_delete_is_refused_by_the_database(self, db, seeded) -> None:
        """Bypassing the ORM cannot delete an order - ``RESTRICT`` says no.

        The policy in one assertion: removing order history has to be a
        deliberate, ORM-mediated act rather than a stray statement.
        """
        order_id = insert_order(db, seeded)
        insert_item(db, seeded, order_id)

        message = _raises_rejection(
            db, "DELETE FROM orders WHERE id = :order_id", {"order_id": order_id}
        )
        assert f"[errno {MYSQL_FOREIGN_KEY_VIOLATION}]" in message, message
        assert "foreign key constraint fails" in message.lower()

        assert (
            db.execute(
                text("SELECT COUNT(*) FROM orders WHERE id = :order_id"),
                {"order_id": order_id},
            ).scalar_one()
            == 1
        )

    def test_raw_delete_of_a_childless_order_succeeds(self, db, seeded) -> None:
        """Negative control for the test above.

        Without this, the RESTRICT assertion would also pass if *every* raw delete
        failed for an unrelated reason - a mistyped table name, say. An order with
        no children has nothing to restrict it, so the database permits the delete.
        """
        order_id = insert_order(db, seeded)
        db.execute(text("DELETE FROM orders WHERE id = :order_id"), {"order_id": order_id})
        assert (
            db.execute(
                text("SELECT COUNT(*) FROM orders WHERE id = :order_id"),
                {"order_id": order_id},
            ).scalar_one()
            == 0
        )


class TestRepositoryConfirmsNoWriteFromReads:
    """The repository is data access only (spec section 18).

    A cheap structural guard: if a future edit adds a ``commit()`` to the
    repository, the transaction boundary stops belonging to the workflow, and
    section 49's "business rows and the outbox commit together" becomes
    impossible to guarantee.
    """

    def test_repository_module_does_not_commit(self) -> None:
        import inspect

        from app.modules.order import repository as module

        source = inspect.getsource(module)
        assert ".commit()" not in source, "the repository must not own the transaction boundary"

    def test_status_log_repository_has_no_mutating_operations(self) -> None:
        """Append-only: the audit trail's mutation methods do not exist."""
        from app.modules.order.repository import OrderStatusLogRepository

        for forbidden in ("update", "delete", "remove", "bulk_update", "purge"):
            assert not hasattr(OrderStatusLogRepository, forbidden), (
                f"OrderStatusLogRepository.{forbidden} exists - the log is append-only"
            )
