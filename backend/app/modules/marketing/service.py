"""Preview, create, publish and resolve promotions under merchant scope."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import (
    PermissionDeniedError,
    PromotionConflictError,
    PromotionNotFoundError,
    ValidationError,
)
from app.modules.catalog.models import Brand, Category, Product, ProductSku
from app.modules.identity.enums import PermissionCode
from app.modules.identity.service import Principal
from app.modules.marketing.models import Promotion, PromotionProduct
from app.modules.marketing.preview_token import PREVIEW_TTL, issue_preview_token, verify_preview_token
from app.modules.marketing.schemas import PromotionCreate, PromotionDraft, PromotionScope
from app.modules.order.models import Order, OrderItem
from app.modules.pricing import PricedLine, PricingService, PromotionRule
from app.shared.db.base import utc_now


def _merchant(principal: Principal, *, write: bool) -> int:
    principal.require_permission(
        PermissionCode.PROMOTION_WRITE.value if write else PermissionCode.PROMOTION_READ.value
    )
    if principal.merchant_id is None:
        raise PermissionDeniedError("a promotion requires a merchant-scoped operator")
    return principal.merchant_id


def _scope_products(session: Session, *, merchant_id: int, scope: PromotionScope) -> set[int]:
    if scope.category_ids:
        categories = set(
            session.execute(
                select(Category.id).where(
                    Category.id.in_(scope.category_ids),
                    Category.merchant_id == merchant_id,
                    Category.deleted_at.is_(None),
                )
            ).scalars()
        )
        if categories != set(scope.category_ids):
            raise ValidationError("promotion scope contains a category outside this merchant")
    if scope.brand_ids:
        brands = set(
            session.execute(
                select(Brand.id).where(
                    Brand.id.in_(scope.brand_ids),
                    Brand.merchant_id == merchant_id,
                    Brand.deleted_at.is_(None),
                )
            ).scalars()
        )
        if brands != set(scope.brand_ids):
            raise ValidationError("promotion scope contains a brand outside this merchant")
    products = session.execute(
        select(Product.id, Product.category_id, Product.brand_id).where(
            Product.merchant_id == merchant_id, Product.deleted_at.is_(None)
        )
    ).all()
    known_ids = {row.id for row in products}
    if not set(scope.product_ids) <= known_ids:
        raise ValidationError("promotion scope contains a product outside this merchant")
    if scope.all_products:
        return known_ids
    return {
        row.id
        for row in products
        if row.id in scope.product_ids
        or row.category_id in scope.category_ids
        or row.brand_id in scope.brand_ids
    }


def promotion_rule(promotion: Promotion, *, eligible_sku_ids: set[int]) -> PromotionRule:
    config = promotion.rule_config
    return PromotionRule(
        promotion_id=promotion.id,
        promotion_type=promotion.promotion_type,
        threshold_amount=config.get("threshold_amount", 0),
        discount_amount=config.get("discount_amount", 0),
        discount_bps=config.get("discount_bps", 0),
        reduction_amount=config.get("reduction_amount", 0),
        max_discount_amount=config.get("max_discount_amount"),
        applicable_sku_ids=frozenset(eligible_sku_ids),
    )


class PromotionService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def resolve_for_cart(
        self,
        *,
        merchant_id: int,
        sku_ids: set[int],
        now: datetime,
        for_update: bool,
    ) -> tuple[PromotionRule | None, Promotion | None]:
        """Select one eligible active rule; order creation may reserve its quota."""
        if not sku_ids:
            return None, None
        sku_rows = self._session.execute(
            select(ProductSku.id, Product.id, Product.category_id, Product.brand_id)
            .join(Product, Product.id == ProductSku.product_id)
            .where(ProductSku.id.in_(sku_ids), Product.merchant_id == merchant_id)
        ).all()
        query = (
            select(Promotion)
            .where(
                Promotion.merchant_id == merchant_id,
                Promotion.status == "ACTIVE",
                Promotion.starts_at <= now,
                Promotion.ends_at > now,
                Promotion.used_quota < Promotion.total_quota,
            )
            .order_by(Promotion.priority.desc(), Promotion.id)
        )
        if for_update:
            query = query.with_for_update()
        for promotion in self._session.execute(query).scalars():
            scope = PromotionScope.model_validate(promotion.scope)
            eligible = {
                row[0]
                for row in sku_rows
                if scope.all_products
                or row[1] in scope.product_ids
                or row[2] in scope.category_ids
                or row[3] in scope.brand_ids
            }
            if eligible:
                return promotion_rule(promotion, eligible_sku_ids=eligible), promotion
        return None, None

    def preview(self, *, principal: Principal, draft: PromotionDraft) -> dict:
        merchant_id = _merchant(principal, write=True)
        now = utc_now()
        product_ids = _scope_products(self._session, merchant_id=merchant_id, scope=draft.scope)
        token = issue_preview_token(
            draft,
            merchant_id=merchant_id,
            now=now,
            secret=get_settings().JWT_SECRET_KEY.get_secret_value(),
        )
        rows = self._session.execute(
            select(Promotion).where(
                Promotion.merchant_id == merchant_id,
                Promotion.status.in_(["DRAFT", "ACTIVE"]),
                Promotion.starts_at < draft.ends_at,
                Promotion.ends_at > draft.starts_at,
            )
        ).scalars()
        conflicts = []
        for existing in rows:
            existing_products = _scope_products(
                self._session,
                merchant_id=merchant_id,
                scope=PromotionScope.model_validate(existing.scope),
            )
            if product_ids & existing_products:
                conflicts.append(
                    {
                        "promotion_id": existing.id,
                        "promotion_no": existing.promotion_no,
                        "reason": "OVERLAPPING_WINDOW_AND_SCOPE",
                    }
                )

        impact = self._estimate_impact(draft=draft, merchant_id=merchant_id, product_ids=product_ids, now=now)
        return {
            "preview_token": token,
            "expires_at": (now + PREVIEW_TTL).isoformat(),
            "promotion": {
                **draft.model_dump(mode="json"),
                "id": None,
                "promotion_no": None,
                "merchant_id": merchant_id,
                "status": "DRAFT",
                "used_quota": 0,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
            "estimated_impact": impact,
            "conflicts": conflicts,
            "warnings": ["该促销与现有活动在时间和商品范围上重叠"] if conflicts else [],
        }

    def _estimate_impact(
        self, *, draft: PromotionDraft, merchant_id: int, product_ids: set[int], now: datetime
    ) -> dict[str, int]:
        if not product_ids:
            return {
                "affected_sku_count": 0,
                "affected_order_count_30d": 0,
                "estimated_discount_amount_30d": 0,
            }
        sku_count = self._session.execute(
            select(func.count()).select_from(ProductSku).where(ProductSku.product_id.in_(product_ids))
        ).scalar_one()
        historical = self._session.execute(
            select(OrderItem)
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                Order.merchant_id == merchant_id,
                Order.created_at >= now - timedelta(days=30),
                OrderItem.product_id.in_(product_ids),
            )
        ).scalars()
        by_order: dict[int, list[OrderItem]] = defaultdict(list)
        for item in historical:
            by_order[item.order_id].append(item)
        amount = 0
        for items in by_order.values():
            rule = PromotionRule(
                promotion_id=0,
                promotion_type=draft.promotion_type,
                threshold_amount=draft.rule_config.get("threshold_amount", 0),
                discount_amount=draft.rule_config.get("discount_amount", 0),
                discount_bps=draft.rule_config.get("discount_bps", 0),
                reduction_amount=draft.rule_config.get("reduction_amount", 0),
                max_discount_amount=draft.rule_config.get("max_discount_amount"),
                applicable_sku_ids=frozenset(item.sku_id for item in items),
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
                for item in items
            ]
            amount += PricingService().calculate_cart_price(lines, promotion=rule).promotion_discount_amount
        return {
            "affected_sku_count": int(sku_count),
            "affected_order_count_30d": len(by_order),
            "estimated_discount_amount_30d": amount,
        }

    def create(self, *, principal: Principal, payload: PromotionCreate) -> Promotion:
        merchant_id = _merchant(principal, write=True)
        draft = PromotionDraft.model_validate(payload.model_dump(exclude={"preview_token"}))
        now = utc_now()
        token_hash = verify_preview_token(
            payload.preview_token,
            draft,
            merchant_id=merchant_id,
            now=now,
            secret=get_settings().JWT_SECRET_KEY.get_secret_value(),
        )
        _scope_products(self._session, merchant_id=merchant_id, scope=draft.scope)
        promotion = Promotion(
            merchant_id=merchant_id,
            promotion_no=token_hash[:32],
            preview_token_hash=token_hash,
            name=draft.name,
            description=draft.description,
            promotion_type=draft.promotion_type.value,
            status="DRAFT",
            priority=draft.priority,
            stackable=draft.stackable,
            rule_config=draft.rule_config,
            scope=draft.scope.model_dump(),
            starts_at=draft.starts_at,
            ends_at=draft.ends_at,
            total_quota=draft.total_quota,
            used_quota=0,
        )
        try:
            with self._session.begin_nested():
                self._session.add(promotion)
                self._session.flush()
                promotion.promotion_no = f"NVP{now:%Y%m%d}{promotion.id:06d}"
                for product_id in draft.scope.product_ids:
                    self._session.add(
                        PromotionProduct(
                            merchant_id=merchant_id,
                            promotion_id=promotion.id,
                            product_id=product_id,
                        )
                    )
                self._session.flush()
            self._session.commit()
            return promotion
        except IntegrityError:
            self._session.rollback()
            existing = self._session.execute(
                select(Promotion).where(
                    Promotion.merchant_id == merchant_id,
                    Promotion.preview_token_hash == token_hash,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            raise

    def transition(self, *, principal: Principal, promotion_id: int, publish: bool) -> Promotion:
        merchant_id = _merchant(principal, write=True)
        promotion = self._session.execute(
            select(Promotion)
            .where(
                Promotion.id == promotion_id,
                Promotion.merchant_id == merchant_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if promotion is None:
            raise PromotionNotFoundError("promotion not found")
        expected = "DRAFT" if publish else "ACTIVE"
        if promotion.status != expected or (publish and promotion.ends_at <= utc_now()):
            raise PromotionConflictError("promotion cannot make this transition")
        promotion.status = "ACTIVE" if publish else "ENDED"
        self._session.commit()
        return promotion

    def list_admin(
        self, *, principal: Principal, page: int = 1, page_size: int = 20
    ) -> tuple[list[Promotion], int]:
        merchant_id = _merchant(principal, write=False)
        predicate = Promotion.merchant_id == merchant_id
        total = int(
            self._session.execute(select(func.count()).select_from(Promotion).where(predicate)).scalar_one()
        )
        rows = (
            self._session.execute(
                select(Promotion)
                .where(predicate)
                .order_by(Promotion.created_at.desc(), Promotion.id.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
            .scalars()
            .all()
        )
        return list(rows), total
