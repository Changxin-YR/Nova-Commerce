"""Coupon template creation, issuance, and transactional order lifecycle."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    CouponAlreadyLockedError,
    CouponAlreadyUsedError,
    CouponExpiredError,
    CouponNotApplicableError,
    CouponNotFoundError,
    CouponThresholdNotMetError,
    PermissionDeniedError,
    PromotionConflictError,
    ValidationError,
)
from app.modules.catalog.models import Product, ProductSku
from app.modules.identity.enums import PermissionCode
from app.modules.identity.service import Principal
from app.modules.marketing.coupon_schemas import CouponCreate, CouponDraft, CouponScope
from app.modules.marketing.models import CouponTemplate, CouponUsageRecord, UserCoupon
from app.modules.marketing.preview_token import PREVIEW_TTL, issue_preview_token, verify_preview_token
from app.modules.marketing.schemas import PromotionScope
from app.modules.marketing.service import _scope_products
from app.modules.order.models import Order, OrderItem
from app.modules.pricing import CartPrice, CouponRule, PricedLine, PricingService
from app.modules.pricing.enums import PricingWarning
from app.shared.db.base import utc_now


def _merchant(principal: Principal, *, write: bool) -> int:
    principal.require_permission(
        PermissionCode.COUPON_WRITE.value if write else PermissionCode.COUPON_READ.value
    )
    if principal.merchant_id is None:
        raise PermissionDeniedError("a coupon template requires a merchant-scoped operator")
    return principal.merchant_id


def _products(session: Session, *, merchant_id: int, scope: CouponScope) -> set[int]:
    return _scope_products(
        session,
        merchant_id=merchant_id,
        scope=PromotionScope(
            all_products=scope.all_products,
            product_ids=scope.product_ids,
            category_ids=scope.category_ids,
            brand_ids=[],
        ),
    )


def _rule(template: CouponTemplate, *, coupon_id: int, sku_ids: set[int]) -> CouponRule:
    return CouponRule(
        coupon_id=coupon_id,
        coupon_type=template.coupon_type,
        threshold_amount=template.threshold_amount,
        face_value_amount=template.face_value_amount or 0,
        discount_bps=template.discount_bps or 0,
        max_discount_amount=template.max_discount_amount,
        applicable_sku_ids=frozenset(sku_ids),
    )


def require_applicable_discount(cart: CartPrice) -> None:
    """A selected coupon must reduce the cart or fail with an actionable code."""
    if cart.coupon_discount_amount > 0:
        return
    if PricingWarning.COUPON_THRESHOLD_NOT_MET.value in cart.warnings:
        raise CouponThresholdNotMetError("coupon threshold is not met")
    raise CouponNotApplicableError("coupon does not discount these items")


class CouponService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def preview(self, *, principal: Principal, draft: CouponDraft) -> dict:
        merchant_id = _merchant(principal, write=True)
        now = utc_now()
        product_ids = _products(self._session, merchant_id=merchant_id, scope=draft.applicable_scope)
        token = issue_preview_token(
            draft,
            merchant_id=merchant_id,
            now=now,
            secret=get_settings().JWT_SECRET_KEY.get_secret_value(),
        )
        sku_count = 0
        if product_ids:
            sku_count = int(
                self._session.execute(
                    select(func.count()).select_from(ProductSku).where(ProductSku.product_id.in_(product_ids))
                ).scalar_one()
            )
        impact = self._estimate_impact(draft, merchant_id, product_ids, now)
        return {
            "preview_token": token,
            "expires_at": (now + PREVIEW_TTL).isoformat(),
            "coupon": {
                **draft.model_dump(mode="json"),
                "id": None,
                "template_no": None,
                "merchant_id": merchant_id,
                "status": "DRAFT",
                "issued_count": 0,
                "created_at": now.isoformat(),
            },
            "estimated_impact": {
                "affected_sku_count": sku_count,
                **impact,
            },
            "warnings": ["预估基于近 30 天订单重算; 未计入活动叠加和每人限领"],
        }

    def _estimate_impact(
        self, draft: CouponDraft, merchant_id: int, product_ids: set[int], now: datetime
    ) -> dict[str, int]:
        if not product_ids:
            return {
                "affected_order_count_30d": 0,
                "estimated_issue_count": 0,
                "estimated_discount_amount": 0,
            }
        items = self._session.execute(
                select(OrderItem)
                .join(Order, Order.id == OrderItem.order_id)
                .where(
                    Order.merchant_id == merchant_id,
                    Order.created_at >= now - timedelta(days=30),
                    OrderItem.product_id.in_(product_ids),
                )
                .order_by(Order.id, OrderItem.id)
        ).scalars()
        by_order: dict[int, list[OrderItem]] = defaultdict(list)
        for item in items:
            by_order[item.order_id].append(item)
        discounts: list[int] = []
        for order_items in by_order.values():
            rule = CouponRule(
                coupon_id=0,
                coupon_type=draft.coupon_type,
                threshold_amount=draft.threshold_amount,
                face_value_amount=draft.face_value_amount or 0,
                discount_bps=draft.discount_bps or 0,
                max_discount_amount=draft.max_discount_amount,
                applicable_sku_ids=frozenset(item.sku_id for item in order_items),
            )
            lines = [
                PricedLine(
                    sku_id=item.sku_id,
                    product_id=item.product_id,
                    product_name=item.product_name,
                    sku_name=item.sku_name,
                    image_object_key=None,
                    image_url=None,
                    sku_snapshot=item.sku_snapshot,
                    unit_price=item.unit_price,
                    quantity=item.quantity,
                )
                for item in order_items
            ]
            discount = PricingService().calculate_cart_price(lines, coupon=rule).coupon_discount_amount
            if discount > 0:
                discounts.append(discount)
        return {
            "affected_order_count_30d": len(by_order),
            "estimated_issue_count": min(draft.total_quota, len(discounts)),
            "estimated_discount_amount": sum(discounts[: draft.total_quota]),
        }

    def create(self, *, principal: Principal, payload: CouponCreate) -> CouponTemplate:
        merchant_id = _merchant(principal, write=True)
        draft = CouponDraft.model_validate(payload.model_dump(exclude={"preview_token"}))
        now = utc_now()
        token_hash = verify_preview_token(
            payload.preview_token,
            draft,
            merchant_id=merchant_id,
            now=now,
            secret=get_settings().JWT_SECRET_KEY.get_secret_value(),
        )
        _products(self._session, merchant_id=merchant_id, scope=draft.applicable_scope)
        template = CouponTemplate(
            merchant_id=merchant_id,
            template_no=token_hash[:32],
            preview_token_hash=token_hash,
            name=draft.name,
            coupon_type=draft.coupon_type.value,
            status="DRAFT",
            face_value_amount=draft.face_value_amount,
            discount_bps=draft.discount_bps,
            threshold_amount=draft.threshold_amount,
            max_discount_amount=draft.max_discount_amount,
            total_quota=draft.total_quota,
            issued_count=0,
            per_user_limit=draft.per_user_limit,
            validity_type=draft.validity_type,
            valid_days=draft.valid_days,
            valid_from=draft.valid_from,
            valid_to=draft.valid_to,
            applicable_scope=draft.applicable_scope.model_dump(),
        )
        try:
            with self._session.begin_nested():
                self._session.add(template)
                self._session.flush()
                template.template_no = f"NVC{now:%Y%m%d}{template.id:06d}"
                self._session.flush()
            self._session.commit()
            return template
        except IntegrityError:
            self._session.rollback()
            existing = self._session.execute(
                select(CouponTemplate).where(
                    CouponTemplate.merchant_id == merchant_id,
                    CouponTemplate.preview_token_hash == token_hash,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            raise

    def transition(self, *, principal: Principal, template_id: int, publish: bool) -> CouponTemplate:
        merchant_id = _merchant(principal, write=True)
        template = self._session.execute(
            select(CouponTemplate).where(
                CouponTemplate.id == template_id,
                CouponTemplate.merchant_id == merchant_id,
            ).with_for_update()
        ).scalar_one_or_none()
        if template is None:
            raise CouponNotFoundError("coupon template not found")
        if template.status != ("DRAFT" if publish else "ACTIVE"):
            raise PromotionConflictError("coupon template cannot make this transition")
        if publish and template.validity_type == "ABSOLUTE" and template.valid_to <= utc_now():
            raise CouponExpiredError("coupon template validity window has ended")
        template.status = "ACTIVE" if publish else "ENDED"
        self._session.commit()
        return template

    def list_admin(self, *, principal: Principal, page: int, page_size: int) -> tuple[list[CouponTemplate], int]:
        merchant_id = _merchant(principal, write=False)
        predicate = CouponTemplate.merchant_id == merchant_id
        total = int(self._session.execute(
            select(func.count()).select_from(CouponTemplate).where(predicate)
        ).scalar_one())
        rows = list(self._session.execute(
            select(CouponTemplate).where(predicate)
            .order_by(CouponTemplate.created_at.desc(), CouponTemplate.id.desc())
            .limit(page_size).offset((page - 1) * page_size)
        ).scalars())
        return rows, total

    def available(
        self, *, principal: Principal, page: int, page_size: int,
    ) -> tuple[list[CouponTemplate], int]:
        """Only templates this customer can still claim at the time of reading."""
        if principal.is_staff:
            raise PermissionDeniedError("coupon discovery requires a customer account")
        now = utc_now()
        owned_count = (
            select(func.count(UserCoupon.id))
            .where(
                UserCoupon.template_id == CouponTemplate.id,
                UserCoupon.user_id == principal.user_id,
            )
            .correlate(CouponTemplate)
            .scalar_subquery()
        )
        filters = (
            CouponTemplate.status == "ACTIVE",
            CouponTemplate.issued_count < CouponTemplate.total_quota,
            or_(CouponTemplate.validity_type == "RELATIVE", CouponTemplate.valid_to > now),
            owned_count < CouponTemplate.per_user_limit,
        )
        total = int(self._session.execute(
            select(func.count()).select_from(CouponTemplate).where(*filters)
        ).scalar_one())
        rows = list(self._session.execute(
            select(CouponTemplate).where(*filters)
            .order_by(CouponTemplate.created_at.desc(), CouponTemplate.id.desc())
            .limit(page_size).offset((page - 1) * page_size)
        ).scalars())
        return rows, total

    def claim(self, *, principal: Principal, template_id: int) -> UserCoupon:
        """Issue once under the template row lock, enforcing both quotas."""
        if principal.is_staff:
            raise PermissionDeniedError("coupon claims require a customer account")
        template = self._session.execute(
            select(CouponTemplate).where(CouponTemplate.id == template_id).with_for_update()
        ).scalar_one_or_none()
        if template is None or template.status != "ACTIVE":
            raise CouponNotFoundError("active coupon template not found")
        now = utc_now()
        if template.issued_count >= template.total_quota:
            raise CouponNotApplicableError("coupon issuance quota is exhausted")
        existing = int(self._session.execute(
            select(func.count()).select_from(UserCoupon).where(
                UserCoupon.template_id == template.id, UserCoupon.user_id == principal.user_id
            )
        ).scalar_one())
        if existing >= template.per_user_limit:
            raise CouponNotApplicableError("per-user coupon limit is reached")
        if template.validity_type == "RELATIVE":
            starts = now
            ends = now + timedelta(days=template.valid_days)
        else:
            starts = template.valid_from
            ends = template.valid_to
            if ends <= now:
                raise CouponExpiredError("coupon template validity window has ended")
        coupon = UserCoupon(
            merchant_id=template.merchant_id,
            template_id=template.id,
            user_id=principal.user_id,
            status="UNUSED",
            valid_from=starts,
            valid_to=ends,
        )
        template.issued_count += 1
        self._session.add(coupon)
        self._session.commit()
        return coupon

    def mine(self, *, principal: Principal) -> list[UserCoupon]:
        if principal.is_staff:
            raise PermissionDeniedError("customer coupon list requires a customer account")
        return list(self._session.execute(
            select(UserCoupon).where(UserCoupon.user_id == principal.user_id)
            .order_by(UserCoupon.created_at.desc(), UserCoupon.id.desc())
        ).scalars())

    def resolve_for_cart(
        self,
        *,
        coupon_id: int,
        user_id: int,
        merchant_id: int,
        sku_ids: set[int],
        now: datetime,
        for_update: bool,
    ) -> tuple[CouponRule, UserCoupon]:
        query = select(UserCoupon).where(
            UserCoupon.id == coupon_id,
            UserCoupon.user_id == user_id,
            UserCoupon.merchant_id == merchant_id,
        )
        if for_update:
            query = query.with_for_update()
        coupon = self._session.execute(query).scalar_one_or_none()
        if coupon is None:
            raise CouponNotFoundError("coupon not found for this customer and merchant")
        template = self._session.execute(
            select(CouponTemplate).where(
                CouponTemplate.id == coupon.template_id,
                CouponTemplate.merchant_id == merchant_id,
            )
        ).scalar_one_or_none()
        if template is None:
            raise CouponNotFoundError("coupon template does not belong to this merchant")
        if coupon.status == "LOCKED":
            raise CouponAlreadyLockedError("coupon is locked by another order")
        if coupon.status == "USED":
            raise CouponAlreadyUsedError("coupon has already been used")
        if coupon.status == "EXPIRED" or coupon.valid_to <= now:
            raise CouponExpiredError("coupon has expired")
        if coupon.status != "UNUSED" or coupon.valid_from > now:
            raise CouponNotApplicableError("coupon is not currently valid")
        scope = CouponScope.model_validate(template.applicable_scope)
        sku_rows = self._session.execute(
            select(ProductSku.id, Product.id, Product.category_id)
            .join(Product, Product.id == ProductSku.product_id)
            .where(ProductSku.id.in_(sku_ids), Product.merchant_id == merchant_id)
        ).all()
        eligible = {
            row[0] for row in sku_rows
            if scope.all_products or row[1] in scope.product_ids or row[2] in scope.category_ids
        }
        if not eligible:
            raise CouponNotApplicableError("coupon scope does not include these items")
        return _rule(template, coupon_id=coupon.id, sku_ids=eligible), coupon

    def lock_for_order(self, *, coupon: UserCoupon, order: Order, now: datetime) -> None:
        if coupon.merchant_id != order.merchant_id or coupon.user_id != order.user_id:
            raise CouponNotFoundError("coupon does not belong to this order")
        if coupon.status != "UNUSED" or coupon.valid_to <= now:
            raise CouponAlreadyLockedError("coupon changed before order creation")
        coupon.status = "LOCKED"
        coupon.order_id = order.id
        coupon.locked_at = now
        self._session.add(CouponUsageRecord(
            merchant_id=order.merchant_id,
            user_coupon_id=coupon.id,
            order_id=order.id,
            action="LOCK",
        ))
        self._session.flush()

    def mark_used(self, *, order: Order, now: datetime) -> None:
        if order.coupon_id is None:
            return
        coupon = self._session.execute(
            select(UserCoupon).where(
                UserCoupon.id == order.coupon_id,
                UserCoupon.merchant_id == order.merchant_id,
                UserCoupon.user_id == order.user_id,
            ).with_for_update()
        ).scalar_one_or_none()
        if coupon is None or coupon.status != "LOCKED" or coupon.order_id != order.id:
            raise ValidationError("paid order does not hold its selected coupon")
        coupon.status = "USED"
        coupon.used_at = now
        self._session.add(CouponUsageRecord(
            merchant_id=order.merchant_id,
            user_coupon_id=coupon.id,
            order_id=order.id,
            action="USE",
        ))
        self._session.flush()

    def release(self, *, order: Order, now: datetime) -> None:
        if order.coupon_id is None:
            return
        coupon = self._session.execute(
            select(UserCoupon).where(
                UserCoupon.id == order.coupon_id,
                UserCoupon.merchant_id == order.merchant_id,
                UserCoupon.user_id == order.user_id,
            ).with_for_update()
        ).scalar_one_or_none()
        if coupon is None or coupon.status != "LOCKED" or coupon.order_id != order.id:
            raise ValidationError("cancelled order does not hold its selected coupon")
        coupon.status = "EXPIRED" if coupon.valid_to <= now else "UNUSED"
        coupon.order_id = None
        coupon.locked_at = None
        self._session.add(CouponUsageRecord(
            merchant_id=order.merchant_id,
            user_coupon_id=coupon.id,
            order_id=order.id,
            action="RELEASE",
        ))
        self._session.flush()
