"""Coupon template creation shapes from API_CONTRACT §13.3."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.pricing.enums import CouponType


class CouponScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    all_products: bool
    product_ids: list[int] = Field(default_factory=list)
    category_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_scope(self) -> CouponScope:
        groups = (self.product_ids, self.category_ids)
        if self.all_products and any(groups):
            raise ValueError("all_products cannot be combined with narrower scope")
        if not self.all_products and not any(groups):
            raise ValueError("an explicit product or category scope is required")
        if any(value <= 0 for group in groups for value in group):
            raise ValueError("scope identifiers must be positive")
        if any(len(group) != len(set(group)) for group in groups):
            raise ValueError("scope identifiers must be unique")
        return self


class CouponDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    coupon_type: CouponType
    face_value_amount: int | None = Field(default=None, gt=0)
    discount_bps: int | None = Field(default=None, ge=1, le=10000)
    threshold_amount: int = Field(default=0, ge=0)
    max_discount_amount: int | None = Field(default=None, gt=0)
    total_quota: int = Field(gt=0)
    per_user_limit: int = Field(gt=0)
    validity_type: Literal["RELATIVE", "ABSOLUTE"]
    valid_days: int | None = Field(default=None, gt=0)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    applicable_scope: CouponScope

    @model_validator(mode="after")
    def valid_discriminators(self) -> CouponDraft:
        if self.coupon_type is CouponType.FIXED_AMOUNT:
            if (
                self.face_value_amount is None
                or self.discount_bps is not None
                or self.max_discount_amount is not None
            ):
                raise ValueError("fixed coupons require face_value_amount only")
        elif self.discount_bps is None or self.face_value_amount is not None:
            raise ValueError("percentage coupons require discount_bps only")
        if self.validity_type == "RELATIVE":
            if self.valid_days is None or self.valid_from is not None or self.valid_to is not None:
                raise ValueError("relative validity requires valid_days only")
        elif (
            self.valid_days is not None
            or self.valid_from is None
            or self.valid_to is None
            or self.valid_from.tzinfo is None
            or self.valid_to.tzinfo is None
            or self.valid_from >= self.valid_to
        ):
            raise ValueError("absolute validity requires an ordered timezone-aware window")
        return self


class CouponCreate(CouponDraft):
    preview_token: str = Field(min_length=1)
