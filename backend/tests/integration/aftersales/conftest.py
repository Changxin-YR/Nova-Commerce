"""Aftersales fixtures built on the shared Phase 5 seed.

PHASE5_DESIGN section 12 and the t5 brief both require it: every Phase 5 test imports its
fixtures and helpers from ``tests/integration/commerce/seed.py`` rather than re-deriving a
merchant/SKU/warehouse seed. Four authors seeding four shops would produce four subtly
different ones, and the first symptom would be a test that passes alone and fails in the
suite because somebody else's warehouse was picked as the default.

So this module does **not** re-seed anything. It delegates to the shared seed for the world
(merchant, catalogue, stock, buyer, staff, address) and for the paid order, and adds only what
is genuinely after-sales specific:

* :class:`AfterSalesShop`, which wraps the shared ``Shop`` with ``claim``/``approve``/
  ``reject``/``cancel``/``refund`` delegates so the tests read as scenarios rather than as
  plumbing;
* an extension of the seeded staff **role** to carry the after-sales permissions. The shared
  seed grants ``order:read`` only, and a claim cannot be reviewed without
  ``after_sale:review`` / ``refund:execute``. Granting them to the role (rather than making a
  second staff account) is the honest little extra this module needs, and it is done through
  ``RoleRepository`` so the grant looks exactly like a real one - which matters for the HTTP
  tests, whose whole point is that the auth dependency re-resolves permissions from the
  database.

## A genuinely settled order, not a hand-written one

``paid_order`` is the shared seed's helper, and it is the reason this file is short. It drives
the **real** payment path - ``CreateOrderWorkflow``, then ``PaymentService.create``, then
``PaymentSuccessWorkflow`` with a correctly signed provider callback - so a refund test starts
from a state the product can actually reach: ``PROCESSING`` with ``payment_status = PAID``,
one ``ORDER_DEDUCT`` movement per line, a fulfillment shell with its items, the
``PENDING_PAYMENT -> PROCESSING`` status log, and a ``PROCESSED`` callback row. An order with
``payment_status`` written by hand is a state no code path can produce, and every assertion
made against it would be worth less than it looks.

## Three units per line, so a second claim is expressible

The shared seed's products are priced 1999 / 2999 / 999, and this module places its order with
``quantity=3`` per line through the public ``OrderLineInput`` interface - so the real pricing
and allocation path still runs. Three units matters because a claim consumes **units**, not
money: with one unit per line a second claim on the same line is impossible, and the cumulative
per-line cap could not be tested at all. The per-line amounts are then read back from
``order_items`` rather than assumed, because the pricing path (not this fixture) decides them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.modules.aftersales.enums import AfterSaleClaimStatus, AfterSaleType
from app.modules.aftersales.models import AfterSale, AfterSaleItem, Refund
from app.modules.aftersales.schemas import (
    ApplyAfterSaleItemIn,
    ApplyAfterSaleRequest,
    ApproveAfterSaleRequest,
    CancelAfterSaleRequest,
    RejectAfterSaleRequest,
)
from app.modules.aftersales.service import AfterSaleService
from app.modules.aftersales.workflow import RefundWorkflow
from app.modules.fulfillment.models import Fulfillment
from app.modules.identity.enums import PermissionCode
from app.modules.identity.models import Permission, Role, RolePermission
from app.modules.identity.repository import RoleRepository
from app.modules.identity.service import Principal
from app.modules.inventory.models import InventoryMovement
from app.modules.order.models import OrderItem
from app.modules.order.workflow import OrderLineInput
from app.shared.db.session import get_session_factory
from tests.integration.commerce.conftest import engine
from tests.integration.commerce.seed import (
    OPENING_STOCK,
    PASSWORD,
    Shop as SeededShop,
    paid_order,
)

__all__ = [
    "AFTER_SALES_PERMISSIONS",
    "LINE_QUANTITY",
    "OPENING_STOCK",
    "PASSWORD",
    "AfterSaleClaimStatus",
    "AfterSalesShop",
    "as_utc",
    "available_stock",
    "claim_items",
    "engine",
    "load_claim",
    "movements_for",
    "read_claim",
    "read_money",
    "refunds_for",
    "seeded_shop",
]

#: Units per line in every order this module places. See the module docstring.
LINE_QUANTITY = 3

#: The permissions the seeded staff role is extended with. The shared seed grants
#: ``order:read`` only; reviewing a claim and executing a refund need these three.
AFTER_SALES_PERMISSIONS: tuple[str, ...] = (
    PermissionCode.AFTER_SALE_READ.value,
    PermissionCode.AFTER_SALE_REVIEW.value,
    PermissionCode.REFUND_EXECUTE.value,
)


def _resolve_permission(session: Session, roles: RoleRepository, code: str) -> Permission:
    """The permission row for ``code``, created if it does not exist - **race-tolerantly**.

    ``permissions`` is *global vocabulary* keyed by a unique ``code``, and more than one thing
    creates these rows (the shared seed, this fixture, and any other suite's fixture). A plain
    ``SELECT``-then-``INSERT`` is a check-then-act race: if another session commits the same code
    between the two, the ``SELECT`` misses and the ``INSERT`` raises 1062 - which is exactly the
    intermittency this suite showed before it was fixed.

    So the insert is **attempted** and a unique violation is read as "somebody else won". This is
    the same rule the project applies to idempotency keys (``HANDOFF.md`` section 6, and
    ``IdempotencyRepository.insert_in_progress``): let the database make the decision atomically.
    The attempt sits inside a ``SAVEPOINT`` because SQLAlchemy marks the session as needing a
    rollback after an ``IntegrityError``, so without it the follow-up re-read would raise
    ``PendingRollbackError`` instead of returning the winner's row.
    """
    existing = roles.get_permission_by_code(code)
    if existing is not None:
        return existing

    resource, _, action = code.partition(":")
    try:
        with session.begin_nested():
            created = roles.add_permission(
                Permission(
                    code=code,
                    resource=resource,
                    action=action,
                    description="Phase 5 after-sales fixture",
                )
            )
        return created
    except IntegrityError:
        # Lost the race: the winner's row is committed, so read it back.
        won = roles.get_permission_by_code(code)
        if won is None:  # pragma: no cover - the unique index implies it is there
            raise
        return won


def _grant_if_absent(
    session: Session, roles: RoleRepository, *, role_id: int, permission_id: int
) -> None:
    """Grant a permission to a role, tolerating the pair already being granted.

    ``RoleRepository.grant_permission`` inserts unconditionally, so granting a pair that is
    already there raises a duplicate-key error rather than being a no-op. Attempt-and-tolerate
    rather than *read-then-grant*, for the same reason as above: the check-then-act version can
    still lose the race, and a fixture that fails intermittently is worse than one that is
    merely slower.
    """
    already = (
        session.execute(
            select(RolePermission).where(
                RolePermission.role_id == role_id,
                RolePermission.permission_id == permission_id,
            )
        )
        .scalars()
        .first()
    )
    if already is not None:
        return
    try:
        with session.begin_nested():
            roles.grant_permission(role_id=role_id, permission_id=permission_id)
    except IntegrityError:
        # Somebody granted it concurrently; that is the outcome we wanted.
        pass


def _extend_staff_role(seeded_shop: SeededShop) -> None:
    """Grant the after-sales permissions to the shared seed's staff role.

    Through ``RoleRepository`` and the database, not by forging a ``Principal``: the HTTP tests
    depend on the real auth dependency re-resolving these grants from these rows, which is the
    thing worth proving - a principal whose permissions were assembled in Python would let the
    routes pass with the database in any state at all.
    """
    factory = get_session_factory()
    with factory() as session:
        roles = RoleRepository(session)
        role = (
            session.execute(
                select(Role).where(
                    Role.merchant_id == seeded_shop.merchant_id, Role.code == "ORDER_OPERATOR"
                )
            )
            .scalars()
            .first()
        )
        if role is None:  # pragma: no cover - the shared seed always creates it
            raise ValidationError("the shared seed did not create the ORDER_OPERATOR role")

        for permission_code in AFTER_SALES_PERMISSIONS:
            permission = _resolve_permission(session, roles, permission_code)
            _grant_if_absent(
                session, roles, role_id=role.id, permission_id=permission.id
            )
        session.commit()


@dataclass
class AfterSalesShop:
    """The shared seed's world, plus the claim lifecycle as one-line helpers.

    Composition rather than inheritance, deliberately: ``SeededShop`` is owned by the data layer
    and may grow. Wrapping it means this module knows only the attributes it uses, and a change
    there surfaces as an ``AttributeError`` here instead of as a quietly different fixture.
    """

    seeded: SeededShop
    #: The order every test starts from: settled through the real payment path.
    order_no: str
    order_id: int
    order_item_ids: tuple[int, ...]
    order_item_amounts: tuple[int, ...]
    #: Monotonic per instance, so `key()` never repeats within one test.
    _key_counter: int = 0

    # -- pass-through of what the tests read from the seed -----------------
    @property
    def marker(self) -> str:
        return self.seeded.marker

    @property
    def merchant_id(self) -> int:
        return self.seeded.merchant_id

    @property
    def consumer_id(self) -> int:
        return self.seeded.consumer_id

    @property
    def staff_id(self) -> int:
        return self.seeded.staff_id

    @property
    def warehouse_id(self) -> int:
        return self.seeded.warehouse_id

    @property
    def sku_ids(self) -> tuple[int, int, int]:
        return self.seeded.sku_ids

    def _latest_payment(self):
        """The ``payments`` row that settled this order.

        Read through the repository rather than stored on the fixture, so a test that asserts on
        the payment's counters is reading the row the workflows actually wrote - and so the
        property keeps working after a refund has moved those counters.
        """
        from app.modules.payment.repository import PaymentRepository

        factory = get_session_factory()
        with factory() as session:
            return PaymentRepository(session).get_latest_for_order(self.order_id)

    @property
    def payment_id(self) -> int:
        payment = self._latest_payment()
        assert payment is not None, "the settlement should have left a payments row"
        return int(payment.id)

    @property
    def payment_no(self) -> str:
        payment = self._latest_payment()
        assert payment is not None, "the settlement should have left a payments row"
        return str(payment.payment_no)

    @property
    def paid_amount(self) -> int:
        """What the order was actually paid - the sum of its lines' payable amounts.

        Derived from the lines rather than read from a second column, so a test comparing the
        two is comparing independent figures. INV-006 *is* that equality, so deriving it here
        makes the fixture assert the invariant merely by being used.
        """
        return sum(self.order_item_amounts)

    @property
    def consumer(self) -> Principal:
        return self.seeded.consumer

    @property
    def staff(self) -> Principal:
        """The seeded operator, now holding the after-sales permissions.

        Rebuilt here rather than taken straight from the seed because the seed's ``staff``
        property is pinned to ``order:read``. The identity, merchant and data scope all still
        come from the seed, so this is the same account - with the grants its role now really
        has in the database.
        """
        seeded = self.seeded.staff
        return Principal(
            user_id=seeded.user_id,
            user_type=seeded.user_type,
            merchant_id=seeded.merchant_id,
            roles=seeded.roles,
            permissions=frozenset(set(seeded.permissions) | set(AFTER_SALES_PERMISSIONS)),
            data_scope=seeded.data_scope,
            session_id=seeded.session_id,
            is_staff=seeded.is_staff,
        )

    def key(self, suffix: str) -> str:
        """A marker-scoped idempotency key, made unique per call.

        The counter is load-bearing: a test that files two claims with different amounts must
        not reuse one key, or the second is refused as *"this key was already used with
        different claim details"* - which looks like a bug in the service and is really a
        fixture that handed out the same key twice.
        """
        self._key_counter += 1
        return self.seeded.key(f"{suffix}-{self._key_counter}")

    def client_request_id(self, suffix: str) -> str:
        return self.seeded.client_request_id(suffix)

    # -- the claim lifecycle, through the real services --------------------
    def claim_request(
        self,
        *,
        amount: int,
        item_indexes: tuple[int, ...] = (0,),
        quantities: tuple[int, ...] | None = None,
        claim_type: str = AfterSaleType.REFUND_ONLY,
        suffix: str = "claim",
    ) -> ApplyAfterSaleRequest:
        """A well-formed apply request for this shop's paid order."""
        qtys = quantities or tuple(1 for _ in item_indexes)
        return ApplyAfterSaleRequest(
            order_no=self.order_no,
            type=claim_type,
            items=[
                ApplyAfterSaleItemIn(order_item_id=self.order_item_ids[index], quantity=quantity)
                for index, quantity in zip(item_indexes, qtys, strict=True)
            ],
            requested_amount=amount,
            reason="the item arrived damaged",
            client_request_id=self.client_request_id(suffix),
        )

    def file_claim(
        self,
        session: Session,
        *,
        amount: int,
        item_indexes: tuple[int, ...] = (0,),
        quantities: tuple[int, ...] | None = None,
        claim_type: str = AfterSaleType.REFUND_ONLY,
        suffix: str = "claim",
    ) -> AfterSale:
        """File a claim the way the customer endpoint does, and return the row."""
        return AfterSaleService(session).apply(
            principal=self.consumer,
            payload=self.claim_request(
                amount=amount,
                item_indexes=item_indexes,
                quantities=quantities,
                claim_type=claim_type,
                suffix=suffix,
            ),
            idempotency_key=self.key(suffix),
        )

    def approve(self, session: Session, claim: AfterSale, *, amount: int) -> AfterSale:
        """Approve through the service, so the state guards are exercised too."""
        return AfterSaleService(session).approve(
            principal=self.staff,
            after_sale_no=claim.after_sale_no,
            payload=ApproveAfterSaleRequest(approved_amount=amount),
        )

    def reject(self, session: Session, claim: AfterSale, *, reason: str) -> AfterSale:
        return AfterSaleService(session).reject(
            principal=self.staff,
            after_sale_no=claim.after_sale_no,
            payload=RejectAfterSaleRequest(reject_reason=reason),
        )

    def cancel(
        self, session: Session, claim: AfterSale, *, reason: str | None = None
    ) -> AfterSale:
        return AfterSaleService(session).cancel(
            principal=self.consumer,
            after_sale_no=claim.after_sale_no,
            payload=CancelAfterSaleRequest(reason=reason),
        )

    def refund(
        self,
        session: Session | None,
        claim: AfterSale,
        *,
        amount: int,
        suffix: str = "refund",
        reason: str | None = None,
        idempotency_key: str | None = None,
    ):
        """Execute a refund through ``RefundWorkflow`` with the console principal.

        ``session`` may be ``None``, and callers normally pass ``None``: the workflow reads the
        claim's live state from the database, so it must run on a **fresh** session. Handing it a
        session that already loaded the claim gives it a stale identity-map instance - after the
        workflow's own commit that instance is expired or outdated, and the state guard then
        judges a status that is no longer the row's. The parameter is kept so a test can pass an
        explicit session when it wants to hold a transaction open deliberately.
        """
        if session is None:
            with get_session_factory()() as fresh:
                return RefundWorkflow(fresh).execute(
                    principal=self.staff,
                    after_sale_no=claim.after_sale_no,
                    amount=amount,
                    reason=reason,
                    idempotency_key=idempotency_key or self.key(suffix),
                )
        return RefundWorkflow(session).execute(
            principal=self.staff,
            after_sale_no=claim.after_sale_no,
            amount=amount,
            reason=reason,
            idempotency_key=idempotency_key or self.key(suffix),
        )


@pytest.fixture
def _commerce_shop_instance(engine) -> Iterator[SeededShop]:
    """One instance of the **shared seed's** world for this module.

    Built by running the shared fixture's own body (``_commerce_shop``), not by re-deriving it
    and not by importing the fixture object: pytest registers a fixture only under the name it
    is *defined* with and only within the conftest chain of the requesting test, and a sibling
    package's conftest is not in this package's chain. So importing ``shop`` from
    ``tests/integration/commerce/conftest.py`` does not make it injectable here - which this
    module discovered the hard way, one failed ``fixture not found`` at a time.

    Driving the generator directly keeps the requirement that matters: there is exactly **one**
    definition of the Phase 5 shop, it lives in the shared seed, and this module does not
    duplicate any of it. The teardown is the shared seed's ``_purge`` as well, so nothing about
    the fixture's lifecycle is reimplemented - only its invocation is local.

    **Function-scoped, not module-scoped**, and that is deliberate: the seed's ``key()`` builds
    idempotency keys from its marker, so a world shared across tests hands the same key to
    different payloads and the second request is refused as a reuse. One world per test means
    one marker per test, which is what makes each test's keys its own - and it also means one
    test cannot see another's committed rows.
    """
    from tests.integration.commerce.seed import shop as shared_shop

    generator = shared_shop.__wrapped__(engine=engine)
    shop_instance = next(generator)
    try:
        yield shop_instance
    finally:
        for _ in generator:
            pass


@pytest.fixture
def seeded_shop(_commerce_shop_instance: SeededShop) -> Iterator[AfterSalesShop]:
    """The shared seed's world, plus a genuinely paid order, via the shared seed itself.

    This is the shape PHASE5_DESIGN section 12 asks for: one definition of the shop and one
    definition of a paid order, both in ``tests/integration/commerce/``, and this module depends
    on them rather than re-deriving anything.

    ``paid_order`` drives the **real** paths - ``CreateOrderWorkflow``, ``PaymentService.create``
    and ``PaymentSuccessWorkflow`` with a signed provider callback - so a refund test starts from
    a state the product actually produces: stock reserved and deducted, the
    ``PENDING_PAYMENT -> PROCESSING`` status log, the fulfillment shell and its items, and a
    ``PROCESSED`` callback row. Nothing about the paid state is hand-written.

    Because ``paid_order`` can only succeed if ``PaymentSuccessWorkflow`` creates its
    fulfillment shell, this fixture asserts the shell exists. That assertion is the guard: it
    fails loudly if the shell stops being created, instead of the suite quietly starting from a
    state the product cannot produce.

    Function-scoped, not module-scoped: the seed builds idempotency keys from its marker, so a
    world shared across tests would hand one key to different payloads and the second request
    would be refused as a reuse. One world per test means one marker per test.
    """
    _extend_staff_role(_commerce_shop_instance)

    factory = get_session_factory()
    order = paid_order(
        _commerce_shop_instance,
        lines=[
            OrderLineInput(sku_id=_commerce_shop_instance.sku_ids[index], quantity=LINE_QUANTITY)
            for index in range(3)
        ],
        suffix=f"as{uuid.uuid4().hex[:8]}",
    )

    with factory() as session:
        items = list(
            session.execute(
                select(OrderItem)
                .where(OrderItem.order_id == order.id)
                .order_by(OrderItem.id.asc())
            )
            .scalars()
            .all()
        )
        assert len(items) == 3, "the shared seed's three SKUs should give three lines"
        # The shell the payment workflow creates must exist, or the fixture is not producing the
        # state it claims to: asserted here rather than assumed, because that is exactly the
        # property that was broken.
        shell = (
            session.execute(select(Fulfillment).where(Fulfillment.order_id == order.id))
            .scalars()
            .first()
        )
        assert shell is not None, "PaymentSuccessWorkflow did not create the fulfillment shell"
        order_item_ids = tuple(int(item.id) for item in items)
        order_item_amounts = tuple(int(item.payable_amount) for item in items)

    yield AfterSalesShop(
        seeded=_commerce_shop_instance,
        order_no=order.order_no,
        order_id=int(order.id),
        order_item_ids=order_item_ids,
        order_item_amounts=order_item_amounts,
    )


@pytest.fixture(scope="module")
def session_factory(engine):
    """A session factory for the tests' own reads and writes.

    The shared seed owns ``engine``; this is the factory bound to it, exposed under the name this
    module uses. Most tests take a session per logical request - ``RefundWorkflow`` commits, so a
    reused session would hand back an object the workflow's own commit had already refreshed.
    """
    return get_session_factory()


# ---------------------------------------------------------------------------
# Read-back helpers - straight SQL, so a test sees what the database holds
# ---------------------------------------------------------------------------
def read_money(session: Session, *, order_no: str, payment_no: str | None = None) -> dict:
    """Every refund counter for one order, read with **SQL** rather than through the ORM.

    Fresh SQL, because HANDOFF section 6's REPEATABLE-READ warning applies: reading the
    post-mutation state through an object the transaction already loaded compares the snapshot
    against itself and passes as a green that means nothing.

    ``payment_no`` is accepted for call-site symmetry and ignored - the order's latest payment is
    what the money assertions need, and resolving it here keeps a test from having to know which
    attempt settled.
    """
    row = session.execute(
        text(
            "SELECT paid_amount, refunded_amount, order_status, payment_status, "
            "after_sale_status FROM orders WHERE order_no = :no"
        ),
        {"no": order_no},
    ).one()
    payment = session.execute(
        text(
            "SELECT paid_amount, refunded_amount, status FROM payments "
            "WHERE order_id = (SELECT id FROM orders WHERE order_no = :no) "
            "ORDER BY created_at DESC, id DESC LIMIT 1"
        ),
        {"no": order_no},
    ).one()
    lines = session.execute(
        text(
            "SELECT oi.id, oi.payable_amount, oi.refunded_amount FROM order_items oi "
            "JOIN orders o ON o.id = oi.order_id WHERE o.order_no = :no ORDER BY oi.id"
        ),
        {"no": order_no},
    ).all()
    _ = payment_no
    return {
        "order_paid": int(row[0]),
        "order_refunded": int(row[1]),
        "payment_paid": int(payment[0]),
        "payment_refunded": int(payment[1]),
        "line_refunded_total": sum(int(line[2]) for line in lines),
        "line_paid_total": sum(int(line[1]) for line in lines),
        "order_status": str(row[2]),
        "payment_status": str(row[3]),
        "after_sale_status": str(row[4]),
        "payment_record_status": str(payment[2]),
    }


def load_claim(session: Session, *, after_sale_no: str) -> AfterSale:
    claim = (
        session.execute(select(AfterSale).where(AfterSale.after_sale_no == after_sale_no))
        .scalars()
        .first()
    )
    assert claim is not None, f"claim {after_sale_no} disappeared"
    return claim


def read_claim(session: Session, *, after_sale_no: str) -> dict:
    """The claim's counters, again by SQL rather than through a loaded object."""
    row = session.execute(
        text(
            "SELECT requested_amount, approved_amount, refunded_amount, claim_status, "
            "completed_at FROM after_sales WHERE after_sale_no = :no"
        ),
        {"no": after_sale_no},
    ).one()
    return {
        "requested_amount": int(row[0]),
        "approved_amount": int(row[1]),
        "refunded_amount": int(row[2]),
        "claim_status": str(row[3]),
        "completed_at": row[4],
    }


def claim_items(session: Session, *, claim: AfterSale) -> list[AfterSaleItem]:
    return list(
        session.execute(
            select(AfterSaleItem)
            .where(AfterSaleItem.after_sale_id == claim.id)
            .order_by(AfterSaleItem.order_item_id.asc())
        )
        .scalars()
        .all()
    )


def refunds_for(session: Session, *, after_sale_id: int) -> list[Refund]:
    return list(
        session.execute(
            select(Refund).where(Refund.after_sale_id == after_sale_id).order_by(Refund.id.asc())
        )
        .scalars()
        .all()
    )


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


def available_stock(session: Session, *, sku_id: int, warehouse_id: int) -> int:
    return int(
        session.execute(
            text(
                "SELECT available_qty FROM inventories WHERE sku_id = :sku AND warehouse_id = :wh"
            ),
            {"sku": sku_id, "wh": warehouse_id},
        ).scalar_one()
    )


def as_utc(value):
    """MySQL hands back naive UTC for ``DATETIME``; compare knowingly."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
