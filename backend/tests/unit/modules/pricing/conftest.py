"""Fixtures for the pricing unit tests.

Pure logic only: no database, no clock, no settings. The pricing authority is
DB-free by design (PHASE4_DESIGN §1), so anything this suite cannot test without
infrastructure would be a design smell rather than a testing gap.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from app.modules.pricing import PricedLine, PricingService

LineFactory = Callable[..., PricedLine]


@pytest.fixture
def pricing() -> PricingService:
    """A pricing authority. Stateless, so a fresh one per test costs nothing."""
    return PricingService()


@pytest.fixture
def make_line() -> LineFactory:
    """Build a :class:`PricedLine` with sensible defaults.

    Defaults are deliberately *not* all equal to the sku id: a line whose price
    happens to track its id is how an accidental cross-wiring between price and
    identity survives a test suite.
    """

    def _make(
        sku_id: int = 1,
        unit_price: int = 1000,
        quantity: int = 1,
        *,
        product_id: int | None = None,
        product_name: str | None = None,
        sku_name: str | None = None,
        image_object_key: str | None = None,
        image_url: str | None = None,
        sku_snapshot: dict | None = None,
    ) -> PricedLine:
        return PricedLine(
            sku_id=sku_id,
            product_id=sku_id * 10 if product_id is None else product_id,
            product_name=f"Product {sku_id}" if product_name is None else product_name,
            sku_name=f"SKU {sku_id}" if sku_name is None else sku_name,
            image_object_key=image_object_key,
            image_url=image_url,
            sku_snapshot=sku_snapshot,
            unit_price=unit_price,
            quantity=quantity,
        )

    return _make
