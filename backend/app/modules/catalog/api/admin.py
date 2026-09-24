"""Merchant catalog reads and draft edits."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.catalog.api.public import _image_url, _summary
from app.modules.catalog.models import Product, ProductSku
from app.modules.catalog.schemas import ProductCreate, ProductUpdate, SkuCreate
from app.modules.catalog.service import CatalogService
from app.modules.identity.dependencies import ConsolePrincipal
from app.modules.order.schemas import page_meta
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


def sku_data(sku: ProductSku) -> dict:
    return {
        "id": sku.id,
        "product_id": sku.product_id,
        "sku_code": sku.sku_code,
        "name": sku.name,
        "specs": sku.attribute_snapshot or {},
        "price_amount": sku.price_amount,
        "original_price_amount": sku.market_price_amount or None,
        "status": sku.status,
        "purchase_limit": sku.purchase_limit,
    }


def product_data(product: Product) -> dict:
    return {
        **_summary(product),
        "subtitle": product.subtitle,
        "description": product.description,
        "category": {"id": product.category.id, "name": product.category.name} if product.category else None,
        "brand": {"id": product.brand.id, "name": product.brand.name} if product.brand else None,
        "images": [
            {"id": image.id, "url": url, "alt": image.alt_text, "sort_order": image.sort_order}
            for image in sorted(product.images, key=lambda row: (row.sort_order, row.id))
            if (url := _image_url(image))
        ],
        "skus": [sku_data(sku) for sku in product.skus if sku.deleted_at is None],
        "max_price_amount": product.max_price,
        "created_at": product.created_at.isoformat(),
        "updated_at": product.updated_at.isoformat(),
    }


@router.get("/admin/products", summary="List this merchant's products")
def list_admin_products(
    principal: ConsolePrincipal,
    session: SessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    keyword: Annotated[str | None, Query(max_length=100)] = None,
    status: Literal["DRAFT", "PUBLISHED", "UNPUBLISHED", "ARCHIVED"] | None = None,
) -> dict:
    rows, total = CatalogService(session).list_admin(
        principal=principal, page=page, page_size=page_size, keyword=keyword, status=status,
    )
    return envelope(data={
        "items": [_summary(row) for row in rows],
        "meta": page_meta(page=page, page_size=page_size, total=total).model_dump(),
    })


@router.get("/admin/products/{product_id}", summary="Read this merchant's product")
def admin_product_detail(product_id: int, principal: ConsolePrincipal, session: SessionDep) -> dict:
    product = CatalogService(session).detail(principal=principal, product_id=product_id)
    return envelope(data=product_data(product))


@router.post("/admin/products", summary="Create a draft product")
def create_product(body: ProductCreate, principal: ConsolePrincipal, session: SessionDep) -> dict:
    product = CatalogService(session).create(principal=principal, body=body)
    return envelope(data=product_data(product))


@router.put("/admin/products/{product_id}", summary="Edit a product")
def update_product(
    product_id: int, body: ProductUpdate, principal: ConsolePrincipal, session: SessionDep,
) -> dict:
    product = CatalogService(session).update(principal=principal, product_id=product_id, body=body)
    return envelope(data=product_data(product))


@router.post("/admin/products/{product_id}/skus", summary="Add a SKU to a product")
def add_product_sku(
    product_id: int, body: SkuCreate, principal: ConsolePrincipal, session: SessionDep,
) -> dict:
    sku = CatalogService(session).add_sku(principal=principal, product_id=product_id, body=body)
    return envelope(data=sku_data(sku))
