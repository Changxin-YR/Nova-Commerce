"""Promotion records and explicit product scope (spec §39/§47)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db.base import Base, MerchantScopedMixin, PkMixin, TimestampMixin, status_column
from app.shared.db.types import BigIntUnsigned, DateTimeMS, MoneyMinor


class Promotion(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    __tablename__ = "promotions"
    __table_args__ = (
        UniqueConstraint("merchant_id", "promotion_no", name="uq_promotions_merchant_no"),
        UniqueConstraint("merchant_id", "preview_token_hash", name="uq_promotions_preview_token"),
        CheckConstraint(
            "promotion_type IN ('DIRECT_DISCOUNT','PERCENT_DISCOUNT','FULL_REDUCTION')",
            name="type_valid",
        ),
        CheckConstraint("status IN ('DRAFT','ACTIVE','ENDED')", name="status_valid"),
        CheckConstraint(
            "total_quota >= 0 AND used_quota >= 0 AND used_quota <= total_quota", name="quota_valid"
        ),
        CheckConstraint("starts_at < ends_at", name="window_valid"),
        Index("ix_promotions_merchant_active_window", "merchant_id", "status", "starts_at", "ends_at"),
        Index("ix_promotions_priority", "priority", "id"),
    )

    merchant_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    promotion_no: Mapped[str] = mapped_column(String(32), nullable=False)
    preview_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    promotion_type: Mapped[str] = mapped_column(status_column(24), nullable=False)
    status: Mapped[str] = mapped_column(
        status_column(16), nullable=False, default="DRAFT", server_default="DRAFT"
    )
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    stackable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    rule_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    scope: Mapped[dict] = mapped_column(JSON, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTimeMS, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTimeMS, nullable=False)
    total_quota: Mapped[int] = mapped_column(Integer, nullable=False)
    used_quota: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")


class PromotionProduct(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    __tablename__ = "promotion_products"
    __table_args__ = (
        UniqueConstraint("promotion_id", "product_id", name="uq_promotion_products_pair"),
        Index("ix_promotion_products_product", "product_id", "promotion_id"),
    )

    merchant_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    promotion_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("promotions.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )


class CouponTemplate(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    __tablename__ = "coupon_templates"
    __table_args__ = (
        UniqueConstraint("merchant_id", "template_no", name="uq_coupon_templates_merchant_no"),
        UniqueConstraint("merchant_id", "preview_token_hash", name="uq_coupon_templates_preview_token"),
        CheckConstraint("coupon_type IN ('FIXED_AMOUNT','PERCENT_DISCOUNT')", name="type_valid"),
        CheckConstraint("status IN ('DRAFT','ACTIVE','ENDED')", name="status_valid"),
        CheckConstraint(
            "total_quota > 0 AND issued_count >= 0 AND issued_count <= total_quota "
            "AND per_user_limit > 0", name="quota_valid"
        ),
        CheckConstraint(
            "threshold_amount >= 0 AND (max_discount_amount IS NULL OR max_discount_amount > 0)",
            name="amount_valid",
        ),
        CheckConstraint(
            "(coupon_type = 'FIXED_AMOUNT' AND face_value_amount > 0 "
            "AND discount_bps IS NULL AND max_discount_amount IS NULL) "
            "OR (coupon_type = 'PERCENT_DISCOUNT' AND face_value_amount IS NULL "
            "AND discount_bps BETWEEN 1 AND 10000)", name="value_valid"
        ),
        CheckConstraint(
            "(validity_type = 'RELATIVE' AND valid_days > 0 AND valid_from IS NULL AND valid_to IS NULL) "
            "OR (validity_type = 'ABSOLUTE' AND valid_days IS NULL AND valid_from < valid_to)",
            name="validity_valid",
        ),
        Index("ix_coupon_templates_merchant_status", "merchant_id", "status"),
    )

    merchant_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    template_no: Mapped[str] = mapped_column(String(32), nullable=False)
    preview_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    coupon_type: Mapped[str] = mapped_column(status_column(24), nullable=False)
    status: Mapped[str] = mapped_column(status_column(16), nullable=False, default="DRAFT", server_default="DRAFT")
    face_value_amount: Mapped[int | None] = mapped_column(MoneyMinor, nullable=True)
    discount_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    threshold_amount: Mapped[int] = mapped_column(MoneyMinor, nullable=False, default=0, server_default="0")
    max_discount_amount: Mapped[int | None] = mapped_column(MoneyMinor, nullable=True)
    total_quota: Mapped[int] = mapped_column(Integer, nullable=False)
    issued_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    per_user_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    validity_type: Mapped[str] = mapped_column(status_column(16), nullable=False)
    valid_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    applicable_scope: Mapped[dict] = mapped_column(JSON, nullable=False)


class UserCoupon(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    __tablename__ = "user_coupons"
    __table_args__ = (
        CheckConstraint("status IN ('UNUSED','LOCKED','USED','EXPIRED')", name="status_valid"),
        CheckConstraint("valid_from < valid_to", name="window_valid"),
        CheckConstraint(
            "(status IN ('UNUSED','EXPIRED') AND order_id IS NULL) "
            "OR (status IN ('LOCKED','USED') AND order_id IS NOT NULL)",
            name="order_lock_valid",
        ),
        Index("ix_user_coupons_user_status_expiry", "user_id", "status", "valid_to"),
    )

    merchant_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    template_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("coupon_templates.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(status_column(16), nullable=False, default="UNUSED", server_default="UNUSED")
    valid_from: Mapped[datetime] = mapped_column(DateTimeMS, nullable=False)
    valid_to: Mapped[datetime] = mapped_column(DateTimeMS, nullable=False)
    order_id: Mapped[int | None] = mapped_column(
        BigIntUnsigned, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)


class CouponUsageRecord(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    __tablename__ = "coupon_usage_records"
    __table_args__ = (
        UniqueConstraint("user_coupon_id", "order_id", "action", name="uq_coupon_usage_action"),
        CheckConstraint("action IN ('LOCK','USE','RELEASE')", name="action_valid"),
    )

    merchant_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("merchants.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    user_coupon_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("user_coupons.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    order_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(status_column(16), nullable=False)
