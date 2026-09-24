"""Frozen metric envelope for merchant analytics."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.analytics.service import AnalyticsService, Granularity, Metric
from app.modules.identity.dependencies import ConsolePrincipal
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/admin/metrics/{metric}", summary="Read a merchant metric")
def read_metric(
    metric: Metric,
    principal: ConsolePrincipal,
    session: SessionDep,
    from_day: Annotated[date | None, Query(alias="from")] = None,
    to_day: Annotated[date | None, Query(alias="to")] = None,
    granularity: Granularity = "day",
) -> dict:
    end = to_day or datetime.now(UTC).date()
    start = from_day or end - timedelta(days=29)
    data = AnalyticsService(session).metric(
        principal=principal, metric=metric, from_day=start, to_day=end,
        granularity=granularity,
    )
    return envelope(data=data)
