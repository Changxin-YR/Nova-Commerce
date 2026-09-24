"""Refund-rate numerator follows a real successful refund workflow."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.core.errors import PermissionDeniedError
from app.modules.analytics.service import AnalyticsService
from app.modules.identity.enums import PermissionCode
from app.shared.db.session import get_session_factory

from .conftest import AfterSalesShop

pytestmark = pytest.mark.integration


def test_refund_rate_uses_successful_refund_money(seeded_shop: AfterSalesShop) -> None:
    with get_session_factory()() as session:
        claim = seeded_shop.file_claim(session, amount=500, suffix="analytics-refund")
        session.commit()
    with get_session_factory()() as session:
        seeded_shop.approve(session, claim, amount=500)
        session.commit()
    refund = seeded_shop.refund(None, claim, amount=500, suffix="analytics-refund")
    assert refund.refund.amount == 500

    today = datetime.now(UTC).date()
    with get_session_factory()() as session:
        with pytest.raises(PermissionDeniedError):
            AnalyticsService(session).metric(
                principal=seeded_shop.staff, metric="refund.rate",
                from_day=today, to_day=today, granularity="day",
            )
        principal = replace(
            seeded_shop.staff,
            permissions=seeded_shop.staff.permissions | {PermissionCode.ANALYTICS_READ.value},
        )
        report = AnalyticsService(session).metric(
            principal=principal, metric="refund.rate",
            from_day=today, to_day=today, granularity="day",
        )
    assert report["unit"] == "ratio"
    assert report["summary"]["total"] == pytest.approx(500 / seeded_shop.paid_amount, abs=1e-6)
    assert report["series"] == [{"bucket": today.isoformat(), "value": report["summary"]["total"]}]
