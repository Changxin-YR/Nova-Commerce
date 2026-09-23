"""Promotion records and explicit product scope (spec §39/§47)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db.base import Base, MerchantScopedMixin, PkMixin, TimestampMixin, status_column
from app.shared.db.types import BigIntUnsigned, DateTimeMS


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
