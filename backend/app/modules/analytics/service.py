"""Read-only metric projections from committed commerce and inventory rows.

The reporting window is UTC and inclusive by date. Sales use the order's paid_at
timestamp, not creation time; refunds use the successful refund's completed_at.
GMV is gross collected money before refunds. Product performance is the sum of
paid order-item amounts, so shipping never appears in it. Refund rate is refund
money completed in the window divided by gross money collected in the same
window; it is a cash-flow ratio and can exceed one for older-order refunds.
Inventory turnover is units sold divided by average opening/closing on-hand
units. On-hand is reconstructed from the append-only movement ledger.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import PermissionDeniedError, ValidationError
from app.modules.aftersales.models import Refund
from app.modules.identity.enums import PermissionCode
from app.modules.identity.service import Principal
from app.modules.inventory.models import Inventory, InventoryMovement
from app.modules.order.models import Order, OrderItem

Metric = Literal[
    "sales.gmv", "sales.order_count", "inventory.turnover",
    "product.performance", "refund.rate",
]
Granularity = Literal["day", "week", "month"]
METRIC_UNITS: dict[str, str] = {
    "sales.gmv": "minor_currency",
    "sales.order_count": "count",
    "inventory.turnover": "ratio",
    "product.performance": "minor_currency",
    "refund.rate": "ratio",
}


def _at_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=UTC)


def _bucket(day: date, granularity: Granularity) -> str:
    if granularity == "month":
        return f"{day.year:04d}-{day.month:02d}"
    if granularity == "week":
        return (day - timedelta(days=day.weekday())).isoformat()
    return day.isoformat()


def _ratio(numerator: int | float, denominator: int | float) -> float:
    if denominator <= 0:
        return 0.0
    return float(round(Decimal(str(numerator)) / Decimal(str(denominator)), 6))


class AnalyticsService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _paid(self, merchant_id: int, start: datetime, end: datetime) -> list[tuple[date, int, int]]:
        rows = self.session.execute(
            select(Order.paid_at, Order.paid_amount, Order.id).where(
                Order.merchant_id == merchant_id,
                Order.paid_at >= start,
                Order.paid_at < end,
                Order.paid_amount > 0,
            )
        ).all()
        return [(paid_at.date(), int(amount), int(order_id)) for paid_at, amount, order_id in rows]

    def _items(
        self, merchant_id: int, start: datetime, end: datetime,
    ) -> list[tuple[date, int, int, int, str]]:
        rows = self.session.execute(
            select(Order.paid_at, OrderItem.payable_amount, OrderItem.quantity,
                   OrderItem.sku_id, OrderItem.sku_name)
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.merchant_id == merchant_id, Order.paid_at >= start, Order.paid_at < end)
        ).all()
        return [
            (paid_at.date(), int(amount), int(quantity), int(sku_id), sku_name)
            for paid_at, amount, quantity, sku_id, sku_name in rows
        ]

    def _refunds(self, merchant_id: int, start: datetime, end: datetime) -> list[tuple[date, int]]:
        rows = self.session.execute(
            select(Refund.completed_at, Refund.amount).where(
                Refund.merchant_id == merchant_id,
                Refund.status == "SUCCEEDED",
                Refund.completed_at >= start,
                Refund.completed_at < end,
            )
        ).all()
        return [(completed_at.date(), int(amount)) for completed_at, amount in rows]

    def _inventory_positions(
        self, merchant_id: int, start: datetime,
    ) -> list[tuple[Inventory, list[InventoryMovement]]]:
        positions = list(self.session.execute(
            select(Inventory).where(Inventory.merchant_id == merchant_id)
        ).scalars())
        if not positions:
            return []
        sku_ids = {row.sku_id for row in positions}
        warehouse_ids = {row.warehouse_id for row in positions}
        movements = self.session.execute(
            select(InventoryMovement).where(
                InventoryMovement.sku_id.in_(sku_ids),
                InventoryMovement.warehouse_id.in_(warehouse_ids),
                InventoryMovement.created_at >= start,
            ).order_by(InventoryMovement.created_at, InventoryMovement.id)
        ).scalars()
        by_position: dict[tuple[int, int], list[InventoryMovement]] = defaultdict(list)
        for movement in movements:
            by_position[(movement.warehouse_id, movement.sku_id)].append(movement)
        return [
            (row, by_position[(row.warehouse_id, row.sku_id)]) for row in positions
        ]

    @staticmethod
    def _on_hand(
        positions: list[tuple[Inventory, list[InventoryMovement]]], at: datetime,
    ) -> int:
        total = 0
        for row, movements in positions:
            if row.created_at >= at:
                continue
            next_movement = next((move for move in movements if move.created_at >= at), None)
            if next_movement:
                total += next_movement.before_available + next_movement.before_locked
            else:
                total += row.available_qty + row.locked_qty
        return total

    def _turnover(
        self, merchant_id: int, start: datetime, end: datetime,
        granularity: Granularity,
    ) -> tuple[dict[str, float], float]:
        items = self._items(merchant_id, start, end)
        sold: dict[str, int] = defaultdict(int)
        for paid_day, _, quantity, _, _ in items:
            sold[_bucket(paid_day, granularity)] += quantity
        if not sold:
            return {}, 0.0
        positions = self._inventory_positions(merchant_id, start)
        result: dict[str, float] = {}
        for key, quantity in sold.items():
            if granularity == "month":
                year, month = map(int, key.split("-"))
                bucket_start = date(year, month, 1)
                bucket_end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            else:
                bucket_start = date.fromisoformat(key)
                bucket_end = bucket_start + timedelta(days=7 if granularity == "week" else 1)
            opening = self._on_hand(positions, max(_at_start(bucket_start), start))
            closing = self._on_hand(positions, min(_at_start(bucket_end), end))
            result[key] = _ratio(quantity * 2, opening + closing)
        overall_open = self._on_hand(positions, start)
        overall_close = self._on_hand(positions, end)
        overall = _ratio(sum(sold.values()) * 2, overall_open + overall_close)
        return result, overall

    def metric(
        self, *, principal: Principal, metric: Metric, from_day: date,
        to_day: date, granularity: Granularity,
    ) -> dict:
        principal.require_permission(PermissionCode.ANALYTICS_READ.value)
        if not principal.is_staff or principal.merchant_id is None:
            raise PermissionDeniedError("merchant staff account required")
        if from_day > to_day or (to_day - from_day).days > 365:
            raise ValidationError("analytics date range must contain 1 to 366 days")
        merchant_id = principal.merchant_id
        start, end = _at_start(from_day), _at_start(to_day + timedelta(days=1))
        span = end - start
        previous_start = start - span
        current_paid = self._paid(merchant_id, start, end)
        previous_paid = self._paid(merchant_id, previous_start, start)
        values: dict[str, int | float] = defaultdict(int)
        dimensions: list[dict[str, str]] = []
        if metric == "sales.gmv":
            for day, amount, _ in current_paid:
                values[_bucket(day, granularity)] += amount
            total = sum(amount for _, amount, _ in current_paid)
            previous = sum(amount for _, amount, _ in previous_paid)
        elif metric == "sales.order_count":
            for day, _, _ in current_paid:
                values[_bucket(day, granularity)] += 1
            total, previous = len(current_paid), len(previous_paid)
        elif metric == "product.performance":
            items = self._items(merchant_id, start, end)
            by_sku: dict[tuple[int, str], int] = defaultdict(int)
            for day, amount, _, sku_id, sku_name in items:
                values[_bucket(day, granularity)] += amount
                by_sku[(sku_id, sku_name)] += amount
            if by_sku:
                top = max(by_sku, key=lambda sku: (by_sku[sku], -sku[0]))
                dimensions = [{"key": "sku_id", "label": "Top SKU", "value": f"{top[1]} (#{top[0]})"}]
            total = sum(amount for _, amount, _, _, _ in items)
            previous = sum(amount for _, amount, _, _, _ in self._items(merchant_id, previous_start, start))
        elif metric == "refund.rate":
            refunded: dict[str, int] = defaultdict(int)
            collected: dict[str, int] = defaultdict(int)
            for day, amount in self._refunds(merchant_id, start, end):
                refunded[_bucket(day, granularity)] += amount
            for day, amount, _ in current_paid:
                collected[_bucket(day, granularity)] += amount
            for key in refunded.keys() | collected.keys():
                values[key] = _ratio(refunded[key], collected[key])
            total = _ratio(sum(refunded.values()), sum(collected.values()))
            dimensions = [
                {"key": "refunded_amount", "label": "Refunded minor units", "value": str(sum(refunded.values()))},
                {"key": "collected_amount", "label": "Collected minor units", "value": str(sum(collected.values()))},
            ]
            previous_refunded = sum(amount for _, amount in self._refunds(merchant_id, previous_start, start))
            previous = _ratio(previous_refunded, sum(amount for _, amount, _ in previous_paid))
        else:
            values, total = self._turnover(merchant_id, start, end, granularity)
            _, previous = self._turnover(merchant_id, previous_start, start, granularity)
        series = [{"bucket": key, "value": value} for key, value in sorted(values.items())]
        average = sum(values.values()) / len(values) if values else 0
        if METRIC_UNITS[metric] == "minor_currency":
            average = round(average)
        change_ratio = _ratio(total - previous, previous) if previous else 0.0
        return {
            "metric": metric,
            "unit": METRIC_UNITS[metric],
            "period": {"from": from_day.isoformat(), "to": to_day.isoformat(), "granularity": granularity},
            "series": series,
            "summary": {"total": total, "average": average, "change_ratio": change_ratio},
            "dimensions": dimensions,
        }
