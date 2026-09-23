"""Merchant-scoped promotion list for the console."""

from __future__ import annotations

from math import ceil
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.identity.dependencies import ConsolePrincipal
from app.modules.marketing.api.promotions import _data
from app.modules.marketing.service import PromotionService
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/admin/promotions", summary="List this merchant's promotions")
def list_admin_promotions(
    principal: ConsolePrincipal,
    session: SessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    rows, total = PromotionService(session).list_admin(principal=principal, page=page, page_size=page_size)
    return envelope(
        data={
            "items": [_data(row) for row in rows],
            "meta": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": ceil(total / page_size),
            },
        }
    )
