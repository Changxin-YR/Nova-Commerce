"""Task endpoints for previewing, creating and publishing promotions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.identity.dependencies import ConsolePrincipal
from app.modules.marketing.models import Promotion
from app.modules.marketing.schemas import PromotionCreate, PromotionDraft
from app.modules.marketing.service import PromotionService
from app.shared.db.base import utc_now
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


def _data(promotion: Promotion) -> dict:
    return {
        "id": promotion.id,
        "promotion_no": promotion.promotion_no,
        "merchant_id": promotion.merchant_id,
        "name": promotion.name,
        "description": promotion.description,
        "promotion_type": promotion.promotion_type,
        "status": promotion.status,
        "priority": promotion.priority,
        "stackable": promotion.stackable,
        "rule_config": promotion.rule_config,
        "scope": promotion.scope,
        "starts_at": promotion.starts_at.isoformat(),
        "ends_at": promotion.ends_at.isoformat(),
        "total_quota": promotion.total_quota,
        "used_quota": promotion.used_quota,
        "created_at": promotion.created_at.isoformat(),
        "updated_at": promotion.updated_at.isoformat(),
    }


@router.get("/promotions", summary="List currently applicable storefront promotions")
def active_promotions(session: SessionDep) -> dict:
    now = utc_now()
    rows = session.execute(
        select(Promotion)
        .where(
            Promotion.status == "ACTIVE",
            Promotion.starts_at <= now,
            Promotion.ends_at > now,
            Promotion.used_quota < Promotion.total_quota,
        )
        .order_by(Promotion.priority.desc(), Promotion.id)
    ).scalars()
    return envelope(data=[_data(row) for row in rows])


@router.post("/promotions/preview", summary="Preview a promotion before creation")
def preview_promotion(payload: PromotionDraft, principal: ConsolePrincipal, session: SessionDep) -> dict:
    return envelope(data=PromotionService(session).preview(principal=principal, draft=payload))


@router.post("/promotions", summary="Create a promotion from a preview token")
def create_promotion(payload: PromotionCreate, principal: ConsolePrincipal, session: SessionDep) -> dict:
    row = PromotionService(session).create(principal=principal, payload=payload)
    return envelope(data=_data(row))


@router.post("/promotions/{promotion_id}/publish", summary="Publish a promotion")
def publish_promotion(promotion_id: int, principal: ConsolePrincipal, session: SessionDep) -> dict:
    row = PromotionService(session).transition(principal=principal, promotion_id=promotion_id, publish=True)
    return envelope(data=_data(row))


@router.post("/promotions/{promotion_id}/unpublish", summary="End a promotion")
def unpublish_promotion(promotion_id: int, principal: ConsolePrincipal, session: SessionDep) -> dict:
    row = PromotionService(session).transition(principal=principal, promotion_id=promotion_id, publish=False)
    return envelope(data=_data(row))
