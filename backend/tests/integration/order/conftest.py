"""Shared fixtures for the Phase 4 order integration tests (FG-10).

These tests run against **real MySQL**. That is not ceremony: the properties under
test are database properties - the two unique constraints that make INV-015 real,
``SELECT ... FOR UPDATE`` on the inventory row, the ``CHECK`` constraints that
restate INV-006 per row, and the fact that ``order_items`` has no catalogue join to
read from. A mocked session would happily "pass" all of it (§113 forbids exactly
that: 禁止 Mock DB 后宣称通过).

## Why the fixtures are committed, not rolled back

The create workflow **owns its transaction** and commits (PHASE4_DESIGN §7 step 9,
§49). A rolled-back outer transaction would be invisible to it, and the replay path
in particular needs to read a *committed* order to be tested at all. So every
fixture commits its seed under a unique marker and the teardown deletes it in
dependency order - the same shape the FG-09 concurrency test uses, for the same
reason.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, text
from sqlalchemy.orm import Session

from app.modules.catalog.models import Product, ProductImage, ProductSku
from app.modules.identity.enums import DataScope, PermissionCode, UserType
from app.modules.identity.models import (
    Merchant,
    Permission,
    Role,
    User,
    UserAddress,
)
from app.modules.identity.repository import RoleRepository
from app.modules.identity.security import hash_password
from app.modules.identity.service import Principal
from app.modules.inventory.enums import MovementType, OperatorType, ReferenceType
from app.modules.inventory.models import Inventory, InventoryMovement, Warehouse
from app.modules.inventory.service import InventoryService
from app.modules.order.models import Order, OrderItem, OrderStatusLog
from app.shared.db.base import utc_now
from app.shared.db.models.idempotency import IdempotencyRecord
from app.shared.db.session import configure_database, get_session_factory

#: The dev password for every fixture account. Argon2-hashed like a real one.
PASSWORD = "Correct-Horse-Battery-9"

#: Opening stock per SKU. Large enough that a test never accidentally exhausts it,
#: so a 40000 from a test would mean the test is broken rather than the code.
OPENING_STOCK = 1000


@dataclass(frozen=True, slots=True)
class Shop:
    """Everything one order test needs, with the ids it must assert against."""

    marker: str
    merchant_id: int
    product_id: int
    sku_ids: tuple[int, int, int]
    sku_prices: tuple[int, int, int]
    warehouse_id: int
    consumer_id: int
    consumer_username: str
    staff_id: int
    staff_username: str
    address_id: int
    created_warehouse: bool

    @property
    def consumer(self) -> Principal:
        """The buyer as the service layer sees them: server-resolved, never from input."""
        return Principal(
            user_id=self.consumer_id,
            user_type=UserType.CONSUMER.value,
            merchant_id=None,
            roles=(),
            permissions=frozenset(),
            data_scope=DataScope.SELF,
            session_id=f"test-{self.marker}",
            is_staff=False,
        )

    @property
    def staff(self) -> Principal:
        """A merchant-scoped console operator holding ``order:read``."""
        return Principal(
            user_id=self.staff_id,
            user_type=UserType.STAFF.value,
            merchant_id=self.merchant_id,
            roles=("ORDER_OPERATOR",),
            permissions=frozenset({PermissionCode.ORDER_READ.value}),
            data_scope=DataScope.MERCHANT,
            session_id=f"test-staff-{self.marker}",
            is_staff=True,
        )

    def key(self, suffix: str) -> str:
        """A marker-prefixed idempotency key, so teardown can find every record.

        Prefixing rather than randomising is deliberate: a random key would leave
        ``idempotency_records`` rows behind that no later test can attribute, and the
        table has no owner column to clean up by.
        """
        return f"{self.marker}-{suffix}"[:128]

    def client_request_id(self, suffix: str) -> str:
        return f"{self.marker}-{suffix}"[:64]


@pytest.fixture(scope="module")
def engine():
    return configure_database()


@pytest.fixture
def client():
    """An HTTP client that can actually exercise the ASGI stack.

    **Why this exists instead of the shared ``client`` fixture in
    ``tests/conftest.py``.** That fixture builds
    ``httpx.Client(transport=httpx.ASGITransport(app=app))``, which cannot work with
    httpx 0.28: ``ASGITransport`` implements only ``handle_async_request``, so a
    *synchronous* ``httpx.Client`` raises ``AttributeError: 'ASGITransport' object has
    no attribute 'handle_request'`` on its first call. No test used the fixture before
    Phase 4, so the defect was latent rather than visible.

    ``fastapi.testclient.TestClient`` is used here because it drives the app
    synchronously through its own portal and needs no async test plumbing. The shared
    fixture is left untouched (it is not this module's file) and the defect is reported
    to the captain - a broken fixture that silently fails on first use is exactly the
    kind of thing that should not be quietly worked around.
    """
    from fastapi.testclient import TestClient

    from app.core.config import get_settings
    from app.main import create_app

    with TestClient(create_app(get_settings())) as http_client:
        yield http_client


def _resolve_or_create_warehouse(
    session: Session, *, merchant_id: int
) -> tuple[Warehouse, bool]:
    """Return (or create) an ACTIVE warehouse **owned by this test merchant**.

    ``CreateOrderWorkflow`` resolves the default warehouse *for the order's merchant*
    (``resolve_warehouse(None, merchant_id=...)``). Resolving without the merchant
    would pick the lowest-id active warehouse in the whole database, which may belong
    to somebody else; the fixture therefore gives each test merchant its own row, so
    the test passes against the real (merchant-scoped) resolution rather than a
    cross-tenant lock.
    """
    from app.core.errors import WarehouseNotFoundError

    service = InventoryService(session)
    try:
        return service.resolve_warehouse(None, merchant_id=merchant_id), False
    except WarehouseNotFoundError:
        warehouse = Warehouse(
            merchant_id=merchant_id,
            code="MAIN",
            name="Main Warehouse",
            is_default=True,
            status="ACTIVE",
        )
        session.add(warehouse)
        session.flush()
        return warehouse, True


@pytest.fixture
def shop(engine) -> Iterator[Shop]:
    """A committed merchant / product / three SKUs / stock / buyer / address / staff user.

    Three SKUs with prices that do **not** divide evenly into round percentages
    (1999 / 2999 / 999), so the §41 pro-rata allocation leaves a genuine remainder for
    the last line to absorb. Testing the remainder rule with prices that divide
    perfectly is the way to have a green suite and a broken invariant.
    """
    factory = get_session_factory()
    marker = uuid.uuid4().hex[:8]
    created: dict[str, object] = {"created_warehouse": False}

    with factory() as session:
        merchant = Merchant(code=f"M{marker}"[:24], name=f"Order Test {marker}")
        session.add(merchant)
        session.flush()
        created["merchant_id"] = merchant.id

        product = Product(
            merchant_id=merchant.id,
            product_no=f"P-{marker}",
            slug=f"p-{marker}",
            name="Nova Phone 15 Pro",
            status="PUBLISHED",
            published_at=utc_now(),
        )
        session.add(product)
        session.flush()
        created["product_id"] = product.id

        prices = (1999, 2999, 999)
        skus = []
        for index, price in enumerate(prices, start=1):
            sku = ProductSku(
                merchant_id=merchant.id,
                product_id=product.id,
                sku_no=f"S-{marker}-{index}",
                sku_code=f"sku-{marker}-{index}",
                name=f"原色钛金属款 {index}",
                price_amount=price,
                cost_amount=price // 2,
                status="ACTIVE",
                attribute_snapshot={"color": "原色钛金属", "index": index},
            )
            session.add(sku)
            skus.append(sku)
        session.flush()
        created["sku_ids"] = tuple(sku.id for sku in skus)

        image = ProductImage(
            merchant_id=merchant.id,
            product_id=product.id,
            bucket="product-images",
            object_key=f"products/{marker}/primary.png",
            checksum="0" * 64,
            content_type="image/png",
            size_bytes=1024,
            role="PRIMARY",
            sort_order=0,
        )
        session.add(image)
        session.flush()
        created["image_id"] = image.id

        warehouse, created_warehouse = _resolve_or_create_warehouse(
            session, merchant_id=merchant.id
        )
        created["warehouse_id"] = warehouse.id
        created["created_warehouse"] = created_warehouse

        for sku in skus:
            inventory = Inventory(
                merchant_id=merchant.id,
                warehouse_id=warehouse.id,
                sku_id=sku.id,
                available_qty=OPENING_STOCK,
                locked_qty=0,
                safety_stock=0,
            )
            session.add(inventory)
            session.flush()
            # The opening balance AND the movement that explains it. Seeding the
            # balance alone would make the row unexplainable by its own ledger
            # (INV-007), which `verify_ledger` correctly flags - so the fixture has
            # to be honest, not merely convenient.
            session.add(
                InventoryMovement.build(
                    warehouse_id=warehouse.id,
                    sku_id=sku.id,
                    movement_type=MovementType.PURCHASE_IN,
                    before_available=0,
                    after_available=OPENING_STOCK,
                    before_locked=0,
                    after_locked=0,
                    idempotency_key=f"seed-{marker}-{sku.id}",
                    reference_type=ReferenceType.PURCHASE_ORDER,
                    operator_type=OperatorType.SYSTEM,
                    reason="Phase 4 fixture: opening stock",
                )
            )
        session.flush()

        consumer = User(
            username=f"buyer_{marker}",
            email=f"buyer_{marker}@example.test",
            password_hash=hash_password(PASSWORD),
            display_name="Order Test Buyer",
            user_type=UserType.CONSUMER.value,
            status="ACTIVE",
            merchant_id=None,
        )
        session.add(consumer)
        session.flush()
        created["consumer_id"] = consumer.id

        staff = User(
            username=f"staff_{marker}",
            email=f"staff_{marker}@example.test",
            password_hash=hash_password(PASSWORD),
            display_name="Order Test Operator",
            user_type=UserType.STAFF.value,
            status="ACTIVE",
            merchant_id=merchant.id,
        )
        session.add(staff)
        session.flush()
        created["staff_id"] = staff.id

        roles = RoleRepository(session)
        role = roles.add_role(
            Role(
                merchant_id=merchant.id,
                code="ORDER_OPERATOR",
                name="Order operator",
                data_scope=DataScope.MERCHANT.value,
            )
        )
        created["role_id"] = role.id
        permission = roles.get_permission_by_code(PermissionCode.ORDER_READ.value)
        if permission is None:
            permission = roles.add_permission(
                Permission(
                    code=PermissionCode.ORDER_READ.value,
                    resource="order",
                    action="read",
                    description="Phase 4 fixture",
                )
            )
            created["permission_id"] = permission.id
        roles.grant_permission(role_id=role.id, permission_id=permission.id)
        roles.assign_role(user_id=staff.id, role_id=role.id)

        address = UserAddress(
            merchant_id=None,
            user_id=consumer.id,
            receiver_name="张三丰",
            receiver_phone="13800005678",
            province="广东省",
            city="深圳市",
            district="南山区",
            detail="科技园路1号A座1801室",
            postal_code="518000",
            tag="HOME",
            is_default=True,
        )
        session.add(address)
        session.flush()
        created["address_id"] = address.id

        session.commit()

        shop = Shop(
            marker=marker,
            merchant_id=merchant.id,
            product_id=product.id,
            sku_ids=tuple(sku.id for sku in skus),  # type: ignore[arg-type]
            sku_prices=prices,
            warehouse_id=warehouse.id,
            consumer_id=consumer.id,
            consumer_username=consumer.username,
            staff_id=staff.id,
            staff_username=staff.username,
            address_id=address.id,
            created_warehouse=created_warehouse,
        )

    try:
        yield shop
    finally:
        _purge(created, marker=marker)


def _purge(created: dict[str, object], *, marker: str) -> None:
    """Delete everything the fixture and the tests created, in dependency order.

    Order matters and is not guesswork: most of these foreign keys are ``RESTRICT``
    precisely so that history cannot be deleted out from under a live row, which means
    the unwinding has to go children-first. Anything left behind would make the next run
    behave differently from this one, and a suite whose second run differs from its
    first is a suite nobody trusts.
    """
    from sqlalchemy import select

    factory = get_session_factory()
    sku_ids = tuple(created.get("sku_ids") or ())
    with factory() as session:
        order_ids = [
            int(row)
            for row in session.execute(
                select(Order.id).where(Order.merchant_id == created["merchant_id"])
            )
            .scalars()
            .all()
        ]
        if order_ids:
            session.execute(delete(OrderStatusLog).where(OrderStatusLog.order_id.in_(order_ids)))
            session.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            session.execute(delete(Order).where(Order.id.in_(order_ids)))
        # Keyed by the marker prefix: `idempotency_records` has no owner column, so a
        # prefix is the only reliable attribution - which is why `Shop.key()` builds
        # keys that way rather than randomising them.
        session.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.idempotency_key.like(f"{marker}%"))
        )
        if sku_ids:
            session.execute(
                delete(InventoryMovement).where(InventoryMovement.sku_id.in_(sku_ids))
            )
            session.execute(delete(Inventory).where(Inventory.sku_id.in_(sku_ids)))
            session.execute(delete(ProductSku).where(ProductSku.id.in_(sku_ids)))
        if created.get("image_id"):
            session.execute(delete(ProductImage).where(ProductImage.id == created["image_id"]))
        session.execute(delete(Product).where(Product.id == created["product_id"]))
        session.execute(delete(UserAddress).where(UserAddress.user_id == created["consumer_id"]))
        if created.get("role_id"):
            session.execute(
                text("DELETE FROM role_permissions WHERE role_id = :id"), {"id": created["role_id"]}
            )
            session.execute(
                text("DELETE FROM user_roles WHERE role_id = :id"), {"id": created["role_id"]}
            )
            session.execute(text("DELETE FROM roles WHERE id = :id"), {"id": created["role_id"]})
        # ``permissions`` is a GLOBAL vocabulary (``uq_permissions_code``), not
        # fixture data, so it is deliberately NOT deleted here. Deleting it made the
        # seed non-repeatable in two different ways, both measured:
        #
        #   * if the table was empty at seed time, the next run inserted a SECOND row
        #     for the same code -> ``(1062, Duplicate entry 'order:read')``;
        #   * if any ``role_permissions`` row still referenced it, the delete itself
        #     failed -> ``(1452, Cannot add or update a child row)``.
        #
        # Either way the NEXT run's fixture failed, so the symptom looked random - it
        # depended on how many roles happened to reference the permission at that
        # moment - and it hit every suite using the seed rather than only the one that
        # ran first. A global vocabulary row created on demand and never removed is
        # the same choice the catalog and identity fixtures already make for their
        # lookup rows.
        for key in ("consumer_id", "staff_id"):
            session.execute(text("DELETE FROM users WHERE id = :id"), {"id": created[key]})
        if created.get("created_warehouse") and created.get("warehouse_id"):
            session.execute(
                text("DELETE FROM warehouses WHERE id = :id"), {"id": created["warehouse_id"]}
            )
        session.execute(
            text("DELETE FROM merchants WHERE id = :id"), {"id": created["merchant_id"]}
        )
        session.commit()


# ---------------------------------------------------------------------------
# Read-back helpers
# ---------------------------------------------------------------------------
def read_position(session: Session, *, sku_id: int, warehouse_id: int) -> tuple[int, int, int]:
    """``(available, locked, version)`` straight from the row, bypassing the ORM."""
    row = session.execute(
        text(
            "SELECT available_qty, locked_qty, version FROM inventories "
            "WHERE sku_id = :sku AND warehouse_id = :wh"
        ),
        {"sku": sku_id, "wh": warehouse_id},
    ).one()
    return int(row[0]), int(row[1]), int(row[2])


def movements_for(session: Session, *, sku_id: int) -> list[InventoryMovement]:
    from sqlalchemy import select

    return list(
        session.execute(
            select(InventoryMovement)
            .where(InventoryMovement.sku_id == sku_id)
            .order_by(InventoryMovement.id.asc())
        )
        .scalars()
        .all()
    )


def order_count_for(session: Session, *, user_id: int) -> int:
    row = session.execute(
        text("SELECT COUNT(*) FROM orders WHERE user_id = :user"), {"user": user_id}
    ).scalar_one()
    return int(row)


def load_order(session: Session, *, order_no: str) -> Order:
    from sqlalchemy import select

    order = (
        session.execute(select(Order).where(Order.order_no == order_no)).scalars().first()
    )
    assert order is not None, f"order {order_no} disappeared"
    return order


def status_logs(session: Session, *, order_id: int) -> list[OrderStatusLog]:
    from sqlalchemy import select

    return list(
        session.execute(
            select(OrderStatusLog)
            .where(OrderStatusLog.order_id == order_id)
            .order_by(OrderStatusLog.id.asc())
        )
        .scalars()
        .all()
    )


def items_of(session: Session, *, order_id: int) -> list[OrderItem]:
    from sqlalchemy import select

    return list(
        session.execute(
            select(OrderItem).where(OrderItem.order_id == order_id).order_by(OrderItem.id.asc())
        )
        .scalars()
        .all()
    )


def as_utc(value: datetime) -> datetime:
    """MySQL hands back naive UTC for ``DATETIME``; compare knowingly."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "OPENING_STOCK",
    "PASSWORD",
    "Shop",
    "as_utc",
    "items_of",
    "load_order",
    "movements_for",
    "order_count_for",
    "read_position",
    "status_logs",
]
