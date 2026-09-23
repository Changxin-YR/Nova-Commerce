"""Strict promotion input shapes matching API_CONTRACT §13.1."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.pricing.enums import PromotionType


class PromotionScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    all_products: bool
    product_ids: list[int] = Field(default_factory=list)
    category_ids: list[int] = Field(default_factory=list)
    brand_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_scope(self) -> PromotionScope:
        groups = (self.product_ids, self.category_ids, self.brand_ids)
        if self.all_products and any(groups):
            raise ValueError("all_products cannot be combined with narrower scope")
        if not self.all_products and not any(groups):
            raise ValueError("an explicit product, category or brand scope is required")
        if any(value <= 0 for group in groups for value in group):
            raise ValueError("scope identifiers must be positive")
        if any(len(group) != len(set(group)) for group in groups):
            raise ValueError("scope identifiers must be unique")
        return self


class PromotionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    promotion_type: PromotionType
    priority: int = Field(default=100, ge=0, le=10000)
    stackable: bool = False
    rule_config: dict[str, Any]
    scope: PromotionScope
    starts_at: datetime
    ends_at: datetime
    total_quota: int = Field(gt=0)

    @model_validator(mode="after")
    def valid_rule(self) -> PromotionDraft:
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("promotion times must include a timezone")
        if self.starts_at >= self.ends_at:
            raise ValueError("starts_at must precede ends_at")
        fields = {
            PromotionType.DIRECT_DISCOUNT: {"discount_amount"},
            PromotionType.PERCENT_DISCOUNT: {"discount_bps", "max_discount_amount"},
            PromotionType.FULL_REDUCTION: {"threshold_amount", "reduction_amount", "max_discount_amount"},
        }[self.promotion_type]
        if set(self.rule_config) != fields:
            raise ValueError("rule_config fields do not match promotion_type")
        for key, value in self.rule_config.items():
            if value is None and key == "max_discount_amount":
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{key} must be a positive integer")
        if self.rule_config.get("discount_bps", 0) > 10000:
            raise ValueError("discount_bps cannot exceed 10000")
        return self


class PromotionCreate(PromotionDraft):
    preview_token: str = Field(min_length=1)
