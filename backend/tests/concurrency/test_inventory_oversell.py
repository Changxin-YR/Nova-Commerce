"""FG-09 - inventory concurrency: never oversell.

Spec §113 states the gate exactly:

    真实 MySQL。库存：1。20 个并发 CreateOrder。必须：成功 1、失败 19、
    最终 available=0、从不负库存。禁止 Mock DB 后宣称通过。

So this test runs against **real MySQL**. That is not ceremony. The property
under test is a database behaviour - ``SELECT ... FOR UPDATE`` taking an
exclusive row lock, and ``CHECK (available_qty >= 0)`` refusing a bad write.
Neither exists in a mock, so a passing mocked run would say nothing at all about
whether the shop can oversell.

The test also carries a **negative control**. A concurrency test can pass for the
wrong reason: if the threads happen to serialise, a naive implementation looks
correct. :meth:`test_naive_read_then_write_DOES_oversell` forces the interleaving
with a barrier and demonstrates that the unlocked path really does oversell. That
is what makes the positive test meaningful - it proves the lock is doing work,
rather than that the load was too gentle to expose a bug.
"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select, text

from app.core.errors import InsufficientStockError
from app.modules.catalog.models import Product, ProductSku
from app.modules.identity.models import Merchant
from app.modules.inventory.enums import MovementType, OperatorType, ReferenceType
from app.modules.inventory.models import Inventory, InventoryMovement, Warehouse
from app.modules.inventory.service import InventoryService
from app.shared.db.session import configure_database, get_session_factory

pytestmark = [pytest.mark.concurrency, pytest.mark.integration]

#: The exact number from §113.
CONCURRENT_CALLERS = 20
INITIAL_STOCK = 1


# ---------------------------------------------------------------------------
# Seeding (committed, because concurrent sessions must see it)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def engine():
    return configure_database()


@pytest.fixture
def seeded(engine):
    """Create one merchant / product / SKU / warehouse / inventory row and commit.

    Committed rather than rolled back on purpose: 20 worker threads open their own
    sessions, and a transaction-local fixture would be invisible to them.
    """
    factory = get_session_factory()
    marker = uuid.uuid4().hex[:10]

    with factory() as session:
        merchant = Merchant(code=f"CC{marker}"[:24], name="Concurrency Test Merchant")
        session.add(merchant)
        session.flush()

        product = Product(
            merchant_id=merchant.id,
            product_no=f"P-{marker}",
            slug=f"p-{marker}",
            name="Concurrency Test Product",
            status="PUBLISHED",
        )
        session.add(product)
        session.flush()

        sku = ProductSku(
            merchant_id=merchant.id,
            product_id=product.id,
            sku_no=f"S-{marker}",
            sku_code=f"sku-{marker}",
            name="Concurrency Test SKU",
            price_amount=1000,
            cost_amount=500,
            status="ACTIVE",
        )
        session.add(sku)
        session.flush()

        warehouse = Warehouse(
            merchant_id=merchant.id,
            code=f"WH{marker}"[:24],
            name="Concurrency Test Warehouse",
            is_default=False,
        )
        session.add(warehouse)
        session.flush()

        inventory = Inventory(
            merchant_id=merchant.id,
            warehouse_id=warehouse.id,
            sku_id=sku.id,
            # The row holds the stock AND a matching PURCHASE_IN movement records
            # where it came from. Both halves are required: seeding the balance
            # alone makes the row unexplainable by its own ledger (which
            # verify_ledger() correctly flagged), while recording the movement
            # alone leaves the row at zero and nothing to reserve.
            available_qty=INITIAL_STOCK,
            locked_qty=0,
        )
        session.add(inventory)
        session.flush()
        session.add(
            InventoryMovement.build(
                warehouse_id=warehouse.id,
                sku_id=sku.id,
                movement_type=MovementType.PURCHASE_IN,
                before_available=0,
                after_available=INITIAL_STOCK,
                before_locked=0,
                after_locked=0,
                idempotency_key=f"seed-{marker}",
                reference_type=ReferenceType.PURCHASE_ORDER,
                operator_type=OperatorType.SYSTEM,
                reason="FG-09 fixture: opening stock",
            )
        )
        session.commit()

        ids = {
            "merchant": merchant.id,
            "product": product.id,
            "sku": sku.id,
            "warehouse": warehouse.id,
            "inventory": inventory.id,
        }

    try:
        yield ids
    finally:
        # Movements and inventories reference the SKU with RESTRICT, so the
        # teardown has to unwind in dependency order.
        with factory() as session:
            session.execute(
                delete(InventoryMovement).where(InventoryMovement.sku_id == ids["sku"])
            )
            session.execute(delete(Inventory).where(Inventory.sku_id == ids["sku"]))
            session.execute(delete(ProductSku).where(ProductSku.id == ids["sku"]))
            session.execute(delete(Product).where(Product.id == ids["product"]))
            session.execute(delete(Warehouse).where(Warehouse.id == ids["warehouse"]))
            session.execute(delete(Merchant).where(Merchant.id == ids["merchant"]))
            session.commit()


def _read_position(engine, sku_id: int, warehouse_id: int) -> tuple[int, int, int]:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT available_qty, locked_qty, version FROM inventories "
                "WHERE sku_id = :sku AND warehouse_id = :wh"
            ),
            {"sku": sku_id, "wh": warehouse_id},
        ).one()
        return int(row[0]), int(row[1]), int(row[2])


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def test_20_concurrent_reservations_against_1_unit(seeded, engine) -> None:
    """§113, verbatim: 1 unit, 20 concurrent callers, exactly 1 winner."""
    sku_id = seeded["sku"]
    warehouse_id = seeded["warehouse"]
    factory = get_session_factory()

    # A barrier maximises genuine contention: every worker waits until all 20 are
    # ready, then they all enter the lock at once. Without it, thread startup
    # jitter would serialise them and the test would be far too gentle.
    barrier = threading.Barrier(CONCURRENT_CALLERS)
    successes: list[int] = []
    insufficient: list[int] = []
    unexpected: list[BaseException] = []
    lock = threading.Lock()

    def attempt(index: int) -> None:
        session = factory()
        try:
            barrier.wait(timeout=30)
            service = InventoryService(session)
            result = service.reserve(
                sku_id=sku_id,
                quantity=1,
                warehouse_id=warehouse_id,
                idempotency_key=f"fg09-{uuid.uuid4().hex}",
            )
            session.commit()
            with lock:
                successes.append(index)
            assert result.replayed is False
        except InsufficientStockError:
            session.rollback()
            with lock:
                insufficient.append(index)
        except BaseException as exc:
            session.rollback()
            with lock:
                unexpected.append(exc)
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=CONCURRENT_CALLERS) as pool:
        list(pool.map(attempt, range(CONCURRENT_CALLERS)))

    # ---- the four assertions of §113 -------------------------------------
    assert not unexpected, f"unexpected failures: {[repr(e) for e in unexpected]}"
    assert len(successes) == 1, (
        f"expected exactly 1 success, got {len(successes)} - the shop oversold"
    )
    assert len(insufficient) == CONCURRENT_CALLERS - 1, (
        f"expected {CONCURRENT_CALLERS - 1} rejections, got {len(insufficient)}"
    )

    available, locked, version = _read_position(engine, sku_id, warehouse_id)
    assert available == 0, f"final available_qty should be 0, got {available}"
    assert locked == 1, f"the one reserved unit should be locked, got {locked}"
    # 20 attempts, only one of which mutated the row.
    assert version == 2, f"expected version 2 (1 initial + 1 mutation), got {version}"


def test_stock_never_goes_negative_at_any_point(seeded, engine) -> None:
    """The DB constraint INV-001/INV-002 holds even under contention.

    Checked after the storm rather than per-instant: MySQL enforces the CHECK on
    every write, so a negative value is not merely unlikely, it is impossible to
    persist. This asserts that no compensating write slipped through afterwards.
    """
    sku_id = seeded["sku"]
    warehouse_id = seeded["warehouse"]
    factory = get_session_factory()
    barrier = threading.Barrier(CONCURRENT_CALLERS)

    def attempt(_index: int) -> None:
        session = factory()
        try:
            barrier.wait(timeout=30)
            InventoryService(session).reserve(
                sku_id=sku_id,
                quantity=1,
                warehouse_id=warehouse_id,
                idempotency_key=f"fg09b-{uuid.uuid4().hex}",
            )
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=CONCURRENT_CALLERS) as pool:
        list(pool.map(attempt, range(CONCURRENT_CALLERS)))

    available, locked, _ = _read_position(engine, sku_id, warehouse_id)
    assert available >= 0, f"available_qty went negative: {available}"
    assert locked >= 0, f"locked_qty went negative: {locked}"


def test_the_database_itself_refuses_a_negative_balance(seeded, engine) -> None:
    """INV-001 as a database guarantee, independent of application code.

    This is the assertion that makes "the CHECK constraint is the last line of
    defence" a fact rather than a claim. Application logic can have a bug; a
    constraint cannot be raced past.
    """
    from sqlalchemy.exc import DataError, IntegrityError, OperationalError

    # Two different mechanisms can reject this, and both are correct:
    #   * an out-of-range error (1264) because the column is BIGINT UNSIGNED and
    #     -1 cannot be represented at all;
    #   * a CHECK violation (3819) if the value were representable but negative.
    # Accepting either keeps the test about the guarantee ("a negative balance
    # cannot be persisted") rather than about which layer happens to fire first.
    with engine.connect() as connection, pytest.raises((IntegrityError, DataError, OperationalError)):
        connection.execute(
            text(
                "UPDATE inventories SET available_qty = -1 "
                "WHERE sku_id = :sku AND warehouse_id = :wh"
            ),
            {"sku": seeded["sku"], "wh": seeded["warehouse"]},
        )
        connection.commit()


def test_ledger_explains_the_balance(seeded, engine) -> None:
    """INV-007: after the storm, the movements must account for the row."""
    sku_id = seeded["sku"]
    warehouse_id = seeded["warehouse"]
    factory = get_session_factory()

    with factory() as session:
        # One reservation has already happened if the earlier test ran; make one
        # more so there is a movement to reconcile.
        service = InventoryService(session)
        try:
            service.reserve(
                sku_id=sku_id,
                quantity=1,
                warehouse_id=warehouse_id,
                idempotency_key=f"fg09-ledger-{uuid.uuid4().hex}",
            )
            session.commit()
        except InsufficientStockError:
            session.rollback()

    with factory() as session:
        ok, detail = InventoryService(session).verify_ledger(
            warehouse_id=warehouse_id, sku_id=sku_id
        )
        assert ok, f"the movement ledger does not explain the balance: {detail}"

        movements = session.execute(
            select(InventoryMovement).where(InventoryMovement.sku_id == sku_id)
        ).scalars().all()
        assert movements, "a reservation happened but no movement was recorded"
        # The fixture's opening PURCHASE_IN is expected here; asserting on the
        # *arithmetic* rather than the type is the point, because INV-007 is about
        # the ledger explaining the balance, whatever the movements happen to be.
        kinds = {movement.movement_type for movement in movements}
        assert MovementType.PURCHASE_IN.value in kinds, "the opening stock is missing from the ledger"
        assert MovementType.ORDER_LOCK.value in kinds, "the reservation is missing from the ledger"

        for movement in movements:
            # The database enforces this too, but asserting it here makes the
            # ledger's contract explicit and keeps the test meaningful if the
            # constraint is ever dropped by mistake.
            assert movement.delta_available == movement.after_available - movement.before_available
            assert movement.delta_locked == movement.after_locked - movement.before_locked


def test_replaying_the_same_idempotency_key_does_not_double_deduct(seeded, engine) -> None:
    """INV-003: the same stock is never deducted twice."""
    sku_id = seeded["sku"]
    warehouse_id = seeded["warehouse"]
    factory = get_session_factory()
    key = f"fg09-idem-{uuid.uuid4().hex}"

    with factory() as session:
        InventoryService(session).reserve(
            sku_id=sku_id, quantity=1, warehouse_id=warehouse_id, idempotency_key=key
        )
        session.commit()

    available_after_first, locked_after_first, _ = _read_position(engine, sku_id, warehouse_id)

    with factory() as session:
        replay = InventoryService(session).reserve(
            sku_id=sku_id, quantity=1, warehouse_id=warehouse_id, idempotency_key=key
        )
        session.commit()

    assert replay.replayed is True, "the replay should be recognised as already applied"

    available_after_replay, locked_after_replay, _ = _read_position(engine, sku_id, warehouse_id)
    assert (available_after_replay, locked_after_replay) == (
        available_after_first,
        locked_after_first,
    ), "a replayed reservation changed the balance - INV-003 is broken"

    with factory() as session:
        movement_count = session.execute(
            select(func.count())
            .select_from(InventoryMovement)
            .where(InventoryMovement.idempotency_key == key)
        ).scalar_one()
        assert movement_count == 1, "a replay produced a second ledger row"


# ---------------------------------------------------------------------------
# Negative control - proves the lock is doing the work
# ---------------------------------------------------------------------------
def test_naive_read_then_write_DOES_oversell(engine) -> None:
    """The control that makes the positive test meaningful.

    Implements the *wrong* algorithm deliberately - read, decide, then write,
    with no lock - and forces two transactions to interleave with a barrier. If
    this did **not** oversell, the lock in :meth:`InventoryService.reserve` would
    be decorative and the gate above would be proving nothing.

    This is the difference between "my test passed" and "my test is testing
    something".
    """
    factory = get_session_factory()
    marker = uuid.uuid4().hex[:10]

    with factory() as session:
        merchant = Merchant(code=f"NC{marker}"[:24], name="Negative Control")
        session.add(merchant)
        session.flush()
        product = Product(
            merchant_id=merchant.id,
            product_no=f"NP-{marker}",
            slug=f"np-{marker}",
            name="Negative Control Product",
            status="PUBLISHED",
        )
        session.add(product)
        session.flush()
        sku = ProductSku(
            merchant_id=merchant.id,
            product_id=product.id,
            sku_no=f"NS-{marker}",
            sku_code=f"nsku-{marker}",
            name="Negative Control SKU",
            price_amount=100,
        )
        session.add(sku)
        session.flush()
        warehouse = Warehouse(merchant_id=merchant.id, code=f"NW{marker}"[:24], name="NC WH")
        session.add(warehouse)
        session.flush()
        inventory = Inventory(
            merchant_id=merchant.id,
            warehouse_id=warehouse.id,
            sku_id=sku.id,
            available_qty=1,
            locked_qty=0,
        )
        session.add(inventory)
        session.commit()
        ids = {
            "merchant": merchant.id,
            "product": product.id,
            "sku": sku.id,
            "warehouse": warehouse.id,
        }

    both_read = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def naive_reserve() -> None:
        """Read -> (interleave) -> write. No lock. This is the bug, on purpose."""
        session = factory()
        try:
            # Read the freshest committed value.
            row = session.execute(
                text(
                    "SELECT available_qty FROM inventories "
                    "WHERE sku_id = :sku AND warehouse_id = :wh"
                ),
                {"sku": ids["sku"], "wh": ids["warehouse"]},
            ).one()
            observed = int(row[0])

            # Both threads now hold the same observation.
            both_read.wait(timeout=15)

            if observed >= 1:
                # Write based on the stale read - the TOCTOU mistake.
                session.execute(
                    text(
                        "UPDATE inventories SET available_qty = :new "
                        "WHERE sku_id = :sku AND warehouse_id = :wh"
                    ),
                    {"new": observed - 1, "sku": ids["sku"], "wh": ids["warehouse"]},
                )
                session.commit()
                with lock:
                    outcomes.append("accepted")
            else:
                session.rollback()
                with lock:
                    outcomes.append("rejected")
        except Exception:
            session.rollback()
            with lock:
                outcomes.append("error")
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _i: naive_reserve(), range(2)))

    # The naive path accepts BOTH callers for a single unit of stock. The
    # database's CHECK cannot save it, because available_qty = 0 is a perfectly
    # legal value - the corruption is that two customers were told yes.
    assert outcomes.count("accepted") == 2, (
        "the negative control did not reproduce the race; it proves nothing about "
        f"the locking version (outcomes={outcomes}). This test needs to be made "
        "more aggressive, not deleted."
    )

    with engine.connect() as connection:
        final = connection.execute(
            text(
                "SELECT available_qty, locked_qty FROM inventories "
                "WHERE sku_id = :sku AND warehouse_id = :wh"
            ),
            {"sku": ids["sku"], "wh": ids["warehouse"]},
        ).one()
    assert int(final[0]) == 0 and int(final[1]) == 0, (
        "expected the naive path to leave the books inconsistent (0 available, "
        f"0 locked) for a unit that was promised twice; got {tuple(final)}"
    )

    # Tear down the control's fixtures.
    with factory() as session:
        session.execute(delete(Inventory).where(Inventory.sku_id == ids["sku"]))
        session.execute(delete(ProductSku).where(ProductSku.id == ids["sku"]))
        session.execute(delete(Product).where(Product.id == ids["product"]))
        session.execute(delete(Warehouse).where(Warehouse.id == ids["warehouse"]))
        session.execute(delete(Merchant).where(Merchant.id == ids["merchant"]))
        session.commit()
    _ = datetime.now(UTC)  # keep the UTC import meaningful for future assertions
