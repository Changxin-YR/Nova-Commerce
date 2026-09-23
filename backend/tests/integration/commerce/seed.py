"""The shared Phase 5 commerce seed - the one merchant/product/stock/buyer every test imports.

    Shop, shop, paid_order, make_order, settle_order, purge_shop, + read-back helpers

`PHASE5_DESIGN` section 12 puts this file here for a specific reason: "every Phase 5
test imports its fixtures/helpers from there rather than re-deriving a
merchant/SKU/warehouse seed four times". Four authors seeding four merchants would
produce four subtly different shops, and the first symptom would be a test that
passes alone and fails in the suite because somebody else's warehouse was picked as
the default.

## Why the seed commits instead of rolling back

The Phase 5 workflows **own their transactions** and commit (design section 6.1 step
11, 6.2 step 7): `CreateOrderWorkflow`, `PaymentSuccessWorkflow` and `RefundWorkflow`
each commit exactly once. A rolled-back outer transaction would be invisible to them
- the payment callback path in particular has to read *committed* rows through a
different session to model a second delivery. So every fixture commits its seed under
a unique marker and :func:`purge_shop` deletes it in foreign-key order afterwards.
That is the same shape `tests/integration/order/conftest.py` uses, for the same
reason.

## `paid_order` drives the REAL payment path, not a hand-written row

It creates the order through `CreateOrderWorkflow`, creates the attempt through
`PaymentService.create`, and settles it through `PaymentSuccessWorkflow` with a
correctly signed provider callback - signed with the production `sign_body`, not a
test-local copy, because a second implementation of a signature scheme is how a suite
passes against a verifier no provider agrees with.

Driving the real path rather than writing `payment_status=PAID` is what makes this
seed worth sharing: a test that starts from a genuinely settled order inherits every
side effect the settlement is supposed to have (the `ORDER_DEDUCT` movement per line,
the fulfillment shell and its items, the `PAID` order with `paid_amount` set, the
`PROCESSING` status log, the `PROCESSED` callback row). A hand-written row silently
omits them, and the test then passes for a state the product can never actually
reach.

## What teardown has to know

The six Phase 5 tables and the phase-4 tables the workflows write are unwound
children-first, because most of these FKs are `RESTRICT` precisely so history cannot
be deleted out from under a live row. :func:`purge_shop` also removes every order and
package belonging to the marker's merchant, not only the ones this module created -
so a test that makes its own orders still cleans up.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.modules.aftersales.models import AfterSale, AfterSaleItem, Refund
from app.modules.catalog.models import Product, ProductImage, ProductSku
from app.modules.fulfillment.models import Fulfillment, FulfillmentItem
from app.modules.identity.enums import DataScope, PermissionCode, UserType
from app.modules.identity.models import Merchant, Permission, Role, User, UserAddress
from app.modules.identity.repository import RoleRepository
from app.modules.identity.security import hash_password
from app.modules.identity.service import Principal
from app.modules.inventory.enums import MovementType, OperatorType, ReferenceType
from app.modules.inventory.models import Inventory, InventoryMovement, Warehouse
from app.modules.order.models import Order, OrderItem, OrderStatusLog
from app.modules.order.service import OrderService
from app.modules.order.workflow import OrderLineInput
from app.modules.payment.enums import PaymentChannel
from app.modules.payment.models import Payment, PaymentCallback
from app.modules.payment.providers import (
    CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED,
    callback_secret_for,
    sign_body,
)
from app.modules.payment.service import PaymentService
from app.modules.payment.workflow import CallbackExecution, CallbackRequest, PaymentSuccessWorkflow
from app.shared.db.base import utc_now
from app.shared.db.models.idempotency import IdempotencyRecord
from app.shared.db.session import get_session_factory

__all__ = [
    "OPENING_STOCK",
    "PASSWORD",
    "Shop",
    "as_utc",
    "items_of",
    "load_order",
    "make_order",
    "movements_for",
    "order_count_for",
    "paid_order",
    "purge_shop",
    "read_position",
    "settle_order",
    "shop",
    "status_logs",
]

#: The dev password for every fixture account. Argon2-hashed like a real one.
PASSWORD = "Correct-Horse-Battery-9"

#: Opening stock per SKU. Large enough that a test never accidentally exhausts it, so a
#: 40000 from a test means the test is broken rather than the code.
OPENING_STOCK = 1000

#: Per-unit prices for the three SKUs. Deliberately **not** round percentages of each
#: other: a cart mixing them leaves a genuine remainder for the pro-rata allocator's
#: last line to absorb (INV-006), and a refund split over them does the same. Testing
#: the remainder rule with prices that divide perfectly is the way to have a green
#: suite and a broken invariant.
SKU_PRICES: tuple[int, int, int] = (1999, 2999, 999)


@dataclass(frozen=True, slots=True)
class Shop:
    """Everything one Phase 5 test needs, with the ids it must assert against.

    ``created_warehouse`` records whether the fixture had to create the merchant's
    default warehouse or found one already. It is not bookkeeping for its own sake:
    ``CreateOrderWorkflow`` resolves the default warehouse *for the order's merchant*,
    so a warehouse created here must be deleted here, while one that already existed
    must not be.
    """

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
        """A merchant-scoped console operator holding ``order:read``.

        The Phase 5 endpoints need more than that (`after-sale:approve`,
        `refund:execute`), and a test that needs one should add the permission to its
        own principal rather than widening this one - a fixture that grants everything
        makes an authorization test unable to fail.
        """
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
        ``idempotency_records`` rows behind that no later test can attribute, and that
        table has no owner column to clean up by.
        """
        return f"{self.marker}-{suffix}"[:128]

    def client_request_id(self, suffix: str) -> str:
        return f"{self.marker}-{suffix}"[:64]


# ---------------------------------------------------------------------------
# Fixture plumbing
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def engine():
    """The configured engine, module-scoped: connecting per test is the slow part."""
    from app.shared.db.session import configure_database

    return configure_database()


def _resolve_or_create_warehouse(session: Session, *, merchant_id: int) -> tuple[Warehouse, bool]:
    """Return (or create) an ACTIVE warehouse **owned by this test merchant**.

    ``CreateOrderWorkflow`` resolves the default warehouse for the order's merchant
    (``resolve_warehouse(None, merchant_id=...)``). Resolving without the merchant
    would pick the lowest-id active warehouse in the whole database, which may belong
    to somebody else; the fixture therefore gives each test merchant its own row, so
    the test passes against the real (merchant-scoped) resolution rather than a
    cross-tenant lock.
    """
    from app.core.errors import WarehouseNotFoundError
    from app.modules.inventory.service import InventoryService

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
def shop(engine, request) -> Iterator[Shop]:
    """A committed merchant / product / three SKUs / stock / buyer / address / staff user.

    Three SKUs at 1999 / 2999 / 999 - see :data:`SKU_PRICES` for why those are not
    round. Opening stock is paired with a matching ``PURCHASE_IN`` movement, because
    seeding a balance without the movement that explains it makes the row
    unexplainable by its own ledger (INV-007) and `verify_ledger` correctly flags it:
    the fixture has to be honest, not merely convenient.

    ## Why this uses ``addfinalizer`` and not ``try/finally`` around ``yield``

    This fixture **commits** rows as it builds them (the workflows under test own their
    transactions and commit, so a rolled-back outer transaction would be invisible to
    them). That makes cleanup correctness rather than tidiness: any row left behind
    changes what later tests see.

    A ``try/finally`` around the ``yield`` is enough when the *test* fails - pytest
    resumes the generator, so the ``finally`` runs. It is **not** enough when the failure
    happens *during setup*: the generator is abandoned before it ever yields, so
    ``finally`` never executes and the already-committed rows stay in the database. That
    is the defect design section 13.5 records - residue that manufactures failures which
    do not exist - and it is the case this fixture could not previously survive.

    ``request.addfinalizer`` is registered **first**, before the seed writes anything, so
    cleanup runs on every exit path: setup failure, test failure, skip, or success.
    ``purge_shop`` is idempotent and marker-scoped, so being called on a half-built shop
    is safe, and the ``finally`` below is kept as a belt-and-braces path for the normal
    case.

    ``created`` is passed **by reference** and filled in as the seed proceeds: a failure
    half-way means the finalizer sees only the ids that actually exist, which is exactly
    the scoping rule - it can never delete a row this test did not create.
    """
    factory = get_session_factory()
    marker = uuid.uuid4().hex[:8]
    created: dict[str, object] = {"created_warehouse": False}
    shop_obj: Shop | None = None

    # Registered before any write, so a setup failure below cannot leak committed rows.
    request.addfinalizer(lambda: purge_shop(created, marker=marker))

    with factory() as session:
        merchant = Merchant(code=f"M{marker}"[:24], name=f"Phase5 Test {marker}")
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

        skus = []
        for index, price in enumerate(SKU_PRICES, start=1):
            sku = ProductSku(
                merchant_id=merchant.id,
                product_id=product.id,
                sku_no=f"S-{marker}-{index}",
                sku_code=f"sku-{marker}-{index}",
                name=f"SKU {index}",
                price_amount=price,
                cost_amount=price // 2,
                status="ACTIVE",
                attribute_snapshot={"color": "titanium", "index": index},
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
            session.add(
                Inventory(
                    merchant_id=merchant.id,
                    warehouse_id=warehouse.id,
                    sku_id=sku.id,
                    available_qty=OPENING_STOCK,
                    locked_qty=0,
                    safety_stock=0,
                )
            )
            session.flush()
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
                    reason="Phase 5 shared seed: opening stock",
                )
            )
        session.flush()

        consumer = User(
            username=f"buyer_{marker}",
            email=f"buyer_{marker}@example.test",
            password_hash=hash_password(PASSWORD),
            display_name="Phase 5 Test Buyer",
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
            display_name="Phase 5 Test Operator",
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
                    description="Phase 5 shared seed",
                )
            )
            created["permission_id"] = permission.id
        roles.grant_permission(role_id=role.id, permission_id=permission.id)
        roles.assign_role(user_id=staff.id, role_id=role.id)

        address = UserAddress(
            merchant_id=None,
            user_id=consumer.id,
            receiver_name="Zhang San",
            receiver_phone="13800005678",
            province="Guangdong",
            city="Shenzhen",
            district="Nanshan",
            detail="Keji Yuan Road 1, Building A, Room 801",
            postal_code="518000",
            tag="HOME",
            is_default=True,
        )
        session.add(address)
        session.flush()
        created["address_id"] = address.id

        session.commit()

        shop_obj = Shop(
            marker=marker,
            merchant_id=merchant.id,
            product_id=product.id,
            sku_ids=tuple(sku.id for sku in skus),  # type: ignore[arg-type]
            sku_prices=SKU_PRICES,
            warehouse_id=warehouse.id,
            consumer_id=consumer.id,
            consumer_username=consumer.username,
            staff_id=staff.id,
            staff_username=staff.username,
            address_id=address.id,
            created_warehouse=created_warehouse,
        )

    assert shop_obj is not None
    try:
        yield shop_obj
    finally:
        # Normally already handled by the finalizer; kept so the happy path does not
        # depend on finalizer ordering, and so a reader sees the cleanup where the
        # ``yield`` is.
        purge_shop(created, marker=marker)


# ---------------------------------------------------------------------------
# Creating an order, and settling it through the real payment path
# ---------------------------------------------------------------------------
def make_order(
    shop: Shop,
    *,
    lines: Sequence[OrderLineInput] | None = None,
    suffix: str = "o1",
    address_id: int | None = None,
) -> Order:
    """Create one order through `CreateOrderWorkflow` in its own session.

    Its own session on purpose: the workflow takes the inventory row's ``FOR UPDATE``
    lock and commits, and a caller sharing this session would be holding locks across
    the test's own reads. Defaults to one unit of the first SKU.
    """
    factory = get_session_factory()
    session = factory()
    try:
        result = OrderService(session).create_order(
            principal=shop.consumer,
            items=list(lines) if lines else [OrderLineInput(shop.sku_ids[0], 1)],
            address_id=address_id if address_id is not None else shop.address_id,
            client_request_id=shop.client_request_id(suffix),
            idempotency_key=shop.key(suffix),
        )
        return result.order
    finally:
        session.close()


def settle_order(
    shop: Shop,
    order: Order,
    *,
    suffix: str = "p1",
    event_id: str | None = None,
    channel: str = PaymentChannel.MOCK.value,
    amount: int | None = None,
) -> CallbackExecution:
    """Drive an existing order PENDING_PAYMENT -> PAID through the real settlement path.

    Two real steps, in the order the product performs them:

    1. `PaymentService.create` - creates the attempt (``PAYING``), which is what moves
       the *order's* axis ``UNPAID -> PAYING``;
    2. `PaymentSuccessWorkflow.execute` - a **correctly signed** provider callback,
       which is the only thing that may write ``SUCCESS``.

    The signature is produced by the production `sign_body` over the same bytes the
    workflow will hash, so this exercises the real verifier rather than bypassing it.
    ``amount`` may be overridden to build a **mismatch** deliberately
    (``PAYMENT_AMOUNT_MISMATCH``, 60002) - the workflow refuses it and commits nothing,
    so the caller can assert the refusal without a second helper.

    Returns the `CallbackExecution`, because some tests need ``replayed``/``applied``
    rather than the order.
    """
    factory = get_session_factory()

    session = factory()
    try:
        attempt = PaymentService(session).create(
            principal=shop.consumer,
            order_no=order.order_no,
            channel=channel,
            client_request_id=shop.client_request_id(f"{suffix}-cr"),
            idempotency_key=shop.key(f"{suffix}-key"),
        )
        payment_no = attempt.payment.payment_no
        payable = attempt.payment.amount
    finally:
        session.close()

    request = signed_success_callback(
        payment_no=payment_no,
        order_no=order.order_no,
        amount=payable if amount is None else amount,
        event_id=event_id or f"evt-{shop.marker}-{suffix}",
        transaction_no=f"txn-{shop.marker}-{suffix}",
        provider=channel,
    )

    session = factory()
    try:
        return PaymentSuccessWorkflow(session).execute(request)
    finally:
        session.close()


def signed_success_callback(
    *,
    payment_no: str,
    order_no: str,
    amount: int,
    event_id: str,
    transaction_no: str,
    provider: str = PaymentChannel.MOCK.value,
    secret: str | None = None,
    timestamp: str | None = None,
    payload: dict | None = None,
) -> CallbackRequest:
    """A provider settlement notification, signed exactly as a real provider signs.

    Exposed (rather than inlined into :func:`settle_order`) because the FG-11
    concurrency gate needs to build the *same* event id many times over from several
    threads - the duplicate-delivery case - and because a signature test needs to
    tamper with the body while keeping a syntactically valid signature.
    """
    resolved_secret = secret or callback_secret_for(get_settings(), provider)
    if resolved_secret is None:
        msg = f"no callback secret configured for provider {provider!r}"
        raise AssertionError(msg)

    stamp = timestamp or str(int(time.time()))
    body = payload or {
        "payment_no": payment_no,
        "order_no": order_no,
        "event_type": CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED,
        "transaction_no": transaction_no,
        "amount": amount,
    }
    raw_body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return CallbackRequest(
        provider=provider,
        event_id=event_id,
        event_type=CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED,
        timestamp=stamp,
        signature=sign_body(secret=resolved_secret, timestamp=stamp, raw_body=raw_body),
        raw_body=raw_body,
        payload=body,
    )


def paid_order(
    shop: Shop,
    *,
    lines: Sequence[OrderLineInput] | None = None,
    suffix: str = "paid1",
) -> Order:
    """Create an order **and** settle it: a genuinely PAID order, with every side effect.

    This is the helper `PHASE5_DESIGN` section 12 promises, and it drives the real
    payment path rather than writing ``payment_status=PAID``:

    * order created via `CreateOrderWorkflow` (``PENDING_PAYMENT``, stock reserved);
    * payment attempt created via `PaymentService.create` (``PAYING``);
    * settled via `PaymentSuccessWorkflow` with a signed callback.

    A test starting here therefore inherits what settlement is *supposed* to produce -
    the ``ORDER_DEDUCT`` movement per line, the fulfillment shell and its items, the
    ``PENDING_PAYMENT -> PROCESSING`` status log, and the ``PROCESSED`` callback row.
    A hand-written row omits them silently, and the test then passes for a state the
    product can never reach.

    Raises `AssertionError` if the settlement did not apply, so a fixture failure
    surfaces as itself rather than as a confusing assertion three lines later.
    """
    order = make_order(shop, lines=lines, suffix=f"{suffix}-o")
    execution = settle_order(shop, order, suffix=f"{suffix}-p")
    assert execution.applied, (
        f"the shared seed could not settle order {order.order_no}: "
        f"status={execution.status} error_code={execution.error_code} detail={execution.detail}"
    )
    return order


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
def purge_shop(created: dict[str, object], *, marker: str) -> None:
    """Delete everything the fixture and its tests created, in dependency order.

    Order matters and is not guesswork: these FKs are ``RESTRICT`` precisely so that
    history cannot be deleted out from under a live row, which means the unwinding has
    to go children-first. Anything left behind would make the next run behave
    differently from this one, and a suite whose second run differs from its first is
    a suite nobody trusts.

    The Phase 5 tables are unwound in the order the workflows created them, and
    anything belonging to this marker's merchant is removed - not only the rows this
    module wrote - so a test that creates its own orders and packages still cleans up.
    """
    factory = get_session_factory()
    merchant_id = created.get("merchant_id")
    marker_like = f"{marker}%"
    # Idempotent by construction: every statement is scoped to ids or marker prefixes
    # that this test created, so a second call (finalizer plus finally) is a no-op.
    # Nothing here deletes by a blanket pattern or truncates a table.
    sku_ids = tuple(created.get("sku_ids") or ())  # type: ignore[arg-type]

    with factory() as session:
        order_ids: list[int] = []
        payment_ids: list[int] = []
        claim_ids: list[int] = []
        if merchant_id is not None:
            order_ids = [
                int(row)
                for row in session.execute(
                    select(Order.id).where(Order.merchant_id == merchant_id)
                )
                .scalars()
                .all()
            ]
            payment_ids = [
                int(row)
                for row in session.execute(
                    select(Payment.id).where(Payment.merchant_id == merchant_id)
                )
                .scalars()
                .all()
            ]
            claim_ids = [
                int(row)
                for row in session.execute(
                    select(AfterSale.id).where(AfterSale.merchant_id == merchant_id)
                )
                .scalars()
                .all()
            ]

        # -- Phase 5, children first -------------------------------------
        if claim_ids:
            session.execute(delete(Refund).where(Refund.after_sale_id.in_(claim_ids)))
            session.execute(
                delete(AfterSaleItem).where(AfterSaleItem.after_sale_id.in_(claim_ids))
            )
            session.execute(delete(AfterSale).where(AfterSale.id.in_(claim_ids)))
        if order_ids:
            session.execute(delete(Refund).where(Refund.order_id.in_(order_ids)))
            session.execute(
                delete(FulfillmentItem).where(
                    FulfillmentItem.fulfillment_id.in_(
                        select(Fulfillment.id).where(Fulfillment.order_id.in_(order_ids))
                    )
                )
            )
            session.execute(delete(Fulfillment).where(Fulfillment.order_id.in_(order_ids)))
        # `payment_callbacks` has no FK to `payments` (a delivery for an unknown
        # payment_no must still be recordable), so it is matched by the marker's own
        # event ids rather than by cascade.
        session.execute(
            delete(PaymentCallback).where(PaymentCallback.provider_event_id.like(marker_like))
        )
        if payment_ids:
            session.execute(delete(Refund).where(Refund.payment_id.in_(payment_ids)))
            session.execute(delete(Payment).where(Payment.id.in_(payment_ids)))
        if order_ids:
            session.execute(delete(OrderStatusLog).where(OrderStatusLog.order_id.in_(order_ids)))
            session.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            session.execute(delete(Order).where(Order.id.in_(order_ids)))

        # -- idempotency records, by marker prefix -----------------------
        # `idempotency_records` has no owner column, so a prefix is the only reliable
        # attribution - which is why `Shop.key()` builds keys that way rather than
        # randomising them.
        session.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.idempotency_key.like(marker_like))
        )

        # -- inventory and catalogue -------------------------------------
        if sku_ids:
            session.execute(delete(InventoryMovement).where(InventoryMovement.sku_id.in_(sku_ids)))
            session.execute(delete(Inventory).where(Inventory.sku_id.in_(sku_ids)))
            session.execute(delete(ProductSku).where(ProductSku.id.in_(sku_ids)))
        if created.get("image_id"):
            session.execute(delete(ProductImage).where(ProductImage.id == created["image_id"]))
        if created.get("product_id"):
            session.execute(delete(Product).where(Product.id == created["product_id"]))

        # -- identity ----------------------------------------------------
        if created.get("consumer_id"):
            session.execute(
                delete(UserAddress).where(UserAddress.user_id == created["consumer_id"])
            )
        if created.get("role_id"):
            session.execute(
                text("DELETE FROM role_permissions WHERE role_id = :id"),
                {"id": created["role_id"]},
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
            if created.get(key):
                session.execute(text("DELETE FROM users WHERE id = :id"), {"id": created[key]})
        if created.get("created_warehouse") and created.get("warehouse_id"):
            session.execute(
                text("DELETE FROM warehouses WHERE id = :id"), {"id": created["warehouse_id"]}
            )
        if merchant_id is not None:
            session.execute(text("DELETE FROM merchants WHERE id = :id"), {"id": merchant_id})
        session.commit()


# ---------------------------------------------------------------------------
# Read-back helpers
#
# These exist so a test never has to reach for SQL to assert on persisted state, and
# so that "read it back with a fresh query" is the path of least resistance. That
# matters for the mandatory gates: HANDOFF section 6 records that reading the
# post-mutation state on the *same* connection under REPEATABLE READ compares a
# snapshot against itself and passes as a false green.
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
    order = session.execute(select(Order).where(Order.order_no == order_no)).scalars().first()
    assert order is not None, f"order {order_no} disappeared"
    return order


def status_logs(session: Session, *, order_id: int) -> list[OrderStatusLog]:
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
