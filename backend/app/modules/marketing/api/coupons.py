"""Coupon template and customer coupon endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.errors import PermissionDeniedError, envelope
from app.modules.identity.dependencies import ConsolePrincipal, CurrentPrincipal
from app.modules.marketing.coupon_schemas import CouponCreate, CouponDraft
from app.modules.marketing.coupon_service import CouponService
from app.modules.marketing.models import CouponTemplate, UserCoupon
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


def template_data(row: CouponTemplate) -> dict:
    return {
        "id": row.id,
        "template_no": row.template_no,
        "merchant_id": row.merchant_id,
        "name": row.name,
        "coupon_type": row.coupon_type,
        "status": row.status,
        "face_value_amount": row.face_value_amount,
        "discount_bps": row.discount_bps,
        "threshold_amount": row.threshold_amount,
        "max_discount_amount": row.max_discount_amount,
        "total_quota": row.total_quota,
        "issued_count": row.issued_count,
        "per_user_limit": row.per_user_limit,
        "validity_type": row.validity_type,
        "valid_days": row.valid_days,
        "valid_from": row.valid_from.isoformat() if row.valid_from else None,
        "valid_to": row.valid_to.isoformat() if row.valid_to else None,
        "applicable_scope": row.applicable_scope,
        "created_at": row.created_at.isoformat(),
    }


def user_coupon_data(row: UserCoupon) -> dict:
    return {
        "id": row.id,
        "template_id": row.template_id,
        "merchant_id": row.merchant_id,
        "status": row.status,
        "valid_from": row.valid_from.isoformat(),
        "valid_to": row.valid_to.isoformat(),
        "order_id": row.order_id,
        "locked_at": row.locked_at.isoformat() if row.locked_at else None,
        "used_at": row.used_at.isoformat() if row.used_at else None,
    }


@router.post("/coupons/preview", summary="Preview a coupon template before creation")
def preview_coupon(payload: CouponDraft, principal: ConsolePrincipal, session: SessionDep) -> dict:
    return envelope(data=CouponService(session).preview(principal=principal, draft=payload))


@router.post("/coupons", summary="Create a coupon template from its preview")
def create_coupon(payload: CouponCreate, principal: ConsolePrincipal, session: SessionDep) -> dict:
    row = CouponService(session).create(principal=principal, payload=payload)
    return envelope(data=template_data(row))


@router.post("/coupons/{template_id}/publish", summary="Open a coupon template for claims")
def publish_coupon(template_id: int, principal: ConsolePrincipal, session: SessionDep) -> dict:
    row = CouponService(session).transition(principal=principal, template_id=template_id, publish=True)
    return envelope(data=template_data(row))


@router.post("/coupons/{template_id}/unpublish", summary="Close a coupon template for claims")
def unpublish_coupon(template_id: int, principal: ConsolePrincipal, session: SessionDep) -> dict:
    row = CouponService(session).transition(principal=principal, template_id=template_id, publish=False)
    return envelope(data=template_data(row))


@router.post("/coupons/{template_id}/claim", summary="Claim one published coupon")
def claim_coupon(template_id: int, principal: CurrentPrincipal, session: SessionDep) -> dict:
    if principal.is_staff:
        raise PermissionDeniedError("coupon claims require a customer account")
    row = CouponService(session).claim(principal=principal, template_id=template_id)
    return envelope(data=user_coupon_data(row))


@router.get("/coupons/mine", summary="List coupons owned by the current customer")
def my_coupons(principal: CurrentPrincipal, session: SessionDep) -> dict:
    if principal.is_staff:
        raise PermissionDeniedError("customer coupon list requires a customer account")
    return envelope(data=[user_coupon_data(row) for row in CouponService(session).mine(principal=principal)])
