"""Fixtures for the Phase 5 payment integration tests (the HTTP surface).

Everything here is the same shape as ``tests/integration/order/conftest.py`` - committed
seed, dependency-ordered teardown - for the same reasons, and the duplication is
deliberate: a test package that imported another package's ``conftest`` would couple the
payment suite to a file this module does not own, and the phase's file-ownership rule
exists precisely so that one writer cannot break another's fixtures by accident.

## Why the order is seeded directly rather than created through the API

The payment surface's own contract is what is under test here, and creating an order
through ``POST /orders`` would drag Phase 4's pricing allocation into every payment
failure. The seed writes the rows the payment path reads, with ``CHECK``-consistent
arithmetic (no discounts, free shipping, so ``payable == original``), so a failure in this
file is a payment failure.

## The token is minted, not logged in

There is no ``/auth/login`` route in this phase (``app/modules/identity/api`` holds only an
``__init__``), so the access token comes from ``AuthService.login`` - the same route the
Phase 4 HTTP tests take, and the reason ``get_current_principal`` re-resolves the principal
from the database on every request is that a minted token is otherwise indistinguishable
from a forged one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, text

from app.core.config import get_settings
from app.modules.catalog.models import Product, ProductSku
from app.modules.identity.enums import DataScope, PermissionCode, UserType
from app.modules.identity.models import Merchant, Permission, Role, User, UserAddress
from app.modules.identity.repository import RoleRepository
from app.modules.identity.security import hash_password
from app.modules.identity.service import AuthService, Principal
from app.modules.inventory.enums import MovementType, OperatorType, ReferenceType
from app.modules.inventory.models import Inventory, InventoryMovement, Warehouse
from app.modules.order.enums import OrderStatus, PaymentStatus
from app.modules.order.models import Order, OrderItem, OrderStatusLog
from app.modules.payment.enums import PaymentChannel, PaymentRecordStatus
from app.modules.payment.models import Payment
from app.shared.db.base import utc_now
from app.shared.db.models.outbox import OutboxMessage
from app.shared.db.session import configure_database, get_session_factory

#: The dev password for every fixture account, argon2-hashed like a real one.
PASSWORD = "Correct-Horse-Battery-9"

#: Opening stock and the quantities this order holds reserved.
OPENING_STOCK = 500
LINE_QUANTITIES = (1, 2)
UNIT_PRICES = (1999, 2999)

#: Per-line payable, so the assertions can be exact rather than recomputed.
LINE_PAYABLES = tuple(
    price * quantity for price, quantity in zip(UNIT_PRICES, LINE_QUANTITIES, strict=True)
)
PAYABLE_AMOUNT = sum(LINE_PAYABLES)


@dataclass(frozen=True, slots=True)
class Shop:
    """A committed commerce fixture with the ids the payment tests assert against."""

    marker: str
    merchant_id: int
    product_id: int
    sku_ids: tuple[int, int]
    warehouse_id: int
    consumer_id: int
    consumer_username: str
    staff_id: int
    staff_username: str
    address_id: int
    order_id: int
    order_no: str
    payment_id: int
    payment_no: str
    order_item_ids: tuple[int, int]
    extra: dict = field(default_factory=dict)

    @property
    def consumer(self) -> Principal:
        """The buyer as the service layer resolves them - server-side, never from input."""
        return Principal(
            user_id=self.consumer_id,
            user_type=UserType.CONSUMER.value,
            merchant_id=None,
            roles=(),
            permissions=frozenset(),
            data_scope=DataScope.SELF,
            session_id=f"pay-it-{self.marker}",
            is_staff=False,
        )

    @property
    def staff(self) -> Principal:
        """A merchant-scoped console operator holding ``payment:read``."""
        return Principal(
            user_id=self.staff_id,
            user_type=UserType.STAFF.value,
            merchant_id=self.merchant_id,
            roles=("PAYMENT_OPERATOR",),
            permissions=frozenset({PermissionCode.PAYMENT_READ.value}),
            data_scope=DataScope.MERCHANT,
            session_id=f"pay-it-staff-{self.marker}",
            is_staff=True,
        )


@pytest.fixture(scope="module")
def engine():
    return configure_database()


@pytest.fixture(scope="module")
def client():
    """A real ASGI client.

    ``TestClient`` rather than ``httpx.ASGITransport``: the shared fixture in
    ``tests/conftest.py`` builds a synchronous ``httpx.Client`` around an async-only
    transport and cannot work with httpx 0.28. ``TestClient`` drives the app through its
    own portal and needs no async plumbing of ours.
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app(get_settings())) as http_client:
        yield http_client


def _seed(marker: str) -> Shop:
    factory = get_session_factory()
    now = utc_now()

    with factory() as session:
        merchant = Merchant(code=f"PIT{marker}"[:24], name=f"Payment IT {marker}")
        session.add(merchant)
        session.flush()

        product = Product(
            merchant_id=merchant.id,
            product_no=f"P-PIT-{marker}",
            slug=f"p-pit-{marker}",
            name="Payment IT Product",
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
                sku_no=f"S-PIT-{marker}-{index}",
                sku_code=f"sku-pit-{marker}-{index}",
                name=f"Payment IT SKU {index}",
                price_amount=price,
                cost_amount=price // 2,
                status="ACTIVE",
            )
            session.add(sku)
            session.flush()
            sku_ids.append(sku.id)

        warehouse = Warehouse(
            merchant_id=merchant.id,
            code=f"WHP{marker}"[:24],
            name="Payment IT Warehouse",
            is_default=True,
            status="ACTIVE",
        )
        session.add(warehouse)
        session.flush()

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
            session.add(
                InventoryMovement.build(
                    warehouse_id=warehouse.id,
                    sku_id=sku_id,
                    movement_type=MovementType.ORDER_LOCK,
                    before_available=OPENING_STOCK,
                    after_available=OPENING_STOCK,
                    before_locked=0,
                    after_locked=quantity,
                    idempotency_key=f"pit-seed-lock-{marker}-{sku_id}",
                    reference_type=ReferenceType.ORDER,
                    operator_type=OperatorType.SYSTEM,
                    reason="payment integration fixture: reserved by the order",
                )
            )

        consumer = User(
            username=f"pit_buyer_{marker}",
            email=f"pit_buyer_{marker}@example.test",
            password_hash=hash_password(PASSWORD),
            display_name="Payment IT Buyer",
            user_type=UserType.CONSUMER.value,
            status="ACTIVE",
            merchant_id=None,
        )
        session.add(consumer)
        session.flush()

        staff = User(
            username=f"pit_staff_{marker}",
            email=f"pit_staff_{marker}@example.test",
            password_hash=hash_password(PASSWORD),
            display_name="Payment IT Operator",
            user_type=UserType.STAFF.value,
            status="ACTIVE",
            merchant_id=merchant.id,
        )
        session.add(staff)
        session.flush()

        roles = RoleRepository(session)
        role = roles.add_role(
            Role(
                merchant_id=merchant.id,
                code="PAYMENT_OPERATOR",
                name="Payment operator",
                data_scope=DataScope.MERCHANT.value,
            )
        )
        permission = roles.get_permission_by_code(PermissionCode.PAYMENT_READ.value)
        if permission is None:
            permission = roles.add_permission(
                Permission(
                    code=PermissionCode.PAYMENT_READ.value,
                    resource="payment",
                    action="read",
                    description="Phase 5 payment fixtures",
                )
            )
        roles.grant_permission(role_id=role.id, permission_id=permission.id)
        roles.assign_role(user_id=staff.id, role_id=role.id)

        address = UserAddress(
            merchant_id=None,
            user_id=consumer.id,
            receiver_name="Payment IT Receiver",
            receiver_phone="13800002222",
            province="Guangdong",
            city="Shenzhen",
            district="Nanshan",
            detail="Payment IT Road 2",
            postal_code="518000",
            tag="HOME",
            is_default=True,
        )
        session.add(address)
        session.flush()

        order = Order(
            merchant_id=merchant.id,
            user_id=consumer.id,
            order_no=f"NVPIT{marker}",
            client_request_id=f"pit-{marker}",
            request_hash="e" * 64,
            order_status=OrderStatus.PENDING_PAYMENT.value,
            payment_status=PaymentStatus.PAYING.value,
            fulfillment_status="UNFULFILLED",
            after_sale_status="NONE",
            original_amount=PAYABLE_AMOUNT,
            promotion_discount_amount=0,
            coupon_discount_amount=0,
            shipping_amount=0,
            payable_amount=PAYABLE_AMOUNT,
            paid_amount=0,
            refunded_amount=0,
            address_id=address.id,
            receiver_name="Payment IT Receiver",
            receiver_phone="13800002222",
            address_snapshot={"full_address": "GuangdongShenzhenNanshanPayment IT Road 2"},
            item_count=sum(LINE_QUANTITIES),
            first_item_name="Payment IT Product Payment IT SKU 1",
            expires_at=now + timedelta(minutes=30),
            created_at=now,
            updated_at=now,
        )
        session.add(order)
        session.flush()

        item_ids: list[int] = []
        for index, (sku_id, quantity, price, payable) in enumerate(
            zip(sku_ids, LINE_QUANTITIES, UNIT_PRICES, LINE_PAYABLES, strict=True), start=1
        ):
            item = OrderItem(
                order_id=order.id,
                warehouse_id=warehouse.id,
                product_id=product.id,
                sku_id=sku_id,
                product_name="Payment IT Product",
                sku_name=f"Payment IT SKU {index}",
                unit_price=price,
                quantity=quantity,
                original_amount=price * quantity,
                promotion_discount_amount=0,
                coupon_discount_amount=0,
                allocated_discount_amount=0,
                payable_amount=payable,
                refunded_amount=0,
                after_sale_status="NONE",
                created_at=now,
                updated_at=now,
            )
            session.add(item)
            session.flush()
            item_ids.append(item.id)

        payment = Payment(
            payment_no=f"NVPAYPIT{marker}",
            order_id=order.id,
            order_no=order.order_no,
            merchant_id=merchant.id,
            user_id=consumer.id,
            channel=PaymentChannel.MOCK.value,
            amount=PAYABLE_AMOUNT,
            status=PaymentRecordStatus.PAYING.value,
            idempotency_key=f"pit-key-{marker}",
            client_request_id=f"pit-client-{marker}",
            request_hash="b" * 64,
            paid_amount=0,
            refunded_amount=0,
            expires_at=now + timedelta(minutes=30),
            created_at=now,
            updated_at=now,
        )
        session.add(payment)
        session.flush()

        shop = Shop(
            marker=marker,
            merchant_id=merchant.id,
            product_id=product.id,
            sku_ids=(sku_ids[0], sku_ids[1]),
            warehouse_id=warehouse.id,
            consumer_id=consumer.id,
            consumer_username=consumer.username,
            staff_id=staff.id,
            staff_username=staff.username,
            address_id=address.id,
            order_id=order.id,
            order_no=order.order_no,
            payment_id=payment.id,
            payment_no=payment.payment_no,
            order_item_ids=(item_ids[0], item_ids[1]),
        )
        session.commit()
        return shop


@pytest.fixture(scope="module")
def shop(engine) -> Iterator[Shop]:
    marker = uuid.uuid4().hex[:8].upper()
    seeded = _seed(marker)
    try:
        yield seeded
    finally:
        _purge(seeded)


def _purge(shop: Shop) -> None:
    """Delete everything the fixture and the tests created, children first.

    The foreign keys are ``RESTRICT`` so that financial history cannot be deleted out from
    under a live row, which makes the unwinding order mandatory rather than stylistic. The
    fulfillment tables are cleaned with raw SQL because the fulfillment module is not
    imported here: this fixture must not depend on another module's implementation to tidy
    up after itself.
    """
    factory = get_session_factory()
    with factory() as session:
        session.execute(
            delete(InventoryMovement).where(InventoryMovement.sku_id.in_(shop.sku_ids))
        )
        # Callbacks are deleted by **event-id pattern**, not only by merchant_id. A refused
        # delivery (forged signature, stale timestamp, unknown payment) resolves no payment,
        # so its `order_no` and `merchant_id` are both NULL - which means a merchant-scoped
        # delete silently misses exactly the rows an incident review would want removed.
        # Measured: 4 rows leaked per run. Every event id this suite builds embeds the
        # fixture marker (`http-<marker>-*`) and the mock-pay route embeds the payment_no, so
        # both patterns are exact, and `payment_callbacks` carries no FK to `payments`
        # (design 5.2) that would otherwise cascade.
        session.execute(
            text(
                "DELETE FROM payment_callbacks "
                "WHERE provider_event_id LIKE :evt_pattern "
                "OR provider_event_id LIKE :mock_pattern "
                "OR merchant_id = :merchant_id OR order_no = :order_no"
            ),
            {
                "evt_pattern": f"%{shop.marker}%",
                "mock_pattern": f"mock-{shop.payment_no}%",
                "merchant_id": shop.merchant_id,
                "order_no": shop.order_no,
            },
        )
        session.execute(delete(Payment).where(Payment.order_id == shop.order_id))
        session.execute(
            text(
                "DELETE fi FROM fulfillment_items fi "
                "JOIN fulfillments f ON f.id = fi.fulfillment_id WHERE f.order_id = :order_id"
            ),
            {"order_id": shop.order_id},
        )
        session.execute(
            text("DELETE FROM fulfillments WHERE order_id = :order_id"),
            {"order_id": shop.order_id},
        )
        session.execute(delete(OrderStatusLog).where(OrderStatusLog.order_id == shop.order_id))
        session.execute(delete(OrderItem).where(OrderItem.order_id == shop.order_id))
        session.execute(delete(Order).where(Order.id == shop.order_id))
        session.execute(delete(Inventory).where(Inventory.warehouse_id == shop.warehouse_id))
        session.execute(delete(ProductSku).where(ProductSku.id.in_(shop.sku_ids)))
        session.execute(delete(Product).where(Product.id == shop.product_id))
        session.execute(delete(UserAddress).where(UserAddress.user_id == shop.consumer_id))
        session.execute(
            text("DELETE ur FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE r.merchant_id = :m"),
            {"m": shop.merchant_id},
        )
        session.execute(
            text("DELETE rp FROM role_permissions rp JOIN roles r ON r.id = rp.role_id WHERE r.merchant_id = :m"),
            {"m": shop.merchant_id},
        )
        session.execute(
            text("DELETE FROM auth_sessions WHERE user_id IN (:c, :s)"),
            {"c": shop.consumer_id, "s": shop.staff_id},
        )
        session.execute(delete(Role).where(Role.merchant_id == shop.merchant_id))
        session.execute(delete(User).where(User.id.in_([shop.consumer_id, shop.staff_id])))
        session.execute(delete(Warehouse).where(Warehouse.id == shop.warehouse_id))
        # Phase 6 appends an `outbox_messages` row per settlement; RESTRICT FK, so it
        # goes before the merchant (the Phase 6 teardown obligation).
        session.execute(
            delete(OutboxMessage).where(OutboxMessage.merchant_id == shop.merchant_id)
        )
        session.execute(delete(Merchant).where(Merchant.id == shop.merchant_id))
        session.commit()


@pytest.fixture(scope="module")
def shop_token(shop: Shop) -> str:
    """A real consumer access token, minted through ``AuthService.login``.

    Module-scoped because minting runs argon2 and logs an auth session; the token is
    valid for the whole module and the auth session is cleaned up by the teardown.
    """
    session = get_session_factory()()
    try:
        issued = AuthService(session, get_settings()).login(
            identifier=shop.consumer_username, password=PASSWORD, client_ip="127.0.0.1"
        )
        session.commit()
        return issued.access_token
    finally:
        session.close()


@pytest.fixture(scope="module")
def staff_token(shop: Shop) -> str:
    """A real, merchant-scoped console operator token."""
    session = get_session_factory()()
    try:
        issued = AuthService(session, get_settings()).login(
            identifier=shop.staff_username, password=PASSWORD, client_ip="127.0.0.1"
        )
        session.commit()
        return issued.access_token
    finally:
        session.close()


@pytest.fixture
def fresh_order(shop: Shop) -> Iterator[Shop]:
    """A **second** order in ``UNPAID`` with no payment attempt, for the create path.

    A separate order rather than resetting the module fixture's one: the settle tests
    consume ``shop``'s attempt, and a test that mutated shared state back would make the
    suite order-dependent - the failure mode where a green run tells you nothing.
    """
    factory = get_session_factory()
    marker = f"{shop.marker}X"
    now = utc_now()
    created: dict[str, int] = {}

    with factory() as session:
        order = Order(
            merchant_id=shop.merchant_id,
            user_id=shop.consumer_id,
            order_no=f"NVPITX{marker}",
            client_request_id=f"pit-x-{marker}",
            request_hash="c" * 64,
            order_status=OrderStatus.PENDING_PAYMENT.value,
            payment_status=PaymentStatus.UNPAID.value,
            fulfillment_status="UNFULFILLED",
            after_sale_status="NONE",
            original_amount=LINE_PAYABLES[0],
            promotion_discount_amount=0,
            coupon_discount_amount=0,
            shipping_amount=0,
            payable_amount=LINE_PAYABLES[0],
            paid_amount=0,
            refunded_amount=0,
            address_id=shop.address_id,
            receiver_name="Payment IT Receiver",
            receiver_phone="13800002222",
            address_snapshot={"full_address": "GuangdongShenzhenNanshanPayment IT Road 2"},
            item_count=LINE_QUANTITIES[0],
            first_item_name="Payment IT Product Payment IT SKU 1",
            expires_at=now + timedelta(minutes=30),
            created_at=now,
            updated_at=now,
        )
        session.add(order)
        session.flush()
        item = OrderItem(
            order_id=order.id,
            warehouse_id=shop.warehouse_id,
            product_id=shop.product_id,
            sku_id=shop.sku_ids[0],
            product_name="Payment IT Product",
            sku_name="Payment IT SKU 1",
            unit_price=UNIT_PRICES[0],
            quantity=LINE_QUANTITIES[0],
            original_amount=LINE_PAYABLES[0],
            promotion_discount_amount=0,
            coupon_discount_amount=0,
            allocated_discount_amount=0,
            payable_amount=LINE_PAYABLES[0],
            refunded_amount=0,
            after_sale_status="NONE",
            created_at=now,
            updated_at=now,
        )
        session.add(item)
        session.flush()
        created["order_id"] = order.id
        created["order_item_id"] = item.id
        session.commit()

    try:
        yield Shop(
            marker=marker,
            merchant_id=shop.merchant_id,
            product_id=shop.product_id,
            sku_ids=shop.sku_ids,
            warehouse_id=shop.warehouse_id,
            consumer_id=shop.consumer_id,
            consumer_username=shop.consumer_username,
            staff_id=shop.staff_id,
            staff_username=shop.staff_username,
            address_id=shop.address_id,
            order_id=created["order_id"],
            order_no=f"NVPITX{marker}",
            payment_id=0,
            payment_no="",
            order_item_ids=(created["order_item_id"], created["order_item_id"]),
            extra={"payable_amount": LINE_PAYABLES[0]},
        )
    finally:
        with get_session_factory()() as session:
            session.execute(delete(Payment).where(Payment.order_id == created["order_id"]))
            session.execute(
                delete(OrderStatusLog).where(OrderStatusLog.order_id == created["order_id"])
            )
            session.execute(
                delete(OrderItem).where(OrderItem.order_id == created["order_id"])
            )
            session.execute(delete(Order).where(Order.id == created["order_id"]))
            session.commit()


def fresh_connection_row(sql: str, params: dict) -> tuple:
    """Read post-mutation state on a **fresh connection**.

    ``HANDOFF.md`` section 6: a read through the session that performed the writes can
    compare the snapshot against itself and pass as a false green, so every assertion about
    committed state goes through here.
    """
    engine = configure_database()
    with engine.connect() as connection:
        return connection.execute(text(sql), params).one()


def utc_stamp() -> str:
    """The current unix timestamp as a string, for a callback header."""
    return str(int(datetime.now(UTC).timestamp()))
