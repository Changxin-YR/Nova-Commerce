"""Merchant catalog write inputs; status changes use dedicated task routes."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SkuCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku_code: str = Field(min_length=1, max_length=96)
    name: str = Field(min_length=1, max_length=200)
    specs: dict[str, str] = Field(default_factory=dict)
    price_amount: int = Field(gt=0)
    original_price_amount: int = Field(default=0, ge=0)
    purchase_limit: int = Field(default=0, ge=0)


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    subtitle: str | None = Field(default=None, max_length=255)
    description: str | None = None
    category_id: int | None = Field(default=None, gt=0)
    brand_id: int | None = Field(default=None, gt=0)
    skus: list[SkuCreate] = Field(default_factory=list, max_length=50)


class ProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    subtitle: str | None = Field(default=None, max_length=255)
    description: str | None = None
    category_id: int | None = Field(default=None, gt=0)
    brand_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def title_cannot_be_null(self) -> ProductUpdate:
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("title cannot be null")
        return self
