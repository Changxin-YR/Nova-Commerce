"""Fixtures for the fulfillment integration tests - real MySQL, real transactions.

## Why this module has its own seed rather than importing the shared one

``PHASE5_DESIGN`` section 12 says every Phase 5 test should import the shared seed
from ``backend/tests/integration/commerce/seed.py`` (owned by data-layer) instead of
re-deriving a merchant/SKU/warehouse four times. That helper had not landed when
these tests were written, so this module carries a **minimal** seed covering exactly
what the ship path needs: one merchant, one SKU, one warehouse with stock, one buyer,
one operator, and an order whose fulfillment is under test.

It is deliberately smaller than the shared seed - no pricing, no cart, no second and
third SKU, because the fulfillment rules are about *quantities and packages*, not
about money. When the shared seed lands, the honest move is to switch these tests to
it and delete this; until then, duplicating the *fixture* is far cheaper than
coupling my tests to a file whose owner is still writing it, which is the situation
section 12's rule exists to avoid in the first place.

## Why the fixtures commit instead of rolling back

``FulfillmentService`` joins the caller's transaction, but the **ship** path takes a
row lock and the tests assert on what a *later* session can read - a package's status
and the order's axis after the fact. A rolled-back outer transaction would be
invisible to both, exactly as ``tests/integration/order/conftest.py`` documents for
``CreateOrderWorkflow``. So each object is committed under a unique marker and the
teardown deletes it in dependency order.

The teardown deletes in FK order deliberately: ``FulfillmentItem`` before
``Fulfillment`` (RESTRICT), ``Fulfillment`` before ``OrderItem`` and before ``Order``,
``OrderItem`` before ``Order``, and the ``Order`` before its ``Merchant``/``User``.
Getting that order wrong makes the fixture fail on cleanup rather than on the thing
under test, which is the most confusing possible failure mode.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.modules.catalog.models import Product, ProductSku
from app.modules.fulfillment.models import Fulfillment, FulfillmentItem
from app.modules.identity.enums import DataScope, PermissionCode, UserType
from app.modules.identity.models import Merchant, User
from app.modules.identity.service import Principal
from app.modules.inventory.enums import MovementType, OperatorType, ReferenceType
from app.modules.inventory.models import Inventory, InventoryMovement, Warehouse
from app.modules.order.enums import FulfillmentStatus, OrderStatus, PaymentStatus
from app.modules.order.models import Order, OrderItem
from app.shared.db.base import utc_now
from app.shared.db.models.outbox import OutboxMessage
from app.shared.db.session import configure_database, get_session_factory

#: Opening stock, large enough that a test never accidentally exhausts it.
OPENING_STOCK = 1000


@dataclass(frozen=True, slots=True)
class Commerce:
    """Everything the ship path needs, with the ids the assertions check against."""

    marker: str
    merchant_id: int
    warehouse_id: int
    product_id: int
    sku_id: int
    buyer_id: int
    staff_id: int
    order_id: int
    order_no: str
    order_item_id: int

    @property
    def staff(self) -> Principal:
        """An operator who may ship, resolved server-side (never from input)."""
        return Principal(
            user_id=self.staff_id,
            user_type=UserType.STAFF.value,
            merchant_id=self.merchant_id,
            roles=("FULFILLMENT_OPERATOR",),
            permissions=frozenset(
                {
                    PermissionCode.FULFILLMENT_SHIP.value,
                    PermissionCode.FULFILLMENT_READ.value,
                }
            ),
            data_scope=DataScope.MERCHANT,
            session_id=f"test-staff-{self.marker}",
            is_staff=True,
        )

    @property
    def consumer(self) -> Principal:
        """The buyer - and, crucially, *not* able to ship."""
        return Principal(
            user_id=self.buyer_id,
            user_type=UserType.CONSUMER.value,
            merchant_id=None,
            roles=(),
            permissions=frozenset(),
            data_scope=DataScope.SELF,
            session_id=f"test-buyer-{self.marker}",
            is_staff=False,
        )

    @property
    def other_merchant_staff(self) -> Principal:
        """Staff of a different tenant, same permissions.

        Used to prove the merchant scope is applied **in the query** rather than
        checked after load: the row must never be fetched, so the answer is
        ``FULFILLMENT_NOT_FOUND`` (70 000) and not a permission error that would
        confirm the package exists.
        """
        return Principal(
            user_id=self.staff_id,
            user_type=UserType.STAFF.value,
            merchant_id=self.merchant_id + 10_000_000,
            roles=("FULFILLMENT_OPERATOR",),
            permissions=frozenset(
                {
                    PermissionCode.FULFILLMENT_SHIP.value,
                    PermissionCode.FULFILLMENT_READ.value,
                }
            ),
            data_scope=DataScope.MERCHANT,
            session_id=f"test-other-{self.marker}",
            is_staff=True,
        )

    def key(self, suffix: str) -> str:
        return f"{self.marker}-{suffix}"[:128]


@pytest.fixture(scope="session")
def engine():
    return configure_database()


@pytest.fixture
def session(engine) -> Iterator[Session]:
    """A committed-work session for the test body and for reading service results."""
    factory = get_session_factory()
    with factory() as s:
        yield s


@pytest.fixture
def commerce(engine, request: pytest.FixtureRequest) -> Iterator[Commerce]:
    """Merchant / warehouse / SKU with stock / buyer / operator, and one PAID order.

    The order is created directly at ``PROCESSING`` + ``PAID`` rather than driven
    through ``PaymentSuccessWorkflow``: that workflow is payment-workflow's, its
    step 8 already calls ``create_shell`` (which is what makes this module's shell
    come from the real path), and coupling every fulfillment test to the payment
    module would make a fulfillment failure look like a payment failure.

    The order line's quantity is **3** so a single order can legitimately produce:
    one shipped package of 2 plus a residual of 1 (the multi-package case), and then
    prove that a further unit would exceed the line (the cumulative cap).
    """
    marker = uuid.uuid4().hex[:8]
    # The holder is populated as the graph is built and registered with a finalizer
    # *before* the first committed write. The teardown after ``yield`` cannot run when
    # setup fails part-way - pytest only runs the post-yield block if the fixture
    # produced a value - so a failure mid-setup used to strand committed rows in the
    # shared schema. That is not hypothetical: residue from exactly this pattern made
    # the suite non-repeatable and produced failures that looked like code defects.
    # ``request.addfinalizer`` runs on the way out either way.
    created: dict[str, object] = {}
    request.addfinalizer(lambda: _purge(factory, created))
    factory = get_session_factory()
    order_quantity = 3
    unit_price = 1999

    with factory() as s:
        merchant = Merchant(code=f"M{marker}"[:24], name=f"Ful Test {marker}")
        s.add(merchant)
        s.flush()

        warehouse = Warehouse(
            merchant_id=merchant.id,
            code="MAIN",
            name="Main Warehouse",
            is_default=True,
            status="ACTIVE",
        )
        s.add(warehouse)
        s.flush()

        product = Product(
            merchant_id=merchant.id,
            product_no=f"P-{marker}",
            slug=f"p-{marker}",
            name="Nova Phone 15 Pro",
            status="PUBLISHED",
            published_at=utc_now(),
        )
        s.add(product)
        s.flush()

        sku = ProductSku(
            merchant_id=merchant.id,
            product_id=product.id,
            sku_no=f"S-{marker}",
            sku_code=f"sku-{marker}",
            name="Original Titanium 256GB",
            price_amount=unit_price,
            cost_amount=999,
            status="ACTIVE",
        )
        s.add(sku)
        s.flush()

        inventory = Inventory(
            merchant_id=merchant.id,
            warehouse_id=warehouse.id,
            sku_id=sku.id,
            available_qty=OPENING_STOCK,
            locked_qty=0,
            safety_stock=0,
        )
        s.add(inventory)
        s.flush()
        # The opening balance AND the movement explaining it (INV-007): seeding the
        # balance alone leaves the row unexplainable by its own ledger.
        s.add(
            InventoryMovement.build(
                warehouse_id=warehouse.id,
                sku_id=sku.id,
                movement_type=MovementType.PURCHASE_IN,
                before_available=0,
                after_available=OPENING_STOCK,
                before_locked=0,
                after_locked=0,
                idempotency_key=f"ful-seed-{marker}",
                reference_type=ReferenceType.PURCHASE_ORDER,
                operator_type=OperatorType.SYSTEM,
                reason="Phase 5 fulfillment fixture: opening stock",
            )
        )

        buyer = User(
            username=f"buyer_{marker}",
            email=f"buyer_{marker}@example.test",
            password_hash=hash_password_stub(),
            display_name="Fulfillment Test Buyer",
            user_type=UserType.CONSUMER.value,
            status="ACTIVE",
            merchant_id=None,
        )
        operator = User(
            username=f"staff_{marker}",
            email=f"staff_{marker}@example.test",
            password_hash=hash_password_stub(),
            display_name="Fulfillment Test Operator",
            user_type=UserType.STAFF.value,
            status="ACTIVE",
            merchant_id=merchant.id,
        )
        s.add(buyer)
        s.add(operator)
        s.flush()

        now = utc_now()
        line_total = unit_price * order_quantity
        order = Order(
            merchant_id=merchant.id,
            user_id=buyer.id,
            order_no=uuid.uuid4().hex,  # stamped below, once the id exists
            client_request_id=f"cr-{marker}",
            request_hash="0" * 64,
            order_status=OrderStatus.PROCESSING.value,
            payment_status=PaymentStatus.PAID.value,
            fulfillment_status=FulfillmentStatus.UNFULFILLED.value,
            after_sale_status="NONE",
            original_amount=line_total,
            promotion_discount_amount=0,
            coupon_discount_amount=0,
            shipping_amount=0,
            payable_amount=line_total,
            paid_amount=line_total,
            refunded_amount=0,
            receiver_name="Test Receiver",
            receiver_phone="13800000000",
            address_snapshot={"province": "Test", "city": "Test", "detail": "Road 1"},
            item_count=1,
            first_item_name="Nova Phone 15 Pro",
            created_at=now,
            updated_at=now,
        )
        s.add(order)
        s.flush()
        order.order_no = f"NV{now:%Y%m%d}{order.id:06d}"

        item = OrderItem(
            order_id=order.id,
            warehouse_id=warehouse.id,
            product_id=product.id,
            sku_id=sku.id,
            product_name="Nova Phone 15 Pro",
            sku_name="Original Titanium 256GB",
            unit_price=unit_price,
            quantity=order_quantity,
            original_amount=line_total,
            promotion_discount_amount=0,
            coupon_discount_amount=0,
            allocated_discount_amount=0,
            payable_amount=line_total,
            refunded_amount=0,
            after_sale_status="NONE",
            created_at=now,
            updated_at=now,
        )
        s.add(item)
        s.flush()
        s.commit()

        created.update(
            marker=marker,
            merchant_id=merchant.id,
            warehouse_id=warehouse.id,
            product_id=product.id,
            sku_id=sku.id,
            buyer_id=buyer.id,
            staff_id=operator.id,
            order_id=order.id,
            order_no=order.order_no,
            order_item_id=item.id,
        )

    # ``Commerce`` is frozen+slots, so build it from the holder rather than assigning
    # onto it - the holder is what the finalizer reads, and it may hold a partial
    # graph if setup failed.
    yield Commerce(**created)  # type: ignore[arg-type]

    # Normal path: the finalizer registered above does the work, so there is exactly
    # one teardown implementation. Duplicating it here is what data-layer correctly
    # refused to do - two cleanup paths drift, and the one that runs less often is the
    # one that rots.


def hash_password_stub() -> str:
    """A fixed, valid-looking hash.

    These fixtures never authenticate over HTTP - principals are built server-side -
    so paying Argon2's cost per fixture would add seconds to the suite for nothing.
    The column only requires a non-null string.
    """
    return "$argon2id$v=19$m=65536,t=3,p=4$" + "0" * 43


def order_of(session: Session, commerce: Commerce) -> Order:
    """The test's order, re-read from the database (never from a stale instance)."""
    return session.get(Order, commerce.order_id)  # type: ignore[return-value]


def packages_of(session: Session, commerce: Commerce) -> list[Fulfillment]:
    """Every package of the test's order, oldest first - with its lines loaded."""
    stmt = select(Fulfillment).where(Fulfillment.order_id == commerce.order_id).order_by(Fulfillment.id.asc())
    return list(session.scalars(stmt))


def _purge(factory, created: dict[str, object]) -> None:
    """Delete everything the fixture created, in FK order, tolerating a partial graph.

    Called from a finalizer, so it must cope with setup having failed at any point: a
    key that was never populated is simply skipped, which is why each deletion is
    guarded rather than assuming the whole graph exists.

    Scoped by the ids the fixture recorded rather than by merchant, so it cannot touch
    another test's rows even if two fixtures of this kind ever shared a merchant.

    Order matters and is not guesswork: these FKs are ``RESTRICT`` precisely so history
    cannot be deleted out from under a live row, so the unwinding goes children-first.
    """
    merchant_id = created.get("merchant_id")
    if merchant_id is None:
        # Nothing was written yet - the failure happened before the first flush.
        return

    with factory() as session:
        order_ids = [int(created["order_id"])] if created.get("order_id") else []
        warehouse_id = created.get("warehouse_id")
        sku_id = created.get("sku_id")
        product_id = created.get("product_id")
        buyer_id = created.get("buyer_id")
        staff_id = created.get("staff_id")

        if order_ids:
            session.execute(
                delete(FulfillmentItem).where(
                    FulfillmentItem.fulfillment_id.in_(
                        select(Fulfillment.id).where(Fulfillment.order_id.in_(order_ids))
                    )
                )
            )
            session.execute(delete(Fulfillment).where(Fulfillment.order_id.in_(order_ids)))
            session.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            session.execute(delete(Order).where(Order.id.in_(order_ids)))
        if warehouse_id is not None:
            session.execute(delete(InventoryMovement).where(InventoryMovement.warehouse_id == warehouse_id))
        if sku_id is not None:
            session.execute(delete(Inventory).where(Inventory.sku_id == sku_id))
            session.execute(delete(ProductSku).where(ProductSku.id == sku_id))
        if product_id is not None:
            session.execute(delete(Product).where(Product.id == product_id))
        if warehouse_id is not None:
            session.execute(delete(Warehouse).where(Warehouse.id == warehouse_id))
        user_ids = [uid for uid in (buyer_id, staff_id) if uid is not None]
        if user_ids:
            session.execute(delete(User).where(User.id.in_(user_ids)))
        # Phase 6: an order created here also appends an `outbox_messages` row, whose
        # RESTRICT FK onto merchants must be unwound first.
        session.execute(
            delete(OutboxMessage).where(OutboxMessage.merchant_id == int(merchant_id))
        )
        session.execute(delete(Merchant).where(Merchant.id == int(merchant_id)))
        session.commit()
