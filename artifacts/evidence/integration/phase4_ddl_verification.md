# Phase 4 DDL verification record (task D1 / t2)

Owner: `order-data`. Revision under test: `a7c4e91b2d63`
(`backend/migrations/versions/20260923_1340_a7c4e91b2d63_phase4_orders_and_idempotency.py`).

This file exists because the design (`docs/architecture/PHASE4_DESIGN.md` section 1)
requires the migration to be **hand-verified in `information_schema`**, and because
`HANDOFF.md` section 6 records that "the migration ran with exit 0" is not evidence
of anything on MySQL. The queries below were run against the live database and the
output is pasted verbatim, not summarised.

Environment: MySQL 8.4.11, `utf8mb4_0900_ai_ci`, `READ-COMMITTED`, database `nova`.

---

## 1. The four tables exist

```sql
SELECT TABLE_NAME FROM information_schema.TABLES
 WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_NAME IN ('orders','order_items','order_status_logs','idempotency_records')
 ORDER BY TABLE_NAME;
```

```
idempotency_records
order_items
order_status_logs
orders
```

## 2. All 15 CHECK constraints landed, with the frozen names

```sql
SELECT tc.TABLE_NAME, tc.CONSTRAINT_NAME, cc.CHECK_CLAUSE
  FROM information_schema.TABLE_CONSTRAINTS tc
  JOIN information_schema.CHECK_CONSTRAINTS cc
    ON cc.CONSTRAINT_SCHEMA = tc.CONSTRAINT_SCHEMA
   AND cc.CONSTRAINT_NAME   = tc.CONSTRAINT_NAME
 WHERE tc.TABLE_SCHEMA = DATABASE()
   AND tc.CONSTRAINT_TYPE = 'CHECK'
   AND tc.TABLE_NAME IN ('orders','order_items','order_status_logs','idempotency_records')
 ORDER BY tc.TABLE_NAME, tc.CONSTRAINT_NAME;
```

```
idempotency_records.ck_idempotency_records_status_valid
order_items.ck_order_items_after_sale_status_valid
order_items.ck_order_items_allocated_consistent
order_items.ck_order_items_amounts_non_negative
order_items.ck_order_items_payable_consistent
order_items.ck_order_items_quantity_positive
order_status_logs.ck_order_status_logs_from_status_valid
order_status_logs.ck_order_status_logs_operator_valid
order_status_logs.ck_order_status_logs_to_status_valid
orders.ck_orders_after_sale_status_valid
orders.ck_orders_amounts_non_negative
orders.ck_orders_fulfillment_status_valid
orders.ck_orders_payable_consistent
orders.ck_orders_payment_status_valid
orders.ck_orders_status_valid
```

15 rows, matching `docs/architecture/PHASE4_DESIGN.md` section 4 exactly - 6 on
`orders`, 5 on `order_items`, 3 on `order_status_logs`, 1 on `idempotency_records`.
This matters because **Alembic does not autogenerate CHECK-constraint changes on
MySQL**: had one been dropped or misnamed, nothing else in the toolchain would
have noticed.

## 3. Money is signed BIGINT, ids are unsigned

```sql
SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
  FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_NAME IN ('orders','order_items','order_status_logs','idempotency_records')
   AND (COLUMN_NAME LIKE '%amount%' OR COLUMN_NAME IN ('unit_price','id'))
 ORDER BY TABLE_NAME, COLUMN_NAME;
```

```
idempotency_records.id                    bigint unsigned
order_items.allocated_discount_amount     bigint
order_items.coupon_discount_amount        bigint
order_items.id                            bigint unsigned
order_items.original_amount               bigint
order_items.payable_amount                bigint
order_items.promotion_discount_amount     bigint
order_items.refunded_amount               bigint
order_items.unit_price                    bigint
order_status_logs.id                      bigint unsigned
orders.coupon_discount_amount             bigint
orders.id                                 bigint unsigned
orders.original_amount                    bigint
orders.paid_amount                        bigint
orders.payable_amount                     bigint
orders.promotion_discount_amount          bigint
orders.refunded_amount                    bigint
orders.shipping_amount                    bigint
```

Every money column is `bigint` - **signed**, not `bigint unsigned`. This is the
decision that makes `ck_orders_payable_consistent` correct: the constraint performs
subtraction (`payable = original - promotion - coupon + shipping`), and MySQL
promotes a mixed signed/unsigned comparison to UNSIGNED, so one unsigned operand
would make `0 - 1` overflow and reject an arithmetically correct row. That is the
exact trap FG-09 lost an hour to on the stock ledger
(`20260923_1235_5cbebbc7b364_ledger_quantities_signed_bigint.py`).

Timestamps are `datetime(3)` and status columns are `varchar` (not ENUM) throughout;
all 8 foreign keys are `ON DELETE RESTRICT`.

---

# MySQL traps encountered

Three real traps surfaced while doing this work. None of them were worked around by
quietly changing a model: each is recorded here, fixed at the right layer, and in two
cases pinned by a regression test.

## Trap A - a violated CHECK is `OperationalError`, not `IntegrityError`

Six of the constraint tests initially failed **even though the constraint fired
correctly**: the assertions were right, the exception class was not. MySQL 8.4 reports
a CHECK violation as errno **3819**, which SQLAlchemy maps to `OperationalError`;
a unique-index violation is errno **1062**, which maps to `IntegrityError`. So
"catch `IntegrityError` to detect a constraint violation" is wrong for half the
constraints in this phase.

Captured directly, on the same connection, in one transaction:

```
(a) CHECK violation -> OperationalError | (pymysql.err.OperationalError) (3819, "Check constraint 'ck_orders_status_valid' is violated.")
(b) UNIQUE violation -> IntegrityError | (pymysql.err.IntegrityError) (1062, "Duplicate entry '620-cr-trap-2' for key 'orders.uq_orders_user_client_request'")
```

Handling: `tests/integration/order/test_data_layer_schema.py::_raises_rejection`
catches `(IntegrityError, OperationalError)` and each test still asserts the
*specific constraint name* in the message, so the rule being verified is unchanged.
The `IdempotencyRepository.insert_in_progress` path is unaffected - it only ever
encounters the 1062 `IntegrityError` of the unique index.

## Trap B - `DROP INDEX` fails on an index that backs a foreign key (errno 1553)

`downgrade()` originally dropped each index before dropping its table, which is the
shape Alembic autogenerates. It failed on the first run:

```
pymysql.err.OperationalError: (1553, "Cannot drop index 'ix_order_status_logs_order_created': needed in a foreign key constraint")
sqlalchemy.exc.OperationalError: (pymysql.err.OperationalError) (1553, "Cannot drop index 'ix_order_status_logs_order_created': needed in a foreign key constraint")
```

`ix_order_status_logs_order_created` on `(order_id, created_at)` is serving
`fk_order_status_logs_order_id_orders` (InnoDB chose the composite index over the
single-column one, which had already been dropped by the time the error was raised).
`DROP TABLE` removes a table's indexes and constraints with the table, so the explicit
drops were the bug rather than a safety measure.

**Why this one is worth recording rather than just fixing:** MySQL DDL is
non-transactional, so the failure left the database *half-downgraded* -
`alembic_version` still named `a7c4e91b2d63` while two indexes were already gone.
The migration reported an error, but the schema was in neither state. Guarded now by
`test_migration_downgrade_does_not_predrop_indexes`, and the round trip
(upgrade → downgrade → upgrade) was executed clean.

## Trap C - `information_schema.CHECK_CONSTRAINTS` has no `TABLE_NAME` (MySQL 8.4)

```sql
SELECT COLUMN_NAME FROM information_schema.COLUMNS
 WHERE TABLE_SCHEMA='information_schema' AND TABLE_NAME='CHECK_CONSTRAINTS';
```

```
CONSTRAINT_CATALOG, CONSTRAINT_SCHEMA, CONSTRAINT_NAME, CHECK_CLAUSE
```

There is no `TABLE_NAME`, so the direct query fails:

```
pymysql.err.OperationalError: (1054, "Unknown column 'cc.TABLE_NAME' in 'field list'")
```

The join through `TABLE_CONSTRAINTS` (query 2 above) is mandatory. Already listed in
`HANDOFF.md` section 6; reproduced here because the Phase 4 verification depends on
it and a future reader should not have to rediscover it from an error message.

---

# Reproducing this record

```powershell
cd backend
..\.venv\Scripts\python.exe -m alembic current          # expect a7c4e91b2d63 (head)
..\.venv\Scripts\python.exe -m alembic check            # expect "No new upgrade operations detected"
..\.venv\Scripts\python.exe -m pytest tests/integration/order/test_data_layer_schema.py -q
```

`alembic check` reporting no operations is the third independent confirmation that
the models and the database agree - alongside the `information_schema` reads above
and the 63-test suite that exercises both.
