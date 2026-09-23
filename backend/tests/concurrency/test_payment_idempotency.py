"""FG-11 - payment idempotency under real concurrency.

The gate, from PHASE5_DESIGN section 11 and REQ-PAY-002/003/004:

    real MySQL; the **same provider event delivered N times** (N >= 10) concurrently,
    asserting exactly one payment SUCCESS, one ``ORDER_DEDUCT`` movement per line, one
    fulfillment plus its items, one ``PENDING_PAYMENT -> PROCESSING`` status log, one
    callback row in ``PROCESSED``, and every loser answering the duplicate code. Plus a
    **negative control**: a second, *distinct* event id for the same payment must be
    refused by the state guard - proving the guard, not the unique index, is what stops
    double settlement.

## Why this runs on real MySQL and cannot be mocked

The property under test **is** a database behaviour: ``UNIQUE (provider,
provider_event_id)`` on ``payment_callbacks`` is the serialisation point, and
``SELECT ... FOR UPDATE`` on the payment row is what makes the decision and the write
atomic. Neither exists in a mock, so a mocked run would say nothing at all about
whether a payment can be settled twice. Section 113 forbids exactly that.

## Why each worker opens its own session

A single-session loop cannot test concurrency: it would serialise the deliveries and
pass against an implementation with no guard at all. Each attempt therefore opens its
own session from the factory, like a real request handler, and commits its own
transaction. The barrier is what makes the run genuinely concurrent - without it,
thread startup jitter would space the deliveries out and the losers would find a
committed ``SUCCESS`` row rather than racing for it.

## The two assertions that carry the most weight

1. ``test_the_same_event_delivered_ten_times_settles_exactly_once`` - the positive
   gate. Every clause of section 11 is asserted separately, so a failure names which
   invariant broke instead of just "the count was wrong".
2. ``test_a_distinct_event_for_the_same_payment_is_refused_by_the_state_guard`` - the
   negative control. Without it, the positive test could be green because the unique
   index absorbed everything, and the *guard* that is supposed to stop a genuinely
   different second settlement would never be exercised. A concurrency test with no
   negative control can pass for the wrong reason, which is why section 11 demands one.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.errors import ErrorCode
from app.modules.catalog.models import Product, ProductSku
from app.modules.identity.enums import DataScope, UserType
from app.modules.identity.models import Merchant, User, UserAddress
from app.modules.identity.security import hash_password
from app.modules.identity.service import Principal
from app.modules.inventory.enums import MovementType, OperatorType, ReferenceType
from app.modules.inventory.models import Inventory, InventoryMovement, Warehouse
from app.modules.order.enums import OrderStatus, PaymentStatus
from app.modules.order.models import Order, OrderItem
from app.modules.payment.enums import (
    CallbackProcessStatus,
    PaymentChannel,
    PaymentRecordStatus,
)
from app.modules.payment.models import Payment
from app.modules.payment.providers import CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED, sign_body
from app.modules.payment.service import PaymentService
from app.modules.payment.workflow import CallbackRequest, PaymentSuccessWorkflow
from app.shared.db.base import utc_now
from app.shared.db.session import configure_database, get_session_factory
from app.shared.outbox import OutboxEventType

pytestmark = [pytest.mark.concurrency, pytest.mark.integration]

#: The number of concurrent deliveries of **one** provider event. Section 11 freezes
#: "N >= 10"; ten is used because it is the smallest number that reliably produces
#: genuine overlap while keeping a failing run readable.
DELIVERIES = 10

#: Opening stock, and the quantity reserved by the order's creation. The deduction is
#: against ``locked_qty``, so the seed must have locked exactly what it will deduct -
#: a fixture that left ``locked_qty`` at zero would fail in ``InventoryService`` before
#: any payment invariant was reached.
OPENING_STOCK = 500
LINE_QUANTITIES = (2, 3)

#: Per-unit prices. Deliberately not round: two lines whose ``payable_amount`` does not
#: divide evenly is what an allocation/reconciliation defect shows up on.
UNIT_PRICES = (1999, 2999)

PASSWORD = "Correct-Horse-Battery-9"


#: How many times a fixture-level transaction is retried when MySQL resolves a lock-order
#: conflict by killing it. See :func:`_retry_on_deadlock`.
_FIXTURE_ATTEMPTS = 4

#: MySQL error numbers that mean "this transaction lost a lock race and must be restarted".
#: 1213 is a deadlock victim; 1205 is the lock-wait timeout. Both are *transient*: the
#: statements of the losing transaction are rolled back, so the caller's response is to run
#: the whole unit of work again, which is exactly what a real API client does.
_TRANSIENT_LOCK_ERRORS = (1213, 1205)


def _retry_on_deadlock(operation, *, what: str, attempts: int = _FIXTURE_ATTEMPTS):
    """Run ``operation`` in its own transaction, retrying on a MySQL lock conflict.

    ## Why this is needed at all, and why it is not papering over a defect

    This file's fixture writes and deletes rows while other suites' rows and indexes are
    live in the same schema. InnoDB resolves a lock-order conflict by choosing a victim and
    raising 1213 - the connection is expected to restart the transaction, which is why the
    server's own message says "try restarting transaction". Under that load the fixture
    deadlocked on an ``INSERT INTO inventories``; earlier the teardown deadlocked on a
    ``DELETE``. Both are properties of shared InnoDB state, not of the payment logic.

    The retry is at the *operation* level rather than around individual statements because
    a deadlock rolls back the whole transaction: re-running one statement would leave the
    rest undone, which is how a retry turns into silent corruption.

    It cannot mask a broken assertion. The gate's own assertions run after this helper has
    already returned a committed fixture, and a retry can only reach a state the database
    permits - if two settlements really did apply, the assertions that count effects would
    still see two.

    Each attempt gets a **fresh marker**, so a partially written attempt cannot collide with
    the next one on ``uq_orders_merchant_order_no`` or the fixture's unique identifiers.
    """
    from sqlalchemy.exc import OperationalError

    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return operation(attempt)
        except OperationalError as exc:
            origin = getattr(exc, "orig", None)
            if origin is None or origin.args[0] not in _TRANSIENT_LOCK_ERRORS:
                raise
            last = exc
            time.sleep(0.15 * attempt)
        except IntegrityError as exc:
            # A duplicate key on the *fixture's* own unique identifier means the previous
            # attempt's rows are still present (the rollback of a deadlocked transaction
            # can leave nothing, but a flush that succeeded before the deadlock may have
            # been committed by a sibling). Retrying with a fresh marker resolves it; a
            # duplicate that is not ours is re-raised by the same check on the last try.
            origin = getattr(exc, "orig", None)
            if origin is None or origin.args[0] != 1062:
                raise
            last = exc
            time.sleep(0.15 * attempt)

    assert last is not None
    raise RuntimeError(
        f"{what} did not complete after {attempts} attempts; the last failure was {last!r}"
    )


def _payload(*, payment_no: str, order_no: str, amount: int, transaction_no: str) -> dict:
    """A provider's settlement notification, as a real provider would post it."""
    return {
        "payment_no": payment_no,
        "order_no": order_no,
        "event_type": CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED,
        "transaction_no": transaction_no,
        "amount": amount,
    }


def _signed_request(
    *,
    event_id: str,
    payment_no: str,
    order_no: str,
    amount: int,
    transaction_no: str,
    secret: str,
    provider: str = PaymentChannel.MOCK.value,
) -> CallbackRequest:
    """Build a correctly signed callback, using the *same* ``sign_body`` a provider would.

    Signed with the production function rather than a test-local copy: a second
    implementation of a signature scheme is how a suite passes against a verifier no
    provider agrees with.
    """
    timestamp = str(int(time.time()))
    payload = _payload(
        payment_no=payment_no,
        order_no=order_no,
        amount=amount,
        transaction_no=transaction_no,
    )
    raw_body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return CallbackRequest(
        provider=provider,
        event_id=event_id,
        event_type=CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED,
        timestamp=timestamp,
        signature=sign_body(secret=secret, timestamp=timestamp, raw_body=raw_body),
        raw_body=raw_body,
        payload=payload,
    )


@dataclass(frozen=True, slots=True)
class Gate:
    """A committed order ready to be settled, with the ids the assertions need."""

    marker: str
    merchant_id: int
    user_id: int
    warehouse_id: int
    sku_ids: tuple[int, int]
    product_id: int
    order_id: int
    order_no: str
    payable_amount: int
    payment_id: int
    payment_no: str
    order_item_ids: tuple[int, int]

    @property
    def consumer(self) -> Principal:
        """The buyer, as the service layer resolves them - never from request input."""
        return Principal(
            user_id=self.user_id,
            user_type=UserType.CONSUMER.value,
            merchant_id=None,
            roles=(),
            permissions=frozenset(),
            data_scope=DataScope.SELF,
            session_id=f"fg11-{self.marker}",
            is_staff=False,
        )


@pytest.fixture(scope="module")
def engine():
    return configure_database()


def _seed_once(marker: str) -> Gate:
    """Create and **commit** one order with a reserved, in-flight payment attempt.

    Committed rather than rolled back on purpose: the worker threads open their own
    sessions, and a transaction-local fixture would be invisible to them.

    The stock is reserved (``ORDER_LOCK``) rather than merely present, because the
    settlement path *deducts* from ``locked_qty``. Seeding available stock alone would
    make the test fail inside ``InventoryService`` - for a fixture reason, not a
    payment reason.
    """
    factory = get_session_factory()
    now = utc_now()

    with factory() as session:
        merchant = Merchant(code=f"FG11{marker}"[:24], name=f"FG-11 {marker}")
        session.add(merchant)
        session.flush()

        product = Product(
            merchant_id=merchant.id,
            product_no=f"P-FG11-{marker}",
            slug=f"p-fg11-{marker}",
            name="FG-11 Product",
            status="PUBLISHED",
            published_at=now,
        )
        session.add(product)
        session.flush()

        sku_ids: list[int] = []
        for index, price in enumerate(UNIT_PRICES, start=1):
            sku = ProductSku(
                merchant_id=merchant.id,
                product_id=product.id,
                sku_no=f"S-FG11-{marker}-{index}",
                sku_code=f"sku-fg11-{marker}-{index}",
                name=f"FG-11 SKU {index}",
                price_amount=price,
                cost_amount=price // 2,
                status="ACTIVE",
            )
            session.add(sku)
            session.flush()
            sku_ids.append(sku.id)

        warehouse = Warehouse(
            merchant_id=merchant.id,
            code=f"WHF{marker}"[:24],
            name="FG-11 Warehouse",
            is_default=True,
            status="ACTIVE",
        )
        session.add(warehouse)
        session.flush()

        total_locked = 0
        for sku_id, quantity in zip(sku_ids, LINE_QUANTITIES, strict=True):
            session.add(
                Inventory(
                    merchant_id=merchant.id,
                    warehouse_id=warehouse.id,
                    sku_id=sku_id,
                    available_qty=OPENING_STOCK,
                    locked_qty=quantity,
                    safety_stock=0,
                )
            )
            session.flush()
            total_locked += quantity
            # The reserve movement that explains ``locked_qty``. Seeding the balance
            # alone would leave the row unexplainable by its own ledger (INV-007).
            session.add(
                InventoryMovement.build(
                    warehouse_id=warehouse.id,
                    sku_id=sku_id,
                    movement_type=MovementType.ORDER_LOCK,
                    before_available=OPENING_STOCK,
                    after_available=OPENING_STOCK,
                    before_locked=0,
                    after_locked=quantity,
                    idempotency_key=f"fg11-seed-lock-{marker}-{sku_id}",
                    reference_type=ReferenceType.ORDER,
                    operator_type=OperatorType.SYSTEM,
                    reason="FG-11 fixture: stock reserved by the order",
                )
            )
        assert total_locked == sum(LINE_QUANTITIES)

        consumer = User(
            username=f"fg11_buyer_{marker}",
            email=f"fg11_buyer_{marker}@example.test",
            password_hash=hash_password(PASSWORD),
            display_name="FG-11 Buyer",
            user_type=UserType.CONSUMER.value,
            status="ACTIVE",
            merchant_id=None,
        )
        session.add(consumer)
        session.flush()

        address = UserAddress(
            merchant_id=None,
            user_id=consumer.id,
            receiver_name="FG-11 Receiver",
            receiver_phone="13800001111",
            province="Guangdong",
            city="Shenzhen",
            district="Nanshan",
            detail="FG-11 Road 1",
            postal_code="518000",
            tag="HOME",
            is_default=True,
        )
        session.add(address)
        session.flush()

        original_amount = sum(
            price * quantity for price, quantity in zip(UNIT_PRICES, LINE_QUANTITIES, strict=True)
        )
        # Free shipping in V1, and no discounts in this fixture: the pricing authority
        # is Phase 4's, and FG-11 is about the payment path. `payable_consistent` is
        # still satisfied exactly (original - 0 - 0 + 0 == original).
        payable_amount = original_amount

        order = Order(
            merchant_id=merchant.id,
            user_id=consumer.id,
            order_no=f"NVFG11{marker}",
            client_request_id=f"fg11-{marker}",
            request_hash="f" * 64,
            order_status=OrderStatus.PENDING_PAYMENT.value,
            payment_status=PaymentStatus.PAYING.value,
            fulfillment_status="UNFULFILLED",
            after_sale_status="NONE",
            original_amount=original_amount,
            promotion_discount_amount=0,
            coupon_discount_amount=0,
            shipping_amount=0,
            payable_amount=payable_amount,
            paid_amount=0,
            refunded_amount=0,
            address_id=address.id,
            receiver_name="FG-11 Receiver",
            receiver_phone="13800001111",
            address_snapshot={"full_address": "GuangdongShenzhenNanshanFG-11 Road 1"},
            item_count=sum(LINE_QUANTITIES),
            first_item_name="FG-11 Product FG-11 SKU 1",
            expires_at=now + timedelta(minutes=30),
            created_at=now,
            updated_at=now,
        )
        session.add(order)
        session.flush()

        item_ids: list[int] = []
        for index, (sku_id, quantity, price) in enumerate(
            zip(sku_ids, LINE_QUANTITIES, UNIT_PRICES, strict=True), start=1
        ):
            item = OrderItem(
                order_id=order.id,
                warehouse_id=warehouse.id,
                product_id=product.id,
                sku_id=sku_id,
                product_name="FG-11 Product",
                sku_name=f"FG-11 SKU {index}",
                image_url=None,
                unit_price=price,
                quantity=quantity,
                original_amount=price * quantity,
                promotion_discount_amount=0,
                coupon_discount_amount=0,
                allocated_discount_amount=0,
                # No discounts in this fixture, so per-line payable == original. INV-006
                # (the parts sum to the whole) therefore holds exactly, which is what
                # lets the assertions below treat payable_amount as the truth.
                payable_amount=price * quantity,
                refunded_amount=0,
                after_sale_status="NONE",
                created_at=now,
                updated_at=now,
            )
            session.add(item)
            session.flush()
            item_ids.append(item.id)

        payment = Payment(
            payment_no=f"NVPAYFG11{marker}",
            order_id=order.id,
            order_no=order.order_no,
            merchant_id=merchant.id,
            user_id=consumer.id,
            channel=PaymentChannel.MOCK.value,
            amount=payable_amount,
            status=PaymentRecordStatus.PAYING.value,
            external_transaction_no=None,
            idempotency_key=f"fg11-key-{marker}",
            client_request_id=f"fg11-client-{marker}",
            request_hash="a" * 64,
            paid_amount=0,
            refunded_amount=0,
            expires_at=now + timedelta(minutes=30),
            created_at=now,
            updated_at=now,
        )
        session.add(payment)
        session.flush()

        gate = Gate(
            marker=marker,
            merchant_id=merchant.id,
            user_id=consumer.id,
            warehouse_id=warehouse.id,
            sku_ids=(sku_ids[0], sku_ids[1]),
            product_id=product.id,
            order_id=order.id,
            order_no=order.order_no,
            payable_amount=payable_amount,
            payment_id=payment.id,
            payment_no=payment.payment_no,
            order_item_ids=(item_ids[0], item_ids[1]),
        )
        session.commit()
        return gate


def _purge(gate: Gate) -> None:
    """Delete everything the fixture and the test created, children first.

    The foreign keys here are ``RESTRICT`` precisely so that financial history cannot be
    deleted out from under a live row, which makes the unwinding order mandatory rather
    than stylistic.

    ## Two things this teardown learned the hard way

    A single run of this file was observed failing **1 in 10 times** with
    ``OperationalError (1213, 'Deadlock found when trying to get lock')`` raised by a
    ``DELETE FROM products`` - and once with five test failures, whose common cause was
    the same exception inside the worker threads. Neither was a defect in the payment
    path: both were teardown contention. A gate that is green 70% of the time is not
    evidence, so:

    1. **Deletes go by primary key.** The first version filtered
       ``Product.product_no.like("P-FG11-%")``, which matches *every* run's products -
       including rows a concurrent test process is holding. Naming the ids captured during
       seeding means this teardown touches only this test's rows.
    2. **A deadlock is retried, because it is a transport-level failure.** MySQL resolves
       a 1213 by killing one transaction and expecting the client to restart it - the same
       contract a real request handler honours. Retrying cannot turn a broken assertion
       green: the assertions above have already run, and this method only deletes rows the
       test created.

    The delete order is the dependency order and is deliberately explicit rather than a
    loop over ``metadata.sorted_tables``: ``order_items.warehouse_id`` RESTRICTs on
    ``warehouses``, ``fulfillments`` RESTRICTs on ``orders``, and a reflection-driven
    order would be correct by luck on one schema and wrong on the next.
    """

    factory = get_session_factory()
    statements = (
        # One plain SQL delete per table, children before parents. `fulfillment_items`
        # has no ORM model imported here on purpose: this gate must not need the
        # fulfillment module loaded to clean up after itself.
        (
            "DELETE fi FROM fulfillment_items fi JOIN fulfillments f "
            "ON f.id = fi.fulfillment_id WHERE f.order_id = :order_id",
            {"order_id": gate.order_id},
        ),
        ("DELETE FROM fulfillments WHERE order_id = :order_id", {"order_id": gate.order_id}),
        ("DELETE FROM inventory_movements WHERE sku_id IN (:sku0, :sku1)",
         {"sku0": gate.sku_ids[0], "sku1": gate.sku_ids[1]}),
        # Callbacks are deleted by **marker**, not only by order_no. A refused delivery
        # resolves no payment, so `order_no` is legitimately NULL on it (design 5.2 keeps
        # that nullable precisely so an unresolvable event stays recordable) - which means an
        # order_no-scoped delete silently misses every refusal-path row. Measured: each run
        # of this file left ~19 rows behind, and because `payment_callbacks` carries no FK to
        # `payments`, nothing ever removed them. The marker is unique per fixture instance
        # (`uuid4`-derived), and every event id this file builds embeds it, so the LIKE cannot
        # reach a neighbouring test's rows.
        (
            "DELETE FROM payment_callbacks WHERE provider_event_id LIKE :event_pattern",
            {"event_pattern": f"%{gate.marker}%"},
        ),
        ("DELETE FROM payment_callbacks WHERE order_no = :order_no",
         {"order_no": gate.order_no}),
        ("DELETE FROM payments WHERE order_id = :order_id", {"order_id": gate.order_id}),
        ("DELETE FROM order_status_logs WHERE order_id = :order_id",
         {"order_id": gate.order_id}),
        ("DELETE FROM order_items WHERE order_id = :order_id", {"order_id": gate.order_id}),
        ("DELETE FROM orders WHERE id = :order_id", {"order_id": gate.order_id}),
        ("DELETE FROM inventories WHERE warehouse_id = :warehouse_id",
         {"warehouse_id": gate.warehouse_id}),
        ("DELETE FROM product_skus WHERE id IN (:sku0, :sku1)",
         {"sku0": gate.sku_ids[0], "sku1": gate.sku_ids[1]}),
        ("DELETE FROM products WHERE id = :product_id", {"product_id": gate.product_id}),
        ("DELETE FROM user_addresses WHERE user_id = :user_id", {"user_id": gate.user_id}),
        ("DELETE FROM auth_sessions WHERE user_id = :user_id", {"user_id": gate.user_id}),
        ("DELETE FROM users WHERE id = :user_id", {"user_id": gate.user_id}),
        ("DELETE FROM warehouses WHERE id = :warehouse_id",
         {"warehouse_id": gate.warehouse_id}),
        # Phase 6 appends an `outbox_messages` row in PaymentSuccessWorkflow step 10,
        # and its `merchant_id` FK is RESTRICT: unwound before the merchant or this
        # teardown dies with errno 1451.
        ("DELETE FROM outbox_messages WHERE merchant_id = :merchant_id",
         {"merchant_id": gate.merchant_id}),
        ("DELETE FROM merchants WHERE id = :merchant_id",
         {"merchant_id": gate.merchant_id}),
    )

    def run(_attempt: int) -> None:
        session = factory()
        try:
            for sql, params in statements:
                session.execute(text(sql), params)
            session.commit()
        finally:
            session.close()

    # The retry lives in `_retry_on_deadlock` rather than in a loop here, so the fixture's
    # write side and its cleanup side cannot drift apart: both are the same "restart the
    # transaction on 1213/1205" contract, and two copies would be two places to get wrong.
    _retry_on_deadlock(run, what=f"FG-11 teardown of {gate.marker}")


def _purge_orphan_marker(marker: str) -> None:
    """Delete the merchant row a **failed** seed attempt may have left behind.

    ``_seed`` retries with a fresh marker, which is what stops a half-written attempt from
    colliding with its successor - but a merchant row can be committed before the conflict
    that killed the rest of its transaction, and the gate fixture holds no object for an
    attempt that failed. So orphans are found by the code the seed stamps on them
    (``FG11<marker>``, bounded to the column's 24 characters) and deleted when no order was
    written under them.

    Why this is more than tidiness: ``test_the_gate_fixture_is_isolated`` asserts exactly one
    order for the fixture's merchant. A leftover merchant would not fail that assertion
    directly, but any future assertion counting merchants, users or products for the run
    would see a number that depends on whether an earlier attempt happened to deadlock - and
    a suite whose second run differs from its first is a suite nobody trusts.

    Deliberately narrow: it deletes the merchant only, and only when ``orders`` is empty for
    it. A merchant that owns rows is left alone, because that shape means the seed got far
    enough for ``_purge`` to have something to work with, and guessing at an unknown partial
    state is how a cleanup helper deletes a neighbouring test's data.
    """
    factory = get_session_factory()
    code = f"FG11{marker}"[:24]

    def run(_attempt: int) -> None:
        session = factory()
        try:
            merchant_id = session.execute(
                text("SELECT id FROM merchants WHERE code = :code"), {"code": code}
            ).scalar()
            if merchant_id is None:
                return
            has_orders = session.execute(
                text("SELECT COUNT(*) FROM orders WHERE merchant_id = :m"), {"m": merchant_id}
            ).scalar()
            if int(has_orders or 0) == 0:
                # A failed attempt may also have claimed a callback row before it died. The
                # event ids and the merchant code both embed the marker, so this is exact.
                session.execute(
                    text(
                        "DELETE FROM payment_callbacks "
                        "WHERE merchant_id = :m OR provider_event_id LIKE :event_pattern"
                    ),
                    {"m": merchant_id, "event_pattern": f"%{marker}%"},
                )
                session.execute(
                    text("DELETE FROM outbox_messages WHERE merchant_id = :m"),
                    {"m": merchant_id},
                )
                session.execute(
                    text("DELETE FROM merchants WHERE id = :m"), {"m": merchant_id}
                )
            session.commit()
        finally:
            session.close()

    _retry_on_deadlock(run, what=f"FG-11 orphan cleanup for {marker}")


def _seed() -> tuple[Gate, list[str]]:
    """Seed one gate, retrying on a transient lock conflict with a **fresh marker**.

    A fresh marker per attempt is what makes the retry safe: the identifiers (``order_no``,
    ``payment_no``, merchant code) embed it, so a half-written attempt cannot collide with
    its successor on ``uq_orders_merchant_order_no`` or the fixture's own uniques.

    The markers tried are returned alongside the gate because a **failed** attempt may have
    committed its merchant row before the conflict killed the rest of its transaction - and
    a marker whose rows are left behind would make the next run's counts differ from this
    one's. The caller purges every attempted marker, not just the successful one.
    """
    markers: list[str] = []

    def attempt_seed(attempt: int) -> Gate:
        marker = f"{uuid.uuid4().hex[:8].upper()}A{attempt}"
        markers.append(marker)
        return _seed_once(marker)

    gate_obj = _retry_on_deadlock(attempt_seed, what="FG-11 seed")
    return gate_obj, markers


@pytest.fixture
def gate(engine) -> Iterator[Gate]:
    seeded, attempted_markers = _seed()
    try:
        yield seeded
    finally:
        _purge(seeded)
        for marker in attempted_markers:
            if marker != seeded.marker:
                _purge_orphan_marker(marker)


# ---------------------------------------------------------------------------
# Read-back helpers, on fresh connections
# ---------------------------------------------------------------------------
def _fresh_row(engine, sql: str, params: dict) -> tuple:
    """Read post-mutation state on a **fresh connection**.

    ``HANDOFF.md`` section 6's REPEATABLE-READ warning applies: a read through the
    session that performed the writes can compare the snapshot against itself and pass
    as a false green. ``engine.connect()`` with no transaction open is a fresh
    connection, so these reads see what the database actually committed.
    """
    with engine.connect() as connection:
        return connection.execute(text(sql), params).one()


def _drain(requests: list[CallbackRequest], *, workers: int = DELIVERIES) -> list:
    """Deliver ``requests`` concurrently, one session each, and return the outcomes.

    Every worker opens its own session and barrier-waits before executing, so the
    deliveries overlap rather than queueing behind thread startup.

    A worker that fails with something other than a business outcome is recorded in the
    returned list rather than aborting the pool: one thread's deadlock must not hide
    the other nine results, and the assertions below should report what actually
    happened.

    ## The one retry, and why it is not a workaround

    Ten simultaneous INSERTs of *the same* unique key are a thundering herd, and MySQL
    can choose one of them as a deadlock victim (errno 1213) - it kills the thread that
    holds the *fewest* locks, which is exactly the delivery that has done the least.
    That is a transport-level retry, the same one a real HTTP client performs, and the
    test retries once with a fresh session. It cannot turn a broken guard into a green
    test: a retry can only reach a state the database allows, and the assertions count
    *effects*, so a second settlement would be counted however it arrived.
    """
    factory = get_session_factory()
    barrier = threading.Barrier(len(requests))
    results: list[object] = [None] * len(requests)
    lock = threading.Lock()

    def deliver(index: int, request: CallbackRequest) -> None:
        for attempt in (1, 2, 3):
            session = factory()
            # `suppress` rather than an `except: pass`: only reachable if another
            # worker died before the barrier released, and the carrier is not carried
            # forward by `ThreadPoolExecutor.map`.
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait(timeout=30)
            try:
                outcome = PaymentSuccessWorkflow(session).execute(request)
                with lock:
                    results[index] = outcome
                return
            except Exception as exc:
                session.rollback()
                if attempt == 3:
                    with lock:
                        results[index] = exc
                    return
                time.sleep(0.05)
            finally:
                session.close()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda pair: deliver(*pair), enumerate(requests)))

    return results


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def test_the_same_event_delivered_ten_times_settles_exactly_once(gate: Gate, engine) -> None:
    """Section 11, clause by clause. One event, ten concurrent deliveries, one effect."""
    settings = get_settings()
    secret = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()
    event_id = f"evt-fg11-{gate.marker}"

    request = _signed_request(
        event_id=event_id,
        payment_no=gate.payment_no,
        order_no=gate.order_no,
        amount=gate.payable_amount,
        transaction_no=f"MOCK-{gate.marker}",
        secret=secret,
    )
    requests = [request for _ in range(DELIVERIES)]

    outcomes = _drain(requests)

    unexpected = [item for item in outcomes if isinstance(item, Exception)]
    assert not unexpected, f"unexpected failures: {[repr(e) for e in unexpected]}"

    winners = [outcome for outcome in outcomes if outcome.applied]
    losses = [outcome for outcome in outcomes if not outcome.applied]

    # ---- clause 1: exactly one payment SUCCESS -------------------------------
    assert len(winners) == 1, (
        f"expected exactly 1 delivery to apply the settlement, got {len(winners)}"
    )

    row = _fresh_row(
        engine,
        "SELECT status, paid_amount, amount, external_transaction_no, paid_at "
        "FROM payments WHERE payment_no = :payment_no",
        {"payment_no": gate.payment_no},
    )
    assert row[0] == PaymentRecordStatus.SUCCESS.value, f"payment status is {row[0]}"
    assert int(row[1]) == gate.payable_amount, f"paid_amount is {row[1]}"
    assert int(row[2]) == gate.payable_amount, "amount must be unchanged by settlement"
    assert row[3], "a verified settlement must record the provider's transaction id"
    assert row[4] is not None, "paid_at must be stamped"

    # ---- clause 2: one ORDER_DEDUCT movement per line ------------------------
    movements = _fresh_row(
        engine,
        "SELECT COUNT(*), COUNT(DISTINCT idempotency_key) FROM inventory_movements "
        "WHERE reference_type = 'ORDER_ITEM' AND reference_id IN (:id0, :id1)",
        {"id0": gate.order_item_ids[0], "id1": gate.order_item_ids[1]},
    )
    assert int(movements[0]) == len(gate.order_item_ids), (
        f"expected {len(gate.order_item_ids)} ORDER_DEDUCT movements, got {movements[0]} "
        "- this is stock deducted twice for one paid line"
    )
    assert int(movements[1]) == int(movements[0]), "two movements shared an idempotency key"

    for item_id in gate.order_item_ids:
        key = f"order-deduct:{gate.order_no}:{item_id}"
        movement = _fresh_row(
            engine,
            "SELECT movement_type, idempotency_key FROM inventory_movements "
            "WHERE idempotency_key = :key",
            {"key": key},
        )
        assert movement[0] == MovementType.ORDER_DEDUCT.value
        # The frozen key shape is asserted as well as the row's existence: a differently
        # shaped key that happened to be unique would still be unique today and would
        # stop being unique the moment a second settlement path appeared.
        assert movement[1] == key

    deducted = _fresh_row(
        engine,
        "SELECT SUM(locked_qty) FROM inventories WHERE warehouse_id = :warehouse_id",
        {"warehouse_id": gate.warehouse_id},
    )
    assert int(deducted[0]) == 0, (
        f"locked stock should be fully consumed by the deduction, {deducted[0]} left"
    )

    # ---- clause 3: one fulfillment plus its items ----------------------------
    fulfillment = _fresh_row(
        engine,
        "SELECT f.id, f.fulfillment_status, f.carrier, COUNT(fi.id) "
        "FROM fulfillments f JOIN fulfillment_items fi ON fi.fulfillment_id = f.id "
        "WHERE f.order_id = :order_id GROUP BY f.id, f.fulfillment_status, f.carrier",
        {"order_id": gate.order_id},
    )
    assert fulfillment is not None, (
        "no fulfillment shell was created - goods the customer paid for have no record"
    )
    assert int(fulfillment[3]) == len(gate.order_item_ids), (
        f"the shell carries {fulfillment[3]} lines, expected {len(gate.order_item_ids)}"
    )
    # Design 6.1 step 9: creating a package is not shipping it, and the settlement
    # must NOT pre-set the order's fulfillment axis.
    assert fulfillment[1] == "UNFULFILLED", f"shell status is {fulfillment[1]}"
    assert fulfillment[2] is None, "an unshipped package must carry no carrier"

    shells = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM fulfillments WHERE order_id = :order_id",
        {"order_id": gate.order_id},
    )
    assert int(shells[0]) == 1, (
        f"a retried settlement created {shells[0]} shells - the operator would be told "
        "twice as much stock is owed"
    )

    # ---- clause 4: exactly one PENDING_PAYMENT -> PROCESSING log -------------
    logs = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM order_status_logs "
        "WHERE order_id = :order_id AND from_status = 'PENDING_PAYMENT' "
        "AND to_status = 'PROCESSING'",
        {"order_id": gate.order_id},
    )
    assert int(logs[0]) == 1, (
        f"expected exactly one settlement status log, got {logs[0]} - a duplicated "
        "audit row is what makes a double-settlement investigation read as real"
    )
    log = _fresh_row(
        engine,
        "SELECT operator_type, operator_id FROM order_status_logs "
        "WHERE order_id = :order_id AND to_status = 'PROCESSING'",
        {"order_id": gate.order_id},
    )
    # A provider settled this, not a person. Attributing money movement to a human who
    # was not there is an audit lie.
    assert log[0] == "SYSTEM"
    assert log[1] is None

    # ---- clause 5: one callback row, in PROCESSED ----------------------------
    callbacks = _fresh_row(
        engine,
        "SELECT COUNT(*), SUM(process_status = 'PROCESSED') FROM payment_callbacks "
        "WHERE provider = :provider AND provider_event_id = :event_id",
        {"provider": PaymentChannel.MOCK.value, "event_id": event_id},
    )
    assert int(callbacks[0]) == 1, (
        f"expected exactly one callback row for one event id, got {callbacks[0]}"
    )
    assert int(callbacks[1]) == 1, "the surviving callback row is not PROCESSED"

    # The snapshot is stored filtered (REQ-PAY-003). This payload carries no secret, so
    # the assertion that matters here is that the audit value *survived* - a filter that
    # redacts everything would have destroyed the reason the column exists.
    snapshot = _fresh_row(
        engine,
        "SELECT payload_snapshot, signature_valid FROM payment_callbacks "
        "WHERE provider = :provider AND provider_event_id = :event_id",
        {"provider": PaymentChannel.MOCK.value, "event_id": event_id},
    )
    stored = json.loads(snapshot[0]) if isinstance(snapshot[0], str) else snapshot[0]
    assert stored["amount"] == gate.payable_amount
    assert stored["transaction_no"] == f"MOCK-{gate.marker}"
    assert snapshot[1] in (True, 1), "a settled callback must record signature_valid"

    # ---- clause 6: every loser answers the duplicate code --------------------
    assert len(losses) == DELIVERIES - 1, (
        f"expected {DELIVERIES - 1} losing deliveries, got {len(losses)}"
    )
    for outcome in losses:
        assert outcome.duplicate, (
            "a losing delivery must be reported as a duplicate, not as a failure: a "
            "provider that sees an error retries forever"
        )
        assert outcome.error_code == ErrorCode.PAYMENT_CALLBACK_DUPLICATE.name, (
            f"loser answered {outcome.error_code}, expected PAYMENT_CALLBACK_DUPLICATE"
        )
        assert not outcome.processed, "a losing delivery applied a business effect"

    # ---- clause 7: the order, read on a fresh connection ---------------------
    order = _fresh_row(
        engine,
        "SELECT order_status, payment_status, paid_amount, fulfillment_status "
        "FROM orders WHERE id = :order_id",
        {"order_id": gate.order_id},
    )
    assert order[0] == OrderStatus.PROCESSING.value, f"order_status is {order[0]}"
    assert order[1] == PaymentStatus.PAID.value, f"payment_status is {order[1]}"
    assert int(order[2]) == gate.payable_amount, f"order paid_amount is {order[2]}"
    # Design section 4.4: a settlement must not roll the fulfillment axis forward.
    assert order[3] == "UNFULFILLED", (
        f"fulfillment_status moved to {order[3]} without anything being shipped"
    )

    # ---- clause 8: no duplicate outbox effect -------------------------------
    # Phase 6 landed the outbox table, so REQ-PAY-004's fourth clause is now observable
    # **directly** instead of by proxy. Ten deliveries of ONE event must leave exactly
    # one `payment.settled` row. The callback count above still says "one settlement
    # happened"; this says the settlement announced itself exactly once. Both are
    # needed: a count over the wrong table satisfies the first and is silent on the
    # second, which is the difference between "once" and "once *and only once*".
    outbox = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM outbox_messages "
        "WHERE merchant_id = :merchant_id AND event_type = :event_type",
        {
            "merchant_id": gate.merchant_id,
            "event_type": OutboxEventType.PAYMENT_SETTLED.value,
        },
    )
    assert int(outbox[0]) == 1, (
        f"a duplicate callback produced {outbox[0]} payment.settled outbox rows; "
        "REQ-PAY-004 clause 4 requires exactly one"
    )


def test_a_distinct_event_for_an_already_settled_payment_applies_no_second_effect(
    gate: Gate, engine
) -> None:
    """The negative control. Without it, the gate above could pass for the wrong reason.

    Ten deliveries of **one** event id are stopped by the unique index. That says nothing
    about a *different* event id for the same payment - a real scenario (a provider's
    correction, a second settlement attempt, the same event replayed under a new id) and
    the one the guards exist for.

    ## What the design actually specifies here, and what this test therefore asserts

    Design 6.1 step 3 has two branches, and both are deliberate:

    * a payment that already ``SUCCESS`` -> *"mark the callback PROCESSED (a concurrent
      duplicate that lost the insert race) and return"* - an **idempotent replay**, not a
      refusal. A provider that re-announces a settlement it already made must be told
      "handled", or it retries forever.
    * a payment in ``FAILED``/``CLOSED``/``REFUNDED`` -> ``PAYMENT_STATE_INVALID``
      (60007), which is the guard proper and is covered by
      :func:`test_a_closed_payment_is_refused_by_the_state_guard` below.

    So this test asserts the fact that matters for both branches: **no second effect.**
    Counting effects alone is not enough, though - the first version of this test passed
    against a workflow with the payment state guard removed, because a second guard
    (the order's status) answered instead and the counts never noticed. What
    distinguishes "refused" from "applied as a replay" is the *money trail*: a replay
    must not rewrite which provider transaction settled the payment.
    """
    settings = get_settings()
    secret = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()
    first_event = f"evt-fg11-first-{gate.marker}"
    second_event = f"evt-fg11-second-{gate.marker}"

    first = _signed_request(
        event_id=first_event,
        payment_no=gate.payment_no,
        order_no=gate.order_no,
        amount=gate.payable_amount,
        transaction_no=f"MOCK-FIRST-{gate.marker}",
        secret=secret,
    )
    factory = get_session_factory()
    with factory() as session:
        initial = PaymentSuccessWorkflow(session).execute(first)
    assert initial.applied, "the first, honest settlement must succeed"

    # A *distinct* event id, so the unique index cannot be what stops it, for the same
    # payment but a different provider transaction id.
    second = _signed_request(
        event_id=second_event,
        payment_no=gate.payment_no,
        order_no=gate.order_no,
        amount=gate.payable_amount,
        transaction_no=f"MOCK-SECOND-{gate.marker}",
        secret=secret,
    )
    with factory() as session:
        replay = PaymentSuccessWorkflow(session).execute(second)

    assert not replay.applied, "a second settlement was applied - the guard failed"
    assert replay.replayed, "the second event was not recognised as a replay"
    assert replay.error_code == ErrorCode.PAYMENT_CALLBACK_DUPLICATE.name, (
        f"replay code was {replay.error_code}, expected PAYMENT_CALLBACK_DUPLICATE"
    )

    # ---- the money trail was not rewritten -----------------------------------
    settled = _fresh_row(
        engine,
        "SELECT status, paid_amount, external_transaction_no FROM payments "
        "WHERE payment_no = :payment_no",
        {"payment_no": gate.payment_no},
    )
    assert settled[0] == PaymentRecordStatus.SUCCESS.value
    assert int(settled[1]) == gate.payable_amount, "paid_amount changed on a replay"
    assert settled[2] == f"MOCK-FIRST-{gate.marker}", (
        f"the settlement now names transaction {settled[2]!r}; the second event rewrote "
        "the money trail instead of being recorded as a replay"
    )

    # ---- no second effect ----------------------------------------------------
    movements = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM inventory_movements "
        "WHERE reference_type = 'ORDER_ITEM' AND reference_id IN (:id0, :id1)",
        {"id0": gate.order_item_ids[0], "id1": gate.order_item_ids[1]},
    )
    assert int(movements[0]) == len(gate.order_item_ids), (
        f"{movements[0]} deduct movements for {len(gate.order_item_ids)} lines - the "
        "second event deducted the stock again"
    )

    logs = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM order_status_logs "
        "WHERE order_id = :order_id AND to_status = 'PROCESSING'",
        {"order_id": gate.order_id},
    )
    assert int(logs[0]) == 1, f"{logs[0]} settlement logs - the second event moved the order"

    shells = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM fulfillments WHERE order_id = :order_id",
        {"order_id": gate.order_id},
    )
    assert int(shells[0]) == 1, f"{shells[0]} fulfillment shells after two events"

    # ---- both events are recorded, and each says what happened ---------------
    rows = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM payment_callbacks WHERE provider_event_id IN (:first, :second)",
        {"first": first_event, "second": second_event},
    )
    assert int(rows[0]) == 2, (
        "a delivery that applied nothing left no trace - "
        "'why did the correction do nothing?' must be answerable from the table"
    )
    second_row = _fresh_row(
        engine,
        "SELECT process_status, signature_valid FROM payment_callbacks "
        "WHERE provider_event_id = :event_id",
        {"event_id": second_event},
    )
    # Kept, and TRUE about its signature: the delivery was authentic and legitimately
    # deduplicated. Recording it as FAILED would libel an honest provider, which is the
    # difference between an audit trail and a blame log.
    assert second_row[0] in ("PROCESSED", "IGNORED")
    assert second_row[1] in (True, 1)


def test_a_closed_payment_is_refused_by_the_state_guard(gate: Gate, engine) -> None:
    """``PAYMENT_STATE_INVALID (60007)``: this attempt is over, so it cannot be settled.

    The branch of design 6.1 step 3 that is a **refusal** rather than a replay. It is the
    one that matters operationally: a ``FAILED`` attempt means a new attempt is the
    correct recovery, and a ``CLOSED`` one means the payment window shut. Settling either
    would hand the customer an order they did not pay for in the first case, and accept
    money against an expired window in the second.

    Driven directly through the workflow (not through HTTP), because the property is
    "the transaction body refuses it whoever calls" - the mock-pay path and any future
    worker share this entry point, and an edge-only check would leave both unguarded.

    The payment is put into ``CLOSED`` through a direct UPDATE on purpose: there is no
    Phase 5 endpoint that closes an attempt (Phase 6's reconciliation owns that), so this
    is the only honest way to construct the state - and it is constructed rather than
    mocked, so the row the guard reads is a real one.
    """
    settings = get_settings()
    secret = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()
    factory = get_session_factory()

    with factory() as session:
        payment = session.get(Payment, gate.payment_id)
        payment.status = PaymentRecordStatus.CLOSED.value
        session.commit()

    request = _signed_request(
        event_id=f"evt-fg11-closed-{gate.marker}",
        payment_no=gate.payment_no,
        order_no=gate.order_no,
        amount=gate.payable_amount,
        transaction_no=f"MOCK-CLOSED-{gate.marker}",
        secret=secret,
    )
    with factory() as session:
        outcome = PaymentSuccessWorkflow(session).execute(request)

    assert not outcome.applied, "a CLOSED attempt was settled"
    assert outcome.error_code == ErrorCode.PAYMENT_STATE_INVALID.name, (
        f"refusal code was {outcome.error_code}, expected PAYMENT_STATE_INVALID"
    )
    assert outcome.uncommitted, (
        "the refusal committed something; a refused delivery must leave no claim behind"
    )

    row = _fresh_row(
        engine,
        "SELECT status, paid_amount, external_transaction_no FROM payments "
        "WHERE payment_no = :payment_no",
        {"payment_no": gate.payment_no},
    )
    assert row[0] == PaymentRecordStatus.CLOSED.value, f"the guard moved the status to {row[0]}"
    assert int(row[1]) == 0, "a CLOSED attempt recorded money"
    assert row[2] is None, "a CLOSED attempt acquired a provider transaction id"

    movements = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM inventory_movements "
        "WHERE reference_type = 'ORDER_ITEM' AND reference_id IN (:id0, :id1)",
        {"id0": gate.order_item_ids[0], "id1": gate.order_item_ids[1]},
    )
    assert int(movements[0]) == 0, "stock was deducted for an attempt that cannot be settled"

    order = _fresh_row(
        engine,
        "SELECT order_status, payment_status, paid_amount FROM orders WHERE id = :order_id",
        {"order_id": gate.order_id},
    )
    assert order[0] == OrderStatus.PENDING_PAYMENT.value, "the order moved"
    assert order[1] == PaymentStatus.PAYING.value, f"order payment_status is {order[1]}"
    assert int(order[2]) == 0, "the order recorded money that never arrived"

    # The event id is free again - the guard refused *this* delivery, it did not burn the
    # event, so a provider that corrects its status can still redeliver.
    claimed = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM payment_callbacks WHERE provider_event_id = :event_id",
        {"event_id": f"evt-fg11-closed-{gate.marker}"},
    )
    assert int(claimed[0]) == 0, "the refused event id was claimed"


def test_a_closed_payment_is_still_settleable_after_a_new_attempt(gate: Gate, engine) -> None:
    """``FAILED``/``CLOSED`` is not terminal for the *order* - the recovery path works.

    Design section 4.1 is explicit that a dead attempt "is not terminal for the order:
    the order stays payable and a new attempt is the correct recovery". A guard that
    refused the abandoned attempt *and* left the order permanently unpayable would be
    half a rule, so the recovery is asserted rather than assumed.
    """
    settings = get_settings()
    factory = get_session_factory()

    with factory() as session:
        abandoned = session.get(Payment, gate.payment_id)
        abandoned.status = PaymentRecordStatus.FAILED.value
        # Free the two unique keys so the replacement attempt can claim its own.
        abandoned.idempotency_key = f"fg11-failed-{gate.marker}"
        abandoned.client_request_id = f"fg11-failed-client-{gate.marker}"
        session.commit()

    with factory() as session:
        result = PaymentService(session, settings).create(
            principal=gate.consumer,
            order_no=gate.order_no,
            channel=PaymentChannel.MOCK.value,
            client_request_id=f"fg11-recovery-client-{gate.marker}",
            idempotency_key=f"fg11-recovery-key-{gate.marker}",
        )

    assert result.payment.status == PaymentRecordStatus.PAYING.value
    assert result.payment.payment_no != gate.payment_no, "the retry reused the dead attempt"
    assert result.payment.amount == gate.payable_amount

    attempts = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM payments WHERE order_id = :order_id",
        {"order_id": gate.order_id},
    )
    assert int(attempts[0]) == 2, (
        f"{attempts[0]} attempts for one order; a dead attempt must be replaceable"
    )

    fresh = _fresh_row(
        engine,
        "SELECT status FROM payments WHERE order_id = :order_id "
        "AND status = 'PAYING' AND payment_no <> :old",
        {"order_id": gate.order_id, "old": gate.payment_no},
    )
    assert fresh[0] == PaymentRecordStatus.PAYING.value

    # Nothing was settled by merely creating the replacement.
    order = _fresh_row(
        engine,
        "SELECT order_status, paid_amount FROM orders WHERE id = :order_id",
        {"order_id": gate.order_id},
    )
    assert order[0] == OrderStatus.PENDING_PAYMENT.value
    assert int(order[1]) == 0


def test_an_unsigned_delivery_is_refused_and_settles_nothing(gate: Gate, engine) -> None:
    """REQ-PAY-005: the signature is the authentication model, so a bad one settles nothing.

    Driven at the workflow level (the same entry point the FG-11 gate uses), because the
    property under test is "the transaction body refuses an unverified callback
    regardless of which layer calls it" - an HTTP-only check would leave the in-process
    mock-pay path and any future worker unguarded.
    """
    forged = CallbackRequest(
        provider=PaymentChannel.MOCK.value,
        event_id=f"evt-fg11-forged-{gate.marker}",
        event_type=CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED,
        timestamp=str(int(time.time())),
        signature="0" * 64,
        raw_body=json.dumps(
            _payload(
                payment_no=gate.payment_no,
                order_no=gate.order_no,
                amount=gate.payable_amount,
                transaction_no="FORGED",
            ),
            sort_keys=True,
            separators=(",", ":"),
        ).encode(),
        payload=_payload(
            payment_no=gate.payment_no,
            order_no=gate.order_no,
            amount=gate.payable_amount,
            transaction_no="FORGED",
        ),
    )

    factory = get_session_factory()
    with factory() as session:
        outcome = PaymentSuccessWorkflow(session).execute(forged)

    assert not outcome.applied
    assert outcome.error_code == "PAYMENT_CALLBACK_INVALID_SIGNATURE"

    # Nothing was settled, and - because the refusal rolls the claim back - nothing
    # claims the event id either, so a correctly signed redelivery still works.
    row = _fresh_row(
        engine,
        "SELECT status, paid_amount FROM payments WHERE payment_no = :payment_no",
        {"payment_no": gate.payment_no},
    )
    assert row[0] != PaymentRecordStatus.SUCCESS.value, (
        "a forged callback settled the payment"
    )
    assert int(row[1]) == 0

    claimed = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM payment_callbacks WHERE provider_event_id = :event_id",
        {"event_id": f"evt-fg11-forged-{gate.marker}"},
    )
    assert int(claimed[0]) == 0, (
        "the refused event id was claimed anyway, so a corrected redelivery would be "
        "answered as a duplicate"
    )


def test_the_workflow_commits_the_transaction_exactly_once(gate: Gate, engine) -> None:
    """One settlement means one **database** commit, not a fan-out of them.

    ## Why the instrumentation listens to the DBAPI connection

    The obvious implementation - counting SQLAlchemy's ``after_commit`` events - was
    written first and observed **two** events for one settlement. That is not a defect
    here: the callback claim is wrapped in ``begin_nested()`` (a SAVEPOINT, which is what
    confines a duplicate-key failure to a rollback that leaves the session usable), and
    SQLAlchemy fires ``after_commit`` for a savepoint release as well as for a real
    commit. A test built on that event cannot tell "one transaction" from "one
    transaction plus one savepoint", so it would have to be relaxed until it proved
    nothing.

    ``ConnectionEvents.commit`` on the engine fires **only** when the DBAPI connection's
    ``commit()`` runs - never for a SAVEPOINT ``RELEASE`` - so it measures exactly the
    boundary this phase's one-transaction rule is about.

    What the counts mean:

    * ``commits == 1`` - the claim and the business effect become visible atomically. A
      workflow that committed the claim separately would show two, and that is the
      "callback row without its effect" defect the module docstring names: a settlement
      the database believes happened, that no customer received, and that a provider
      retry can never repair because the event id is already claimed.
    * ``rollbacks == 0`` - nothing had to be undone. A workflow that rolled back and
      retried inside one call would be making its decisions twice.
    * ``savepoints >= 1`` - the duplicate-key confinement is genuinely in use
      (REQ-PAY-002), rather than the claim being pre-checked with a ``SELECT``.
    """
    settings = get_settings()
    secret = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()
    request = _signed_request(
        event_id=f"evt-fg11-once-{gate.marker}",
        payment_no=gate.payment_no,
        order_no=gate.order_no,
        amount=gate.payable_amount,
        transaction_no=f"MOCK-ONCE-{gate.marker}",
        secret=secret,
    )

    counts = {"commits": 0, "rollbacks": 0, "savepoints": 0}

    def _on_commit(_connection) -> None:
        counts["commits"] += 1

    def _on_rollback(_connection) -> None:
        counts["rollbacks"] += 1

    event.listen(engine, "commit", _on_commit)
    event.listen(engine, "rollback", _on_rollback)

    session = get_session_factory()()
    real_begin_nested = session.begin_nested

    def counting_begin_nested(*args, **kwargs):
        counts["savepoints"] += 1
        return real_begin_nested(*args, **kwargs)

    session.begin_nested = counting_begin_nested  # type: ignore[method-assign]

    try:
        outcome = PaymentSuccessWorkflow(session).execute(request)
    finally:
        session.close()
        event.remove(engine, "commit", _on_commit)
        event.remove(engine, "rollback", _on_rollback)

    assert outcome.applied, "the measured delivery did not settle the payment"
    assert counts["commits"] == 1, (
        f"the settlement committed {counts['commits']} times, expected exactly 1"
    )
    assert counts["rollbacks"] == 0, (
        f"the settlement rolled back {counts['rollbacks']} times inside one call"
    )
    assert counts["savepoints"] >= 1, (
        "no SAVEPOINT was used for the callback claim, so a duplicate-key failure would "
        "poison the session instead of being confined"
    )


def test_no_deduction_happens_when_the_amount_does_not_match(gate: Gate, engine) -> None:
    """The amount guard is a guard, not a log line: a mismatch must settle nothing.

    A callback reporting an amount that differs from the payment row is the shape of a
    partial payment, a currency confusion or an attack. The payment must stay payable
    and no stock may move - so the deduction assert is the one that matters, because
    "the status was not updated" would still be true of a path that deducted first.
    """
    settings = get_settings()
    secret = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()
    request = _signed_request(
        event_id=f"evt-fg11-amount-{gate.marker}",
        payment_no=gate.payment_no,
        order_no=gate.order_no,
        amount=gate.payable_amount - 1,
        transaction_no=f"MOCK-SHORT-{gate.marker}",
        secret=secret,
    )

    factory = get_session_factory()
    with factory() as session:
        outcome = PaymentSuccessWorkflow(session).execute(request)

    assert not outcome.applied
    assert outcome.error_code == ErrorCode.PAYMENT_AMOUNT_MISMATCH.name

    row = _fresh_row(
        engine,
        "SELECT status, paid_amount FROM payments WHERE payment_no = :payment_no",
        {"payment_no": gate.payment_no},
    )
    assert row[0] == PaymentRecordStatus.PAYING.value, (
        f"a short payment moved the record to {row[0]}"
    )
    assert int(row[1]) == 0

    movements = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM inventory_movements "
        "WHERE reference_type = 'ORDER_ITEM' AND reference_id IN (:id0, :id1)",
        {"id0": gate.order_item_ids[0], "id1": gate.order_item_ids[1]},
    )
    assert int(movements[0]) == 0, "stock was deducted for a payment that did not match"

    # The refusal is recorded for triage, and the event id is free again - a corrected
    # redelivery of the same event must not be answered as a duplicate.
    recorded = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM payment_callbacks WHERE provider_event_id = :event_id",
        {"event_id": f"evt-fg11-amount-{gate.marker}"},
    )
    assert int(recorded[0]) == 0, "the refused event id was claimed"

    # And the payment is still settleable: the guard refused *this* delivery, it did not
    # put the attempt beyond recovery.
    corrected = _signed_request(
        event_id=f"evt-fg11-amount-fixed-{gate.marker}",
        payment_no=gate.payment_no,
        order_no=gate.order_no,
        amount=gate.payable_amount,
        transaction_no=f"MOCK-FIXED-{gate.marker}",
        secret=secret,
    )
    with factory() as session:
        recovered = PaymentSuccessWorkflow(session).execute(corrected)
    assert recovered.applied, "a corrected redelivery was refused as well"

    settled = _fresh_row(
        engine,
        "SELECT status, paid_amount FROM payments WHERE payment_no = :payment_no",
        {"payment_no": gate.payment_no},
    )
    assert settled[0] == PaymentRecordStatus.SUCCESS.value
    assert int(settled[1]) == gate.payable_amount


def test_the_service_marks_the_order_paying_without_marking_it_paid(gate: Gate, engine) -> None:
    """Design 4.1: creating an attempt is not a payment.

    ``UNPAID -> PAYING`` on the order, and the payment record itself in ``PAYING``. It
    must **not** reach ``PAID``: no JWT-reachable path can do that (REQ-PAY-005/006,
    INV-008), and only a verified callback can.

    The seed starts the order at ``PAYING`` (because the fixture represents an attempt
    already in flight), so this test drives a second order through the real
    ``PaymentService.create`` to observe the transition itself rather than asserting a
    state the fixture set.
    """
    settings = get_settings()
    factory = get_session_factory()

    # Re-open the order's axis to UNPAID so the transition is observable.
    with factory() as session:
        order = session.get(Order, gate.order_id)
        order.payment_status = PaymentStatus.UNPAID.value
        order.client_request_id = f"fg11-paying-{gate.marker}"
        order.order_no = f"NVFG11P{gate.marker}"
        session.commit()
        new_order_no = order.order_no

    # The payment row's unique keys must not collide with the fixture's attempt.
    with factory() as session:
        existing = session.execute(
            select(Payment).where(Payment.order_id == gate.order_id)
        ).scalars().first()
        existing.status = PaymentRecordStatus.CLOSED.value
        existing.idempotency_key = f"fg11-closed-{gate.marker}"
        existing.client_request_id = f"fg11-closed-client-{gate.marker}"
        session.commit()

    with factory() as session:
        result = PaymentService(session, settings).create(
            principal=gate.consumer,
            order_no=new_order_no,
            channel=PaymentChannel.MOCK.value,
            client_request_id=f"fg11-new-client-{gate.marker}",
            idempotency_key=f"fg11-new-key-{gate.marker}",
        )

    assert result.payment.status == PaymentRecordStatus.PAYING.value
    assert result.payment.amount == gate.payable_amount, (
        "the amount must come from orders.payable_amount, never from the request"
    )
    assert result.replayed is False

    state = _fresh_row(
        engine,
        "SELECT order_status, payment_status, paid_amount FROM orders WHERE id = :order_id",
        {"order_id": gate.order_id},
    )
    assert state[0] == OrderStatus.PENDING_PAYMENT.value, "creating an attempt moved order_status"
    assert state[1] == PaymentStatus.PAYING.value, f"order payment_status is {state[1]}"
    assert int(state[2]) == 0, "creating an attempt must not write paid_amount"

    # A create must not have settled anything: no deduction, no log, no shell.
    movements = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM inventory_movements "
        "WHERE reference_type = 'ORDER_ITEM' AND reference_id IN (:id0, :id1)",
        {"id0": gate.order_item_ids[0], "id1": gate.order_item_ids[1]},
    )
    assert int(movements[0]) == 0
    logs = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM order_status_logs WHERE order_id = :order_id",
        {"order_id": gate.order_id},
    )
    assert int(logs[0]) == 0, "creating an attempt appended a status log"
    shells = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM fulfillments WHERE order_id = :order_id",
        {"order_id": gate.order_id},
    )
    assert int(shells[0]) == 0

    # The replay path: the same Idempotency-Key returns the same attempt.
    with factory() as session:
        replayed = PaymentService(session, settings).create(
            principal=gate.consumer,
            order_no=new_order_no,
            channel=PaymentChannel.MOCK.value,
            client_request_id=f"fg11-new-client-{gate.marker}",
            idempotency_key=f"fg11-new-key-{gate.marker}",
        )
    assert replayed.payment.payment_no == result.payment.payment_no


def test_the_claim_is_attempted_not_pre_checked(gate: Gate, engine) -> None:
    """REQ-PAY-002: the second delivery of an event is answered without a second effect.

    A deliberate *sequential* duplicate (the easy case), asserted separately from the
    concurrent gate so that a failure here localises the defect to the claim logic
    rather than to the locking. The concurrent version is above; this one runs first in
    a developer's mental model and is the one that should be read first when something
    breaks.
    """
    settings = get_settings()
    secret = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()
    request = _signed_request(
        event_id=f"evt-fg11-seq-{gate.marker}",
        payment_no=gate.payment_no,
        order_no=gate.order_no,
        amount=gate.payable_amount,
        transaction_no=f"MOCK-SEQ-{gate.marker}",
        secret=secret,
    )
    factory = get_session_factory()

    with factory() as session:
        first = PaymentSuccessWorkflow(session).execute(request)
    assert first.applied

    with factory() as session:
        second = PaymentSuccessWorkflow(session).execute(request)

    assert not second.applied
    assert second.duplicate
    assert second.error_code == ErrorCode.PAYMENT_CALLBACK_DUPLICATE.name

    processed = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM payment_callbacks WHERE process_status = 'PROCESSED' "
        "AND provider_event_id = :event_id",
        {"event_id": f"evt-fg11-seq-{gate.marker}"},
    )
    assert int(processed[0]) == 1

    total_callbacks = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM payment_callbacks WHERE provider_event_id = :event_id",
        {"event_id": f"evt-fg11-seq-{gate.marker}"},
    )
    assert int(total_callbacks[0]) == 1, "the duplicate inserted a second callback row"

    movements = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM inventory_movements "
        "WHERE reference_type = 'ORDER_ITEM' AND reference_id IN (:id0, :id1)",
        {"id0": gate.order_item_ids[0], "id1": gate.order_item_ids[1]},
    )
    assert int(movements[0]) == len(gate.order_item_ids), "the duplicate deducted again"

    logs = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM order_status_logs WHERE order_id = :order_id",
        {"order_id": gate.order_id},
    )
    assert int(logs[0]) == 1, "the duplicate appended a status log"


def test_the_callback_process_status_vocabulary_is_the_one_the_table_accepts() -> None:
    """A cheap structural check that a claim outcome is representable.

    ``payment_callbacks.process_status`` carries a hand-written ``CHECK`` derived
    from the Python enum, so the two must agree. Pinning the membership here means a
    vocabulary change the migration did not follow fails as a named test rather than as
    an integrity error in the middle of a settlement.
    """
    assert {member.value for member in CallbackProcessStatus} == {
        "RECEIVED",
        "PROCESSED",
        "FAILED",
        "IGNORED",
    }


def test_the_gate_fixture_is_isolated(gate: Gate, engine) -> None:
    """The fixture must not leak rows between runs or between tests.

    A concurrency suite whose second run behaves differently from its first is a suite
    nobody trusts; asserting the seeded order is the only one for the marker's merchant
    makes a leaked row from an earlier run fail here rather than distort the counts in
    the gate above.
    """
    orders = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM orders WHERE merchant_id = :merchant_id",
        {"merchant_id": gate.merchant_id},
    )
    assert int(orders[0]) == 1

    payments = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM payments WHERE order_id = :order_id",
        {"order_id": gate.order_id},
    )
    assert int(payments[0]) == 1

    items = _fresh_row(
        engine,
        "SELECT COUNT(*) FROM order_items WHERE order_id = :order_id",
        {"order_id": gate.order_id},
    )
    assert int(items[0]) == len(gate.order_item_ids)
