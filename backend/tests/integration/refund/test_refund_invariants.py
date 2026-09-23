"""FG-12 - the refund caps are enforced at the database boundary.

Spec section 112, `docs/architecture/ARCHITECTURE_INVARIANTS.md` INV-005
("*``refunded_amount <= paid_amount``*"), `PHASE5_DESIGN` sections 5.5 and 8,
REQ-AFS-002: *"Total refunded <= paid amount; item refund <= item payable amount"*.

## What this module is for, and why it is not a re-run of anything

The refund workflow's own tests assert that the amounts it writes are **correct**.
This module asserts the different, stronger thing the gate exists for: that the
database would **refuse** an incorrect one. Those are separate claims, and only the
second survives a bug in the code path that happened to run.

There are three row-level caps and the design puts a `CHECK` under each of them:

* `payments.refunded_amount <= paid_amount` -> `ck_payments_refund_cap`
* `orders.refunded_amount <= paid_amount` -> `ck_orders_refund_cap`
* `order_items.refunded_amount <= payable_amount` -> `ck_order_items_refund_cap`

These names are read from `information_schema`, never retyped, because the migration
prefixes constraint names through ``op.f()`` and a hand-typed name silently becomes a
constraint the ORM does not know about (q.v. `ck_orders_ck_orders_refund_cap` in the
migration's own notes).

## The three traps this file is built to survive

1. **A rejection for the wrong reason.** On a live table a violating write can be
   refused by a unique key, a foreign key or a neighbouring `CHECK`. MySQL reports a
   violated `CHECK` as **errno 3819 -> `OperationalError`**, while a unique violation is
   **1062 -> `IntegrityError`** - so `pytest.raises(IntegrityError)` does not catch a
   CHECK violation at all, and a test written that way passes while proving nothing.
   Observed while building this file: the first `order_items` probe was refused with
   `(1062, "Duplicate entry '12798-29386' for key 'order_items.uq_order_items_order_sku'")`,
   which is a correct refusal and a failed control. Every probe here therefore asserts
   the errno, the **constraint by name**, and `not isinstance(..., IntegrityError)`.

2. **The REPEATABLE READ / connection-reuse false green.** `HANDOFF.md` section 6. A
   probe that re-reads on the connection that performed the mutation can compare a
   snapshot against itself and pass regardless of what the database did. Every
   before/after comparison here reads through a **separate session and therefore a
   separate pooled connection**, after `rollback()` has returned the writer connection
   to the pool.

3. **A twin that proves nothing about the deployed schema.** Verifying the clause on a
   synthetic copy is worth doing (it isolates the clause from every other constraint)
   but it cannot show that the **live** table carries it. So the enforcement controls
   below run against the real tables, and the twin is used only for the meta-control at
   the end. Both claims are needed: the live tables for *enforcement*, twins for
   *clause isolation*.
   Two server behaviours make the twin harder to build than it looks, both measured on
   this MySQL 8.4.11 before the twin was written:

   * `CREATE TABLE ... LIKE` **does copy** `CHECK` constraints here (5 for `payments`,
     7 for `orders`, 6 for `order_items`). The widely-repeated claim that it does not
     is *not* true on this server, and a twin built on that assumption carries the very
     cap it is meant to be absent of - the control would then "prove" the cap fires when
     all it proved is that `LIKE` copied it. The twin below is therefore created with an
     **explicit minimal column list**, so there is nothing it could have inherited, and
     the test asserts the twin starts with zero constraints rather than assuming it.
   * `CHECK` constraint names are **unique per schema, not per table** - re-adding the
     live name to a twin fails with `(3822, "Duplicate check constraint name
     'ck_payments_refund_cap'.")`. The twin's constraint therefore gets its own name,
     which is the only reason the name below differs from the live one.

## The meta-control, and the concurrent-reader hazard it must not create

The design's section 8 negative controls are one per cap. A "meta-control" - drop the
cap, show the identical write now lands - is the strongest way to prove the rejection
was the cap. Two things bound how it may be built.

**It must not be run against a live table.** ``ALTER TABLE`` has no transaction semantics:
it performs an implicit commit, which closes the surrounding transaction and destroys its
savepoints, so a ``session.rollback()`` cannot undo it and the shared schema is left
damaged (observed while building this file: ``(1305, 'SAVEPOINT sa_savepoint_1 does not
exist')``, with a dropped cap on the live ``payments`` table that had to be restored from
``information_schema``).

**It must not create a table any other connection can see either.** An ordinary owned table
works for the test but is visible to everyone else, and ``alembic check`` compares the
models against the live schema - so while such a twin exists, ``alembic check`` reports
``('remove_table', Table('_fg12_alembic_probe', ...))`` and exits non-zero. Measured.
``alembic check`` is a gate several people run concurrently here, and a phantom table named
by someone else's running test is a false failure indistinguishable from a real one. This
file therefore uses a **TEMPORARY** table: session-scoped, invisible to
``information_schema`` and unreferenceable by any other connection
(``(1146, "Table 'nova._vis_probe' doesn't exist")``), and destroyed by the server when the
connection closes - so not even a hard crash can leave one behind. MySQL 8.4 does enforce a
``CHECK`` on a temporary table; that was verified before the construction was used.

## Quiescence, and why a reading has to say so

Every reading recorded from this file was taken on a **quiesced** database, with the pre-run
row counts captured and no other writer active. ``alembic check`` and the schema assertions
are both comparisons against shared state, so a concurrent writer can produce a false
failure that has nothing to do with the property under test. Where a capture names a
database state it also names the counts and the commit it was taken at, so the reading can
be reproduced rather than merely believed.


The design's section 8 negative controls are one per cap. A "meta-control" - drop the
cap, show the identical write now lands - is the strongest way to prove the rejection
was the cap. **It must not be run against a live table.** `ALTER TABLE` has no
transaction semantics: it performs an implicit commit, which closes the surrounding
transaction and destroys its savepoints, so a `session.rollback()` cannot undo it and
the shared schema is left damaged. This file does that control on a twin table it
creates and drops itself, which needs no transaction to be rolled back at all.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session

#: Re-exported rather than redefined: `commerce` owns the shared Phase 5 seed, and a
#: second definition of the engine would be a second definition of the shop (PHASE5
#: section 12). pytest resolves a fixture imported into this module normally.
from tests.integration.commerce.seed import engine

from app.shared.db.session import get_session_factory

__all__ = ["engine"]

pytestmark = [pytest.mark.integration]

#: MySQL's errno for a violated CHECK constraint.
ERRNO_CHECK_VIOLATION = 3819

#: MySQL's errno for a reference to a table the current session cannot see. Used to assert
#: that another connection cannot even reference the temporary twin.
ERRNO_NO_SUCH_TABLE = 1146

#: MySQL's errno for a deadlock. Retryable by definition, and expected when a test writes to
#: tables other writers are using concurrently.
ERRNO_DEADLOCK = 1213

#: The three row-level caps: (table, constraint name, cap column, probe column).
CAPS: tuple[tuple[str, str, str], ...] = (
    ("payments", "ck_payments_refund_cap", "paid_amount"),
    ("orders", "ck_orders_refund_cap", "paid_amount"),
    ("order_items", "ck_order_items_refund_cap", "payable_amount"),
)

#: Probe amounts: (legal value at the cap, violating value above it). `> 0` so the
#: separate non-negativity CHECKs cannot be the constraint that fires.
LEGAL_AT_CAP = 1000
OVER_THE_CAP = 2000

#: Every probe row is created and then removed inside a rolled-back transaction, so
#: these ids never reach the tables. High enough not to collide with fixture rows.
_PROBE_ID_FLOOR = 900_000


def _live_clause(engine: Engine, table: str, constraint: str) -> str | None:
    """The clause MySQL has stored for ``constraint``, or ``None`` if it is absent.

    Read from the server rather than retyped from the model. A probe that hardcodes the
    expression it expects to find can pass against a database whose stored clause is
    different in a way that matters.
    """
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT cc.CHECK_CLAUSE FROM information_schema.CHECK_CONSTRAINTS cc "
                "JOIN information_schema.TABLE_CONSTRAINTS tc "
                "  ON tc.CONSTRAINT_NAME = cc.CONSTRAINT_NAME "
                " AND tc.CONSTRAINT_SCHEMA = cc.CONSTRAINT_SCHEMA "
                "WHERE tc.TABLE_SCHEMA = DATABASE() "
                "  AND tc.TABLE_NAME = :table AND cc.CONSTRAINT_NAME = :constraint"
            ),
            {"table": table, "constraint": constraint},
        ).scalar()


def _read_cap_columns(session: Session, table: str, row_id: int, cap_col: str) -> tuple[int, int]:
    """``(cap_column, refunded_amount)`` for one row, as physical values."""
    row = session.execute(
        text(f"SELECT {cap_col}, refunded_amount FROM {table} WHERE id = :row_id"),
        {"row_id": row_id},
    ).one()
    return int(row[0]), int(row[1])


def _fresh_read(table: str, row_id: int, cap_col: str) -> tuple[int, int] | None:
    """Read through a **new session**, i.e. a different pooled connection.

    This is the whole defence against the self-comparison false green: a session that
    already performed the mutation may be serving its own snapshot.
    """
    session = get_session_factory()()
    try:
        row = session.execute(
            text(f"SELECT {cap_col}, refunded_amount FROM {table} WHERE id = :row_id"),
            {"row_id": row_id},
        ).one_or_none()
        return None if row is None else (int(row[0]), int(row[1]))
    finally:
        session.close()


#: Every probe row is created and then removed, keyed by these prefixes so cleanup can
#: find it even when a previous run died between the insert and the teardown.
PROBE_ORDER_PREFIX = "FG12-"
PROBE_PAYMENT_PREFIX = "FGTWELVE"


def _purge_residue(engine: Engine) -> None:
    """Remove any probe rows a previous interrupted run left behind.

    Called before the probes, because a leftover row is not merely untidy: a stale
    `order_items` probe keeps its ``(order_id, sku_id)`` pair occupied, and the next run's
    probe row is then refused by ``uq_order_items_order_sku`` *during setup*. That failure
    looks exactly like a cap defect while being an artefact of the last run - it was
    observed. Cleaning first makes the suite idempotent, which a test that runs against a
    shared development database has to be.
    """
    # Retried, because this runs against a database other people are writing to. A
    # `DELETE ... WHERE col LIKE ...` cannot use an index prefix and takes gap locks, so it
    # can deadlock (MySQL 1213) against a teammate's concurrent insert into the same table -
    # observed: `(1213, 'Deadlock found when trying to get lock; try restarting transaction')`
    # on the `payments` delete, which failed the cleanup and therefore the test. A deadlock is
    # a retryable condition by definition, and retrying is the honest fix; broadening the
    # transaction or ignoring the error would trade a flaky failure for silent residue.
    last: Exception | None = None
    for _attempt in range(3):
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "DELETE FROM order_items WHERE order_id IN "
                        "(SELECT id FROM orders WHERE order_no LIKE :prefix)"
                    ),
                    {"prefix": f"{PROBE_ORDER_PREFIX}%"},
                )
                connection.execute(
                    text("DELETE FROM orders WHERE order_no LIKE :prefix"),
                    {"prefix": f"{PROBE_ORDER_PREFIX}%"},
                )
                connection.execute(
                    text("DELETE FROM payments WHERE payment_no LIKE :prefix"),
                    {"prefix": f"{PROBE_PAYMENT_PREFIX}%"},
                )
            return
        except OperationalError as exc:
            if exc.orig is None or exc.orig.args[0] != ERRNO_DEADLOCK:
                raise
            last = exc
    raise AssertionError(f"probe residue could not be purged after 3 attempts: {last}")


def _insert_probe_row(session: Session, table: str, legal: int) -> tuple[int, str]:
    """Insert one throwaway row whose ``refunded_amount`` sits exactly at the cap.

    Returns ``(row_id, probe_marker)``. The marker is an ``order_no``/``payment_no`` built
    from the prefix above, so teardown can find the row by name rather than by an id that
    a failed insert never produced.

    A **new** row rather than an existing one: mutating a real row would make the probe
    depend on that row's other columns satisfying every other constraint.

    Three constructions were tried and two are recorded here because each looked correct:

    * the base values are taken from *other* tables, never from the table under probe. The
      first version copied from the same table (`INSERT ... SELECT ... FROM payments
      LIMIT 1`) and worked only while `payments` happened to be non-empty; it failed with
      `TypeError: int() argument ... not 'NoneType'` the moment the table was empty - a
      defect in the probe wearing the costume of a defect in the cap;
    * the `order_items` branch picks a ``(order_id, sku_id)`` pair that is **globally**
      absent (a cartesian product of the tables, filtered by ``NOT EXISTS``) while keeping
      both values foreign-key-valid. Offsetting ``sku_id`` by a constant is not a real SKU
      and is refused with `(1452, ... fk_order_items_sku_id_product_skus)`; picking a
      sibling SKU of the same product is refused with
      `(1062, ... uq_order_items_order_sku)` whenever that pair is already used. Both are
      correct refusals and both are failed controls, because a control must leave exactly
      one possible reason for the refusal - the constraint under test.
    """
    marker = f"{PROBE_ORDER_PREFIX}{legal}-{uuid.uuid4().hex[:8]}"
    if table == "payments":
        session.execute(
            text(
                "INSERT INTO payments (payment_no, order_id, order_no, user_id, channel, amount, "
                " status, idempotency_key, client_request_id, request_hash, paid_amount, "
                " refunded_amount, created_at, updated_at) "
                "SELECT :marker, o.id, o.order_no, o.user_id, 'MOCK', :legal, 'SUCCESS', "
                " :marker, :marker, 'probe', :legal, :legal, NOW(3), NOW(3) "
                "FROM orders o LIMIT 1"
            ),
            {"marker": f"{PROBE_PAYMENT_PREFIX}{legal}-{uuid.uuid4().hex[:8]}", "legal": legal},
        )
    elif table == "orders":
        session.execute(
            text(
                "INSERT INTO orders (user_id, order_no, client_request_id, request_hash, "
                " receiver_name, receiver_phone, address_snapshot, first_item_name, "
                " paid_amount, refunded_amount, payable_amount, original_amount, "
                " created_at, updated_at) "
                "SELECT user_id, :marker, :marker, 'probe', receiver_name, receiver_phone, "
                " address_snapshot, first_item_name, :legal, :legal, :legal, :legal, "
                " NOW(3), NOW(3) FROM orders LIMIT 1"
            ),
            {"marker": marker, "legal": legal},
        )
    else:
        # A brand-new order carrying the probe prefix, so `(order_id, sku_id)` is empty by
        # construction (a fresh order has no lines) and teardown can find the line through
        # its parent. Anchoring the line to an *existing* order instead was tried and left
        # residue: the row was invisible to the purge, so the next run tripped
        # `uq_order_items_order_sku` during setup. A probe whose cleanup depends on a parent
        # it does not own is not cleanable.
        session.execute(
            text(
                "INSERT INTO orders (user_id, order_no, client_request_id, request_hash, "
                " receiver_name, receiver_phone, address_snapshot, first_item_name, "
                " paid_amount, refunded_amount, payable_amount, original_amount, "
                " item_count, created_at, updated_at) "
                "SELECT user_id, :marker, :marker, 'probe', receiver_name, receiver_phone, "
                " address_snapshot, first_item_name, :legal, :legal, :legal, :legal, 1, "
                " NOW(3), NOW(3) FROM orders LIMIT 1"
            ),
            {"marker": marker, "legal": legal},
        )
        order_id = int(
            session.execute(
                text("SELECT id FROM orders WHERE order_no = :marker"), {"marker": marker}
            ).scalar_one()
        )
        # Any real SKU and any real warehouse: with a fresh order there is no pair to
        # collide with, so both foreign keys are satisfied and the unique key cannot fire.
        sku_id = int(
            session.execute(text("SELECT id FROM product_skus ORDER BY id LIMIT 1")).scalar_one()
        )
        session.execute(
            text(
                "INSERT INTO order_items (order_id, warehouse_id, product_id, sku_id, quantity, "
                " product_name, sku_name, unit_price, original_amount, payable_amount, "
                " refunded_amount, after_sale_status, created_at, updated_at) "
                "SELECT :order_id, w.id, ps.product_id, ps.id, 1, 'FG-12 probe', 'FG-12 probe', "
                " :legal, :legal, :legal, :legal, 'NONE', NOW(3), NOW(3) "
                "FROM product_skus ps CROSS JOIN warehouses w "
                "WHERE ps.id = :sku_id LIMIT 1"
            ),
            {"order_id": order_id, "sku_id": sku_id, "legal": legal},
        )
    return int(session.execute(text(f"SELECT MAX(id) FROM {table}")).scalar_one()), marker


@pytest.mark.parametrize(("table", "constraint", "cap_col"), CAPS, ids=[cap[1] for cap in CAPS])
def test_the_live_table_carries_the_named_cap(
    engine: Engine, table: str, constraint: str, cap_col: str
) -> None:
    """The deployed schema really has the constraint, under the name the ORM expects.

    Separate from the enforcement test below on purpose: this one fails for a *schema*
    reason (the cap was never created, was dropped, or landed under a double-prefixed
    name) and the other fails for a *behaviour* reason. Collapsing them loses the
    diagnosis.
    """
    clause = _live_clause(engine, table, constraint)
    assert clause is not None, (
        f"{table}.{constraint} is absent from the deployed schema - the cap is not enforced "
        "at the database boundary at all, whatever the migration file says"
    )
    assert "refunded_amount" in clause, clause


@pytest.mark.parametrize(("table", "constraint", "cap_col"), CAPS, ids=[cap[1] for cap in CAPS])
def test_a_write_over_the_cap_is_refused_by_that_named_check(
    engine: Engine, table: str, constraint: str, cap_col: str
) -> None:
    """The live table refuses a violating write, names the constraint, and changes nothing.

    Four claims, all needed:

    1. the probing write is **refused**;
    2. it is refused as a **CHECK** violation (errno 3819), by the **named** constraint -
       not as an `IntegrityError` from a key, and not by a neighbouring constraint;
    3. the stored row is **byte-identical** afterwards, read on a **different connection**;
    4. the row is then **removed completely**, so the test leaves the shared database as
       it found it.

    ## The transaction shape, which is forced rather than chosen

    The probe row is inserted and **committed** *outside* any savepoint, and the failing
    write is issued inside `begin_nested()`. The first version of this test did the
    opposite - it created the row inside a nested block that was then released - and the
    cross-connection read returned `None`, because in MySQL a savepoint's writes become
    visible to *other connections* only when the **outer** transaction commits. A probe
    that keeps its row inside an uncommitted transaction therefore cannot be checked
    from a second connection at all, and the check silently degrades into "the row is
    invisible", which is exactly the false green this file exists to avoid. Committing
    the row up front is what makes claim 3 a real observation; the `finally` block makes
    it safe.

    Only the violating write is savepoint-scoped, because only *it* needs to be
    discarded - and `rollback()` on a statement refused by a `CHECK` would otherwise have
    been unnecessary, since MySQL never applied it. The savepoint is kept for the same
    reason the committed Phase 4 constraint test keeps it: the failure must be confined,
    and the session must still be usable to clean up.
    """
    assert _live_clause(engine, table, constraint) is not None, (
        f"{table}.{constraint} is missing, so this control cannot mean anything"
    )
    _purge_residue(engine)

    session = get_session_factory()()
    row_id: int | None = None
    try:
        # Committed deliberately: a second connection can only observe a committed row.
        row_id, _marker = _insert_probe_row(session, table, LEGAL_AT_CAP)
        session.commit()

        before = _fresh_read(table, row_id, cap_col)
        assert before == (LEGAL_AT_CAP, LEGAL_AT_CAP), (
            f"{table}: the boundary row did not land as expected (got {before}); a probe row "
            "that is itself inadmissible would make the refusal below meaningless"
        )

        # Claims 1 + 2: the violating write is refused, by this constraint, as a CHECK.
        with pytest.raises(DBAPIError) as caught, session.begin_nested():
            session.execute(
                text(f"UPDATE {table} SET refunded_amount = :over WHERE id = :row_id"),
                {"over": OVER_THE_CAP, "row_id": row_id},
            )

        error = caught.value
        assert not isinstance(error, IntegrityError), (
            f"{table}: refused by a key (1062/1452), not by the cap - the probe is built so no "
            f"unique or foreign key can be what fires: {error.orig.args}"
        )
        assert isinstance(error, OperationalError), (
            f"{table}: expected OperationalError (CHECK/3819), got {type(error).__name__}"
        )
        assert error.orig.args[0] == ERRNO_CHECK_VIOLATION, error.orig.args
        assert constraint in str(error.orig.args[1]), (
            f"{table}: expected {constraint} to fire, server said: {error.orig.args[1]}"
        )

        # Claim 3: the refused write left nothing behind. A constraint that merely raised
        # after applying the row would show OVER_THE_CAP here.
        assert _fresh_read(table, row_id, cap_col) == before, (
            f"{table}: the refused write still landed on a fresh connection"
        )
    finally:
        session.close()
        # Claim 4: remove the probe row by its marker, through a fresh session, so teardown
        # does not depend on this session's state after a refusal - and so a run that died
        # mid-test is still cleaned up by the next one.
        _purge_residue(engine)

    # Verified on yet another connection: the shared database is exactly as it was.
    assert _fresh_read(table, row_id, cap_col) is None, (
        f"{table}: the probe row survived the test - this file must leave no residue"
    )


def test_dropping_the_cap_lets_the_identical_write_land(engine: Engine) -> None:
    """Meta-control on an owned TEMPORARY twin: prove the rejection was the cap.

    Without this step a "refused" result is consistent with the probe row being
    inadmissible for some unrelated reason. With it, the identical INSERT is shown to be
    acceptable, and the only thing that changed between the two attempts is the presence
    of the constraint.

    ## Why a TEMPORARY table, and not just an owned table

    An owned ordinary table works, but it has two measured costs this construction avoids
    entirely:

    * **`alembic check` fails while it exists.** `alembic check` compares the models against
      the live schema, so any extra table is reported as `remove_table` and the check exits
      non-zero. Measured with an owned twin standing:
      `FAILED: New upgrade operations detected: [('remove_table', Table('_fg12_alembic_probe' ...))]`.
      `alembic check` is a gate, several people run it, and a phantom table named by a
      concurrent test is a false failure indistinguishable from a real one. A TEMPORARY
      table is session-scoped and **invisible** to other connections and to
      `information_schema` (verified: `visible in information_schema.CHECK_CONSTRAINTS: 0`),
      so it cannot appear in anyone else's diff.
    * **a leftover twin would poison a later check.** A twin dropped only on the success path
      outlives a failing test. A TEMPORARY table is destroyed by the server when its
      connection closes, so even a hard crash of the test cannot leave one behind; the
      explicit `DROP TEMPORARY TABLE` in `finally` is belt-and-braces, not the mechanism.

    MySQL 8.4 does enforce a `CHECK` on a TEMPORARY table - verified before this test was
    written, since the whole construction rests on it:
    `INSERT (1000, 2000) -> (3819, "Check constraint 'ck_tmp_probe_cap' is violated.")`.

    ## The other measured constraints on this construction

    * the clause is attached **before** the violating row is inserted. MySQL validates a new
      `CHECK` against rows already present, so attaching it afterwards fails on the `ALTER`
      itself with `(3819, ...)`. (On a table that has legitimately drifted, that ordering
      means the cap cannot be added at all.)
    * the twin is built from an **explicit column list**, not `CREATE TABLE ... LIKE`. On
      this server `LIKE` **does** copy `CHECK` constraints (renamed to `<table>_chk_N`) and
      copies keys too, so a `LIKE` twin is neither constraint-free nor key-free and the
      control would silently measure the wrong thing.
    * it must never be run against a **live** table. `ALTER TABLE` performs an implicit commit
      on MySQL, so a `DROP CHECK` there is not undone by `session.rollback()` - it destroys
      the surrounding savepoints and leaves the shared schema damaged (observed:
      `(1305, 'SAVEPOINT sa_savepoint_1 does not exist')`).

    The clause itself is the live table's own, read out of `information_schema`, so this
    control exercises the expression the deployed schema actually stores rather than a string
    retyped from the design document.
    """
    live_table = "payments"
    live_constraint = "ck_payments_refund_cap"
    twin_constraint = "ck_fg12_twin_refund_cap"

    clause = _live_clause(engine, live_table, live_constraint)
    assert clause is not None, f"{live_table}.{live_constraint} is absent; no basis for this control"

    with engine.connect() as connection:
        # One connection holds the temporary table for its whole life, which is also why
        # every statement below goes through it rather than through `engine.begin()`.
        connection.execute(text("DROP TEMPORARY TABLE IF EXISTS _fg12_twin_refund_cap"))
        try:
            connection.execute(
                text(
                    "CREATE TEMPORARY TABLE _fg12_twin_refund_cap ("
                    "  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,"
                    "  paid_amount BIGINT NOT NULL,"
                    "  refunded_amount BIGINT NOT NULL"
                    ")"
                )
            )
            connection.execute(
                text(
                    f"ALTER TABLE _fg12_twin_refund_cap "
                    f"ADD CONSTRAINT {twin_constraint} CHECK {clause}"
                )
            )
            connection.commit()

            def _insert_here(paid: int, refunded: int) -> None:
                connection.execute(
                    text(
                        "INSERT INTO _fg12_twin_refund_cap (paid_amount, refunded_amount) "
                        "VALUES (:paid, :refunded)"
                    ),
                    {"paid": paid, "refunded": refunded},
                )
                connection.commit()

            # Premise: nothing else in this table's shape refuses the row. Sanity-checked by
            # a legal insert first, so a refusal below cannot be a NOT NULL or type mismatch.
            _insert_here(LEGAL_AT_CAP, LEGAL_AT_CAP)

            # The isolation this construction depends on, asserted rather than assumed. A
            # second connection must not merely fail to *see* the twin - it must be unable to
            # reference it at all, because that is what keeps it out of a concurrent
            # `alembic check`'s schema diff. MySQL raises 1146 for a reference to another
            # session's temporary table: observed
            # `ProgrammingError (1146, "Table 'nova._vis_probe' doesn't exist")`.
            with engine.connect() as observer:
                with pytest.raises(ProgrammingError) as blind:
                    observer.execute(
                        text(
                            "INSERT INTO _fg12_twin_refund_cap (paid_amount, refunded_amount) "
                            "VALUES (1, 1)"
                        )
                    )
                assert blind.value.orig.args[0] == ERRNO_NO_SUCH_TABLE, blind.value.orig.args
                assert (
                    observer.execute(
                        text(
                            "SELECT COUNT(*) FROM information_schema.TABLES "
                            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '_fg12_twin_refund_cap'"
                        )
                    ).scalar_one()
                    == 0
                ), "the temporary twin is visible in information_schema, so it could appear in a DDL diff"

            # The violating INSERT is refused - by this constraint, as a CHECK.
            with pytest.raises(OperationalError) as caught:
                _insert_here(LEGAL_AT_CAP, OVER_THE_CAP)
            assert caught.value.orig.args[0] == ERRNO_CHECK_VIOLATION, caught.value.orig.args
            assert twin_constraint in str(caught.value.orig.args[1]), caught.value.orig.args

            # The control's premise: with the cap gone, the identical INSERT lands. Without
            # this step "refused" would be consistent with the row being inadmissible for
            # some unrelated reason.
            connection.execute(text(f"ALTER TABLE _fg12_twin_refund_cap DROP CHECK {twin_constraint}"))
            connection.commit()
            _insert_here(LEGAL_AT_CAP, OVER_THE_CAP)
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM _fg12_twin_refund_cap")
                ).scalar_one()
                == 2
            ), "the identical INSERT did not land with the cap removed, so the cap is not what refused it"
        finally:
            connection.execute(text("DROP TEMPORARY TABLE IF EXISTS _fg12_twin_refund_cap"))
            connection.commit()

    # And nothing outlives the connection, on any path including a crash.
    with engine.connect() as after:
        assert (
            after.execute(
                text(
                    "SELECT COUNT(*) FROM information_schema.TABLES "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '_fg12_twin_refund_cap'"
                )
            ).scalar_one()
            == 0
        ), "the twin outlived the test"
