"""Expire due, unreserved coupons from durable database state."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.marketing.models import UserCoupon
from app.shared.db.base import utc_now
from app.shared.db.session import session_scope


def expire_due_coupons(session: Session, *, now: datetime, limit: int = 100) -> int:
    """Claim a bounded batch; a coupon attached to an order stays locked for that order."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    coupons = list(
        session.execute(
            select(UserCoupon)
            .where(UserCoupon.status == "UNUSED", UserCoupon.valid_to <= now)
            .order_by(UserCoupon.valid_to, UserCoupon.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).scalars()
    )
    for coupon in coupons:
        coupon.status = "EXPIRED"
    session.flush()
    return len(coupons)


def run_coupon_expiry_cycle(*, now: datetime | None = None, limit: int = 100) -> int:
    with session_scope() as session:
        return expire_due_coupons(session, now=now or utc_now(), limit=limit)
