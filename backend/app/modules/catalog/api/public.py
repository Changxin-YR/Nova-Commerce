"""Published catalog reads; checkout still reprices from the database."""

from __future__ import annotations

from collections import defaultdict
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ProductNotFoundError, envelope
from app.modules.catalog.models import Brand, Category, Product, ProductImage, ProductSku
from app.modules.inventory.models import Inventory, Warehouse
from app.modules.order.schemas import page_meta
from app.shared.db.session import get_session
from app.shared.storage.factory import get_object_storage
from app.shared.storage.port import StorageError

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


def _image_url(image: ProductImage) -> str | None:
    try:
        return get_object_storage().presigned_get_url(bucket=image.bucket, key=image.object_key)
    except StorageError:
        # Object storage is degradable. Product text and order placement remain available.
        return None


def _stock_by_sku(session: Session, sku_ids: list[int]) -> dict[int, int]:
    if not sku_ids:
        return {}
    rows = session.execute(
        select(Inventory.sku_id, Inventory.available_qty, Inventory.safety_stock)
        .join(Warehouse, Warehouse.id == Inventory.warehouse_id)
        .where(Inventory.sku_id.in_(sku_ids), Warehouse.status == "ACTIVE")
    ).all()
    result: dict[int, int] = defaultdict(int)
    for sku_id, available, safety in rows:
        result[sku_id] += max(available - safety, 0)
    return dict(result)


def _active_skus(product: Product) -> list[ProductSku]:
    return sorted(
        (sku for sku in product.skus if sku.deleted_at is None and sku.status == "ACTIVE"),
        key=lambda sku: (sku.sort_order, sku.id),
    )


def _summary(product: Product) -> dict:
    skus = _active_skus(product)
    cheapest = min(skus, key=lambda sku: (sku.price_amount, sku.id), default=None)
    image = next((image for image in product.images if image.role == "PRIMARY"), None)
    return {
        "id": product.id,
        "title": product.name,
        "cover_url": _image_url(image) if image else None,
        "min_price_amount": cheapest.price_amount if cheapest else 0,
        "original_price_amount": (
            cheapest.market_price_amount
            if cheapest and cheapest.market_price_amount > cheapest.price_amount else None
        ),
        "sales_count": product.sales_count,
        "brand_name": product.brand.name if product.brand else None,
        "tags": [],
        "status": product.status,
    }


@router.get("/public/products", summary="Search published products")
def list_products(
    session: SessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
    keyword: Annotated[str | None, Query(max_length=100)] = None,
    category_id: int | None = None,
    brand_id: int | None = None,
    min_price_amount: int | None = None,
    max_price_amount: int | None = None,
    sort: Literal["default", "price_asc", "price_desc", "sales"] = "default",
) -> dict:
    live_prices = (
        select(ProductSku.product_id, func.min(ProductSku.price_amount).label("min_price"))
        .where(ProductSku.status == "ACTIVE", ProductSku.deleted_at.is_(None))
        .group_by(ProductSku.product_id)
        .subquery()
    )
    filters = [Product.status == "PUBLISHED", Product.deleted_at.is_(None)]
    if keyword:
        escaped = keyword.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        filters.append(Product.name.like(f"%{escaped}%", escape="\\"))
    if category_id is not None:
        filters.append(Product.category_id == category_id)
    if brand_id is not None:
        filters.append(Product.brand_id == brand_id)
    if min_price_amount is not None:
        filters.append(live_prices.c.min_price >= min_price_amount)
    if max_price_amount is not None:
        filters.append(live_prices.c.min_price <= max_price_amount)
    order = {
        "default": (Product.sort_order.desc(), Product.id.desc()),
        "price_asc": (live_prices.c.min_price.asc(), Product.id.desc()),
        "price_desc": (live_prices.c.min_price.desc(), Product.id.desc()),
        "sales": (Product.sales_count.desc(), Product.id.desc()),
    }[sort]
    total = int(session.execute(
        select(func.count()).select_from(Product)
        .join(live_prices, live_prices.c.product_id == Product.id).where(*filters)
    ).scalar_one())
    rows = list(session.execute(
        select(Product).join(live_prices, live_prices.c.product_id == Product.id)
        .where(*filters).order_by(*order).limit(page_size).offset((page - 1) * page_size)
    ).scalars())
    return envelope(data={
        "items": [_summary(row) for row in rows],
        "meta": page_meta(page=page, page_size=page_size, total=total).model_dump(),
    })


@router.get("/public/products/{product_id}", summary="Published product detail")
def product_detail(product_id: int, session: SessionDep) -> dict:
    product = session.execute(select(Product).where(
        Product.id == product_id,
        Product.status == "PUBLISHED",
        Product.deleted_at.is_(None),
    )).scalar_one_or_none()
    if product is None:
        raise ProductNotFoundError("published product not found")
    skus = _active_skus(product)
    stock = _stock_by_sku(session, [sku.id for sku in skus])
    images = []
    for image in sorted(product.images, key=lambda row: (row.sort_order, row.id)):
        url = _image_url(image)
        if url:
            images.append({"id": image.id, "url": url, "alt": image.alt_text, "sort_order": image.sort_order})
    summary = _summary(product)
    return envelope(data={
        **summary,
        "subtitle": product.subtitle,
        "description": product.description,
        "category": {"id": product.category.id, "name": product.category.name} if product.category else None,
        "brand": {"id": product.brand.id, "name": product.brand.name} if product.brand else None,
        "images": images,
        "skus": [{
            "id": sku.id,
            "product_id": product.id,
            "sku_code": sku.sku_code,
            "specs": sku.attribute_snapshot or {},
            "price_amount": sku.price_amount,
            "original_price_amount": sku.market_price_amount if sku.market_price_amount > sku.price_amount else None,
            "available_stock": stock.get(sku.id, 0),
            "status": sku.status,
        } for sku in skus],
        "max_price_amount": max((sku.price_amount for sku in skus), default=product.max_price),
        "created_at": product.created_at.isoformat(),
        "updated_at": product.updated_at.isoformat(),
    })


@router.get("/public/categories", summary="Browse categories")
def categories(
    session: SessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 100,
) -> dict:
    predicate = Category.deleted_at.is_(None)
    total = int(session.execute(select(func.count()).select_from(Category).where(predicate)).scalar_one())
    rows = session.execute(select(Category).where(predicate).order_by(
        Category.sort_order, Category.id
    ).limit(page_size).offset((page - 1) * page_size)).scalars()
    items = [{
        "id": row.id, "name": row.name, "parent_id": row.parent_id,
        "level": row.depth, "sort_order": row.sort_order,
    } for row in rows]
    return envelope(data={"items": items, "meta": page_meta(page=page, page_size=page_size, total=total).model_dump()})


@router.get("/public/brands", summary="Browse brands")
def brands(
    session: SessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 100,
) -> dict:
    predicate = Brand.deleted_at.is_(None)
    total = int(session.execute(select(func.count()).select_from(Brand).where(predicate)).scalar_one())
    rows = session.execute(select(Brand).where(predicate).order_by(
        Brand.sort_order, Brand.id
    ).limit(page_size).offset((page - 1) * page_size)).scalars()
    items = [{"id": row.id, "name": row.name, "logo_url": None} for row in rows]
    return envelope(data={"items": items, "meta": page_meta(page=page, page_size=page_size, total=total).model_dump()})
