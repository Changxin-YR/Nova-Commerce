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
#: seed, ``FG11...`` from the gate. Anchored so a real provider's event id cannot match.
_EVENT_ID_PATTERNS = ("evt-%", "FG11%", "fg11%")


@dataclass(slots=True)
class ResidueReport:
    """What residue exists. Building one never mutates anything."""

    counts: dict[str, int] = field(default_factory=dict)
    merchants: list[tuple[int, str]] = field(default_factory=list)
    users: list[tuple[int, str]] = field(default_factory=list)

    @property
    def total_residue(self) -> int:
        return sum(self.counts.values())

    @property
    def is_clean(self) -> bool:
        return self.total_residue == 0

    def line(self) -> str:
        """One line, for embedding in a measurement record."""
        if self.is_clean:
            return "residue: 0 (no rows attributable to a fixture marker)"
        parts = ", ".join(f"{t}={n}" for t, n in sorted(self.counts.items()))
        return f"residue: {self.total_residue} ({parts})"

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
    m = merchant_ids or [-1]
    ids: dict[str, list[int]] = {
        "orders": [
            int(r[0])
            for r in session.execute(
                select(Order.id).where(
                    or_(Order.merchant_id.in_(m), Order.user_id.in_(user_ids or [-1]))
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


def report_residue(session: Session) -> ResidueReport:
    """Count residue. Read-only - safe to call against any database."""
    report = ResidueReport()
    report.merchants, report.users = _roots(session)
    if not report.merchants and not report.users:
        return report

    merchant_ids = [mid for mid, _ in report.merchants]
    user_ids = [uid for uid, _ in report.users]
    ids = _scoped_ids(session, merchant_ids, user_ids)

    def count(model, clause) -> int:
        return int(
            session.execute(select(func.count()).select_from(model).where(clause)).scalar_one()
        )

    orders = ids["orders"] or [-1]
    claims = ids["claims"] or [-1]
    payments = ids["payments"] or [-1]
    products = ids["products"] or [-1]
    skus = ids["skus"] or [-1]
    warehouses = ids["warehouses"] or [-1]

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
    report.counts["payment_callbacks"] = count(
        PaymentCallback,
        or_(*[PaymentCallback.provider_event_id.like(p) for p in _EVENT_ID_PATTERNS]),
    )
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
    report.counts["user_addresses"] = count(UserAddress, UserAddress.user_id.in_(user_ids or [-1]))

    patterns = _marker_like_patterns(report)
    if patterns:
        report.counts["idempotency_records"] = count(
            IdempotencyRecord,
            or_(*[IdempotencyRecord.idempotency_key.like(p) for p in patterns]),
        )

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
    orders = ids["orders"] or [-1]
    claims = ids["claims"] or [-1]
    payments = ids["payments"] or [-1]
    products = ids["products"] or [-1]
    skus = ids["skus"] or [-1]
    warehouses = ids["warehouses"] or [-1]
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
    session.execute(
        delete(PaymentCallback).where(
            or_(*[PaymentCallback.provider_event_id.like(p) for p in _EVENT_ID_PATTERNS])
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
    session.execute(delete(ProductSku).where(ProductSku.id.in_(skus)))
    session.execute(delete(ProductImage).where(ProductImage.product_id.in_(products)))
    session.execute(delete(Product).where(Product.id.in_(products)))
    session.execute(delete(UserAddress).where(UserAddress.user_id.in_(user_ids or [-1])))

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
    for uid in user_ids:
        session.execute(text("DELETE FROM user_roles WHERE user_id = :u"), {"u": uid})
        session.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
    session.execute(delete(Warehouse).where(Warehouse.id.in_(warehouses)))
    session.execute(delete(Merchant).where(Merchant.id.in_(merchant_ids or [-1])))

    session.commit()
    return report
