"""Test-residue detection and sweeping, in one place.

Design section 13.5. A test count is only meaningful together with the state of the
database it was measured on, because **a failing test skips its teardown**: left-over
rows then change later tests - an orphaned default warehouse shadows merchant-scoped
resolution - and the next run reports failures that exist nowhere but in that residue.
That has already produced one convincing false regression: six fulfilment failures
where shipping worked but ``fulfillment_status`` stayed ``UNFULFILLED``.

Two entry points, and the split is the point:

* :func:`report_residue` - **read-only**. Returns per-table counts, so a measurement can
  record the state it was taken on. Safe on any database, including a demo one. This is
  the half that makes evidence honest.
* :func:`purge_test_residue` - **destructive and opt-in** (``dry_run=True`` by default).
  Deletes only rows belonging to recognised fixture markers, in FK-safe order.

## Why this is not "delete everything that looks like a test"

The dangerous version of this tool truncates tables, or deletes any row whose name
matches a loose pattern somewhere in the string. Both are one typo from emptying a
database somebody cares about. So the sweep here is **marker-rooted**:

1. find the **merchants** and **users** the fixtures created, by the exact marker shapes
   the shared seed and the gate really produce;
2. resolve every other table through those ids - never through a name pattern;
3. delete children before parents, because almost every FK in this phase is RESTRICT
   (deliberately, so history cannot vanish by accident - and so that it cannot be
   swept accidentally either).

A row this tool cannot attribute is a row this tool does not touch. In particular
``payment_callbacks`` is matched by the **event ids the fixtures generate**, never by
``provider = 'MOCK'``: MOCK is a real provider that dev and demo data uses, so matching
on it would delete rows this tool has no right to claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.orm import Session

from app.modules.aftersales.models import AfterSale, AfterSaleItem, Refund
from app.modules.catalog.models import Product, ProductImage, ProductSku
from app.modules.fulfillment.models import Fulfillment, FulfillmentItem
from app.modules.identity.models import Merchant, User, UserAddress
from app.modules.inventory.models import Inventory, InventoryMovement, Warehouse
from app.modules.order.models import Order, OrderItem, OrderStatusLog
from app.modules.payment.models import Payment, PaymentCallback
from app.shared.db.models.idempotency import IdempotencyRecord

__all__ = ["MARKER_PATTERNS", "ResidueReport", "purge_test_residue", "report_residue"]

#: The marker shapes the fixtures actually produce, each anchored.
#:
#: * ``M<8 hex>`` - the shared seed and the order/after-sales fixtures build the
#:   merchant code as ``f"M{marker}"[:24]`` from ``uuid4().hex[:8]``.
#: * ``FG11<hex>`` - the FG-11 concurrency gate's own merchant and accounts.
#: * ``(fg11_)?buyer_<marker>`` / ``(fg11_)?staff_<marker>`` - those fixtures' accounts.
#:   Matched by name because a consumer has ``merchant_id IS NULL``, so no merchant id
#:   can reach them.
MARKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("merchant", re.compile(r"^M([0-9a-f]{8})$")),
    ("merchant", re.compile(r"^FG11[0-9A-F]{8,}$", re.IGNORECASE)),
    ("user", re.compile(r"^(?:fg11_)?buyer_[0-9a-fA-F]{8,}$")),
    ("user", re.compile(r"^(?:fg11_)?staff_[0-9a-fA-F]{8,}$")),
)

#: Event-id shapes the fixtures generate: ``evt-<marker>-<suffix>`` from the shared
#: seed, ``FG11...`` from the gate.
#:
#: An earlier version of this comment claimed these were "anchored so a real provider's
#: event id cannot match". That was **false for ``evt-%``**, which matches any event id
#: beginning ``evt-`` - including a real provider's or a naming convention this project
#: does not own. ``FG11%``/``fg11%`` genuinely are anchored to fixture namespaces; the
#: bare prefix is not. The comment was worse than no comment, because it gave a reader
#: confidence in exactly the direction that mattered. Reported by payment-workflow, who
#: measured it rather than taking the description.
#:
#: The prefix is kept as the *discovery* filter - it is how rows the fixtures plausibly
#: produced are found at all - but nothing is deleted on it alone: every sweep below is
#: additionally scoped to ids the report already attributed to a marker, or to an order
#: that no longer exists. Two conditions, so a provider's real delivery cannot be removed
#: by shape alone.
_EVENT_ID_PATTERNS = ("evt-%", "FG11%", "fg11%")


@dataclass(slots=True)
class ResidueReport:
    """What residue exists. Building one never mutates anything."""

    counts: dict[str, int] = field(default_factory=dict)
    merchants: list[tuple[int, str]] = field(default_factory=list)
    users: list[tuple[int, str]] = field(default_factory=list)
    #: Rows that *look* like test output (marker-shaped event ids) but that no marker
    #: root reaches, so this tool cannot prove they are residue. Reported, never swept:
    #: they are inert because each run generates a fresh random marker and therefore
    #: never reuses a leftover event id.
    unattributable_callbacks: int = 0
    #: Event-id-shaped callbacks whose named order no longer exists - pre-existing
    #: accumulation reachable only by the explicit opt-in sweep, never by a
    #: marker-scoped purge, because their markers are gone.
    orphaned_callbacks: int = 0

    @property
    def total_residue(self) -> int:
        return sum(self.counts.values())

    @property
    def is_clean(self) -> bool:
        return self.total_residue == 0

    def line(self) -> str:
        """One line, for embedding in a measurement record."""
        bits = []
        if self.unattributable_callbacks:
            bits.append(
                f"unattributable event-shaped callbacks: {self.unattributable_callbacks} (inert)"
            )
        if self.orphaned_callbacks:
            bits.append(f"orphaned fixture callbacks (order gone): {self.orphaned_callbacks}")
        extra = ("; " + "; ".join(bits)) if bits else ""
        if self.is_clean:
            return f"residue: 0 (no rows attributable to a fixture marker){extra}"
        parts = ", ".join(f"{t}={n}" for t, n in sorted(self.counts.items()))
        return f"residue: {self.total_residue} ({parts}){extra}"

    def render(self) -> str:
        lines = [self.line(), ""]
        lines.append(f"merchants recognised: {self.merchants or 'none'}")
        lines.append(f"users recognised    : {self.users or 'none'}")
        return "\n".join(lines)


def _is_marker(kind: str, value: str) -> bool:
    return any(k == kind and p.match(value) for k, p in MARKER_PATTERNS)


def _marker_like_patterns(report: ResidueReport) -> list[str]:
    """``LIKE`` patterns for the two tables that have no id to join on.

    ``idempotency_records`` is keyed by a marker-prefixed string and
    ``payment_callbacks`` by a marker-shaped event id; neither carries an owner column.
    """
    patterns = []
    for _, code in report.merchants:
        patterns += [f"{code}%", f"%{code}%"]
    for _, name in report.users:
        marker = name.split("_")[-1]
        patterns += [f"{name}%", f"%{marker}%"]
    return sorted(set(patterns))


def _roots(session: Session) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    merchants = [
        (int(mid), str(code))
        for mid, code in session.execute(select(Merchant.id, Merchant.code)).all()
        if _is_marker("merchant", str(code))
    ]
    users = [
        (int(uid), str(name))
        for uid, name in session.execute(select(User.id, User.username)).all()
        if _is_marker("user", str(name))
    ]
    return merchants, users


def _scoped_ids(session: Session, merchant_ids: list[int], user_ids: list[int]) -> dict[str, list[int]]:
    """Every id the sweep may touch, resolved **only** from the marker roots."""
    m = merchant_ids
    ids: dict[str, list[int]] = {
        "orders": [
            int(r[0])
            for r in session.execute(
                select(Order.id).where(
                    or_(Order.merchant_id.in_(m), Order.user_id.in_(user_ids))
                )
            ).all()
        ],
        "claims": [
            int(r[0]) for r in session.execute(select(AfterSale.id).where(AfterSale.merchant_id.in_(m))).all()
        ],
        "payments": [
            int(r[0]) for r in session.execute(select(Payment.id).where(Payment.merchant_id.in_(m))).all()
        ],
        "warehouses": [
            int(r[0]) for r in session.execute(select(Warehouse.id).where(Warehouse.merchant_id.in_(m))).all()
        ],
        "products": [
            int(r[0]) for r in session.execute(select(Product.id).where(Product.merchant_id.in_(m))).all()
        ],
        "skus": [
            int(r[0]) for r in session.execute(select(ProductSku.id).where(ProductSku.merchant_id.in_(m))).all()
        ],
    }
    return ids


def _orphaned_callback_rows(session: Session) -> list[tuple[str, str]]:
    """Fixture-shaped callbacks whose named order no longer exists."""
    rows = list(
        session.execute(
            select(PaymentCallback.provider_event_id, PaymentCallback.order_no).where(
                or_(*[PaymentCallback.provider_event_id.like(pat) for pat in _EVENT_ID_PATTERNS]),
                PaymentCallback.order_no.is_not(None),
            )
        ).all()
    )
    if not rows:
        return []
    named = {str(r[1]) for r in rows}
    live = {
        str(r[0])
        for r in session.execute(select(Order.order_no).where(Order.order_no.in_(named))).all()
    }
    return [(str(r[0]), str(r[1])) for r in rows if str(r[1]) not in live]


def _count_orphaned_callbacks(session: Session) -> int:
    return len(_orphaned_callback_rows(session))


def report_residue(session: Session) -> ResidueReport:
    """Count residue. Read-only - safe to call against any database."""
    report = ResidueReport()
    report.merchants, report.users = _roots(session)
    if not report.merchants and not report.users:
        report.orphaned_callbacks = _count_orphaned_callbacks(session)
        return report

    merchant_ids = [mid for mid, _ in report.merchants]
    user_ids = [uid for uid, _ in report.users]
    ids = _scoped_ids(session, merchant_ids, user_ids)

    def count(model, clause) -> int:
        return int(
            session.execute(select(func.count()).select_from(model).where(clause)).scalar_one()
        )

    orders = ids["orders"]
    claims = ids["claims"]
    payments = ids["payments"]
    products = ids["products"]
    skus = ids["skus"]
    warehouses = ids["warehouses"]

    report.counts["refunds"] = count(
        Refund,
        or_(Refund.order_id.in_(orders), Refund.after_sale_id.in_(claims), Refund.payment_id.in_(payments)),
    )
    report.counts["after_sale_items"] = count(AfterSaleItem, AfterSaleItem.after_sale_id.in_(claims))
    report.counts["after_sales"] = count(AfterSale, AfterSale.id.in_(claims))
    report.counts["fulfillment_items"] = count(
        FulfillmentItem,
        FulfillmentItem.fulfillment_id.in_(
            select(Fulfillment.id).where(Fulfillment.order_id.in_(orders))
        ),
    )
    report.counts["fulfillments"] = count(Fulfillment, Fulfillment.order_id.in_(orders))
    report.counts["payments"] = count(Payment, Payment.id.in_(payments))
    # Attribute the event-shaped callbacks exactly once. Those a marker reaches are
    # residue and are swept; the rest are reported separately (see below) and never
    # touched, because this tool cannot prove they are test output.
    markers = {code for _, code in report.merchants} | {
        name.split("_")[-1] for _, name in report.users
    }
    event_ids = [
        str(r[0])
        for r in session.execute(
            select(PaymentCallback.provider_event_id).where(
                or_(*[PaymentCallback.provider_event_id.like(p) for p in _EVENT_ID_PATTERNS])
            )
        ).all()
    ]
    attributed = [eid for eid in event_ids if any(m and m in eid for m in markers)]
    report.unattributable_callbacks = len(event_ids) - len(attributed)
    report.counts["payment_callbacks"] = len(attributed)
    report.counts["order_status_logs"] = count(OrderStatusLog, OrderStatusLog.order_id.in_(orders))
    report.counts["order_items"] = count(OrderItem, OrderItem.order_id.in_(orders))
    report.counts["orders"] = count(Order, Order.id.in_(orders))
    report.counts["inventory_movements"] = count(InventoryMovement, InventoryMovement.sku_id.in_(skus))
    report.counts["inventories"] = count(
        Inventory,
        or_(Inventory.warehouse_id.in_(warehouses), Inventory.sku_id.in_(skus)),
    )
    report.counts["product_skus"] = count(ProductSku, ProductSku.id.in_(skus))
    report.counts["product_images"] = count(ProductImage, ProductImage.product_id.in_(products))
    report.counts["products"] = count(Product, Product.id.in_(products))
    report.counts["warehouses"] = count(Warehouse, Warehouse.id.in_(warehouses))
    report.counts["user_addresses"] = count(UserAddress, UserAddress.user_id.in_(user_ids))

    patterns = _marker_like_patterns(report)
    if patterns:
        report.counts["idempotency_records"] = count(
            IdempotencyRecord,
            or_(*[IdempotencyRecord.idempotency_key.like(p) for p in patterns]),
        )

    # Orphaned fixture callbacks: event-id shaped, and the order they name no longer
    # exists. This is the accumulation `purge_shop` could not reach while its LIKE
    # pattern failed to match its own event ids, so their markers are long gone and no
    # live fixture can attribute them. Counted separately, and swept only on request.
    if _orphaned_callback_rows(session):
        report.orphaned_callbacks = len(_orphaned_callback_rows(session))

    report.counts = {k: v for k, v in report.counts.items() if v}
    return report


def purge_test_residue(session: Session, *, dry_run: bool = True) -> ResidueReport:
    """Delete recognised residue, children first. ``dry_run`` defaults to **True**.

    A maintenance sweep rather than application data access, which is why it is allowed
    to reach the two association tables (``user_roles``, ``role_permissions``) that have
    no ORM models - and why every other statement goes through the ORM so the FK graph
    stays the single description of deletion order.
    """
    report = report_residue(session)
    if dry_run or report.is_clean:
        return report

    merchant_ids = [mid for mid, _ in report.merchants]
    user_ids = [uid for uid, _ in report.users]
    ids = _scoped_ids(session, merchant_ids, user_ids)
    orders = ids["orders"]
    claims = ids["claims"]
    payments = ids["payments"]
    products = ids["products"]
    skus = ids["skus"]
    warehouses = ids["warehouses"]
    patterns = _marker_like_patterns(report)

    # --- children before parents -------------------------------------------
    session.execute(delete(Refund).where(Refund.after_sale_id.in_(claims)))
    session.execute(delete(Refund).where(Refund.payment_id.in_(payments)))
    session.execute(delete(Refund).where(Refund.order_id.in_(orders)))
    session.execute(delete(AfterSaleItem).where(AfterSaleItem.after_sale_id.in_(claims)))
    session.execute(delete(AfterSale).where(AfterSale.id.in_(claims)))
    session.execute(
        delete(FulfillmentItem).where(
            FulfillmentItem.fulfillment_id.in_(select(Fulfillment.id).where(Fulfillment.order_id.in_(orders)))
        )
    )
    session.execute(delete(Fulfillment).where(Fulfillment.order_id.in_(orders)))
    session.execute(delete(Payment).where(Payment.id.in_(payments)))
    # Deleted by the **marker-derived** patterns, not by `_EVENT_ID_PATTERNS`, so the set
    # swept is exactly the set the report attributed - by construction rather than by
    # coincidence. The broad prefix is discovery only; deletion requires attribution.
    if patterns:
        session.execute(
            delete(PaymentCallback).where(
                or_(*[PaymentCallback.provider_event_id.like(p) for p in patterns])
            )
        )
    session.execute(delete(OrderStatusLog).where(OrderStatusLog.order_id.in_(orders)))
    session.execute(delete(OrderItem).where(OrderItem.order_id.in_(orders)))
    session.execute(delete(Order).where(Order.id.in_(orders)))
    if patterns:
        session.execute(
            delete(IdempotencyRecord).where(
                or_(*[IdempotencyRecord.idempotency_key.like(p) for p in patterns])
            )
        )
    session.execute(delete(InventoryMovement).where(InventoryMovement.sku_id.in_(skus)))
    session.execute(
        delete(Inventory).where(
            or_(Inventory.warehouse_id.in_(warehouses), Inventory.sku_id.in_(skus))
        )
    )
    # Lines that reference a fixture SKU must go before the SKU itself: both
    # `order_items.sku_id` and `fulfillment_items.sku_id` are RESTRICT. Scoping by SKU
    # is safe - an order line pointing at a fixture SKU cannot belong to a real order.
    session.execute(delete(FulfillmentItem).where(FulfillmentItem.sku_id.in_(skus)))
    session.execute(delete(OrderItem).where(OrderItem.sku_id.in_(skus)))
    session.execute(delete(ProductSku).where(ProductSku.id.in_(skus)))
    session.execute(delete(ProductImage).where(ProductImage.product_id.in_(products)))
    session.execute(delete(Product).where(Product.id.in_(products)))
    session.execute(delete(UserAddress).where(UserAddress.user_id.in_(user_ids)))

    # --- identity last -----------------------------------------------------
    for mid in merchant_ids:
        session.execute(
            text("DELETE FROM user_roles WHERE role_id IN (SELECT id FROM roles WHERE merchant_id = :m)"),
            {"m": mid},
        )
        session.execute(
            text(
                "DELETE FROM role_permissions WHERE role_id IN "
                "(SELECT id FROM roles WHERE merchant_id = :m)"
            ),
            {"m": mid},
        )
        session.execute(text("DELETE FROM roles WHERE merchant_id = :m"), {"m": mid})
    owned_user_ids = [
        int(r[0])
        for r in session.execute(
            select(User.id).where(User.merchant_id.in_(merchant_ids))
        ).all()
    ]
    for uid in sorted(set(user_ids) | set(owned_user_ids)):
        session.execute(text("DELETE FROM user_roles WHERE user_id = :u"), {"u": uid})
        session.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
    session.execute(delete(Warehouse).where(Warehouse.id.in_(warehouses)))
    session.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids)))

    session.commit()
    return report


def purge_orphaned_fixture_callbacks(session: Session, *, dry_run: bool = True) -> int:
    """Delete fixture-shaped callbacks whose named order no longer exists.

    Deliberately **separate** from :func:`purge_test_residue`, and opt-in, because its
    attribution is weaker: it relies on the order being gone rather than on a live marker.

    Two conditions must hold together - the event id has a fixture shape, **and** the row's
    ``order_no`` matches no ``orders`` row.

    Why that is safe enough to offer: a ``payment_callbacks`` row is *kept* when its
    delivery cannot be resolved (design 5.2, which is why ``order_no`` may be NULL), so
    absence of a payment proves nothing and is deliberately not part of the test. Absence
    of the **order** is the stronger signal - these rows were left by test runs whose whole
    fixture graph has been tidied, and an incident review reading them would otherwise be
    reading test fixtures instead of real deliveries.

    Returns the number of rows deleted.
    """
    report = report_residue(session)
    if dry_run or report.orphaned_callbacks == 0:
        return 0

    rows = list(
        session.execute(
            select(PaymentCallback.provider_event_id, PaymentCallback.order_no).where(
                or_(*[PaymentCallback.provider_event_id.like(pat) for pat in _EVENT_ID_PATTERNS]),
                PaymentCallback.order_no.is_not(None),
            )
        ).all()
    )
    named = {str(r[1]) for r in rows}
    live = {
        str(r[0])
        for r in session.execute(select(Order.order_no).where(Order.order_no.in_(named))).all()
    }
    doomed = [str(r[0]) for r in rows if str(r[1]) not in live]
    if not doomed:
        return 0
    session.execute(delete(PaymentCallback).where(PaymentCallback.provider_event_id.in_(doomed)))
    session.commit()
    return len(doomed)
