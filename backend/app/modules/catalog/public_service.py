"""Public storefront catalogue reads (search, detail, categories, brands).

## Why this is a service and not the router it replaced

The storefront reads are the one place in the catalogue where a response is
*computed* rather than echoed: the cheapest active SKU determines the "from" price,
`sales_count` is denormalised on the product, live availability is summed across
active warehouses, and every image needs a presigned URL. None of that is
transport, and putting it in a router meant the query, the policy and the response
shape all lived in the interface layer - so the interface could not be replaced
(and could not be tested) without carrying the SQL with it.

The service therefore returns the **wire payload** for each endpoint, and the
router's whole job becomes: validate input, call one method, wrap it in the
``envelope``. That is the architecture invariant this module exists to satisfy:
``Controller -> Application Service -> Repository -> Infrastructure``.

Two behaviours are preserved exactly because they are observable:

* the list is priced from ``product_skus`` in a single subquery rather than from
  ``products.min_price`` (a denormalised column the publication transition
  refreshes), so a price change is visible to shoppers immediately;
* object storage is *degradable* - a storage failure yields ``None`` for a URL
  rather than a 500, because the product text and the ability to order must not
  depend on the CDN.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ProductNotFoundError
from app.modules.catalog.models import Brand, Category, Product, ProductImage, ProductSku
from app.modules.inventory.models import Inventory, Warehouse
from app.modules.order.schemas import page_meta
from app.shared.storage.factory import get_object_storage
from app.shared.storage.port import StorageError

SortOrder = Literal["default", "price_asc", "price_desc", "sales"]


def image_url(image: ProductImage) -> str | None:
    try:
        return get_object_storage().presigned_get_url(bucket=image.bucket, key=image.object_key)
    except StorageError:
        # Object storage is degradable. Product text and order placement remain available.
        return None


def active_skus(product: Product) -> list[ProductSku]:
    """The product's sellable SKUs, in display order."""
    return sorted(
        (sku for sku in product.skus if sku.deleted_at is None and sku.status == "ACTIVE"),
        key=lambda sku: (sku.sort_order, sku.id),
    )


def summary_data(product: Product) -> dict:
    """The list-item view of a product (also the base of the detail view)."""
    skus = active_skus(product)
    cheapest = min(skus, key=lambda sku: (sku.price_amount, sku.id), default=None)
    image = next((image for image in product.images if image.role == "PRIMARY"), None)
    return {
        "id": product.id,
        "title": product.name,
        "cover_url": image_url(image) if image else None,
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


class PublicCatalogService:
    """Storefront reads. No merchant scope, no writes, no transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # -- queries ---------------------------------------------------------
    def _live_prices(self):
        """One row per product holding its cheapest active SKU price.

        A subquery rather than ``products.min_price``: the denormalised column is
        refreshed by the publication transition, so a SKU priced after publication
        would be invisible to a shopper sorting by price - and sorting by a price the
        checkout then disagrees with is a defect the customer sees.
        """
        return (
            select(ProductSku.product_id, func.min(ProductSku.price_amount).label("min_price"))
            .where(ProductSku.status == "ACTIVE", ProductSku.deleted_at.is_(None))
            .group_by(ProductSku.product_id)
            .subquery()
        )

    def _published_products(self, live_prices, filters: list):
        return (
            select(Product)
            .join(live_prices, live_prices.c.product_id == Product.id)
            .where(*filters)
        )

    def stock_by_sku(self, sku_ids: list[int]) -> dict[int, int]:
        """Sellable quantity per SKU, summed over **active** warehouses.

        ``available - safety_stock`` floored at zero, per warehouse, then summed: the
        safety stock is a per-warehouse buffer, so subtracting the sum from the total
        would let one warehouse's buffer cover another's sales.
        """
        if not sku_ids:
            return {}
        rows = self._session.execute(
            select(Inventory.sku_id, Inventory.available_qty, Inventory.safety_stock)
            .join(Warehouse, Warehouse.id == Inventory.warehouse_id)
            .where(Inventory.sku_id.in_(sku_ids), Warehouse.status == "ACTIVE")
        ).all()
        result: dict[int, int] = defaultdict(int)
        for sku_id, available, safety in rows:
            result[sku_id] += max(available - safety, 0)
        return dict(result)

    # -- endpoints -------------------------------------------------------
    def list_products(
        self,
        *,
        page: int,
        page_size: int,
        keyword: str | None,
        category_id: int | None,
        brand_id: int | None,
        min_price_amount: int | None,
        max_price_amount: int | None,
        sort: SortOrder,
    ) -> dict:
        """The frozen ``GET /catalog/public/products`` payload."""
        live_prices = self._live_prices()
        filters = [Product.status == "PUBLISHED", Product.deleted_at.is_(None)]
        if keyword:
            # The escape character is applied to the *input* as well as named in the
            # LIKE, or a keyword containing ``%`` matches everything.
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
        total = int(
            self._session.execute(
                select(func.count())
                .select_from(Product)
                .join(live_prices, live_prices.c.product_id == Product.id)
                .where(*filters)
            ).scalar_one()
        )
        rows = (
            self._session.execute(
                self._published_products(live_prices, filters)
                .order_by(*order)
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
            .scalars()
            .all()
        )
        return {
            "items": [summary_data(row) for row in rows],
            "meta": page_meta(page=page, page_size=page_size, total=total).model_dump(),
        }

    def product_detail(self, product_id: int) -> dict:
        """The frozen ``GET /catalog/public/products/{id}`` payload."""
        product = (
            self._session.execute(
                select(Product).where(
                    Product.id == product_id,
                    Product.status == "PUBLISHED",
                    Product.deleted_at.is_(None),
                )
            )
            .scalars()
            .first()
        )
        if product is None:
            # An unpublished product is indistinguishable from a missing one: telling
            # the difference would expose a draft catalogue to an anonymous caller.
            raise ProductNotFoundError("published product not found")

        skus = active_skus(product)
        stock = self.stock_by_sku([sku.id for sku in skus])
        images = []
        for image in sorted(product.images, key=lambda row: (row.sort_order, row.id)):
            url = image_url(image)
            if url:
                images.append(
                    {"id": image.id, "url": url, "alt": image.alt_text, "sort_order": image.sort_order}
                )
        return {
            **summary_data(product),
            "subtitle": product.subtitle,
            "description": product.description,
            "category": (
                {"id": product.category.id, "name": product.category.name}
                if product.category
                else None
            ),
            "brand": (
                {"id": product.brand.id, "name": product.brand.name} if product.brand else None
            ),
            "images": images,
            "skus": [
                {
                    "id": sku.id,
                    "product_id": product.id,
                    "sku_code": sku.sku_code,
                    "specs": sku.attribute_snapshot or {},
                    "price_amount": sku.price_amount,
                    "original_price_amount": (
                        sku.market_price_amount if sku.market_price_amount > sku.price_amount else None
                    ),
                    "available_stock": stock.get(sku.id, 0),
                    "status": sku.status,
                }
                for sku in skus
            ],
            # Falls back to the denormalised ``max_price`` when the product has no
            # active SKU at all, so a published-but-unstocked product still reports the
            # range it was published with instead of zero.
            "max_price_amount": max(
                (sku.price_amount for sku in skus), default=product.max_price
            ),
            "created_at": product.created_at.isoformat(),
            "updated_at": product.updated_at.isoformat(),
        }

    def categories(self, *, page: int, page_size: int) -> dict:
        """The frozen ``GET /catalog/public/categories`` payload.

        Flat and paged rather than a nested tree, matching the frozen contract: the
        client builds the tree from ``parent_id``/``level``, so a category's children
        do not have to be loaded to render its siblings.
        """
        predicate = Category.deleted_at.is_(None)
        total = int(
            self._session.execute(
                select(func.count()).select_from(Category).where(predicate)
            ).scalar_one()
        )
        rows = (
            self._session.execute(
                select(Category)
                .where(predicate)
                .order_by(Category.sort_order, Category.id)
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
            .scalars()
            .all()
        )
        items = [
            {
                "id": row.id,
                "name": row.name,
                "parent_id": row.parent_id,
                "level": row.depth,
                "sort_order": row.sort_order,
            }
            for row in rows
        ]
        return {
            "items": items,
            "meta": page_meta(page=page, page_size=page_size, total=total).model_dump(),
        }

    def brands(self, *, page: int, page_size: int) -> dict:
        """The frozen ``GET /catalog/public/brands`` payload."""
        predicate = Brand.deleted_at.is_(None)
        total = int(
            self._session.execute(
                select(func.count()).select_from(Brand).where(predicate)
            ).scalar_one()
        )
        rows = (
            self._session.execute(
                select(Brand)
                .where(predicate)
                .order_by(Brand.sort_order, Brand.id)
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
            .scalars()
            .all()
        )
        items = [{"id": row.id, "name": row.name, "logo_url": None} for row in rows]
        return {
            "items": items,
            "meta": page_meta(page=page, page_size=page_size, total=total).model_dump(),
        }


__all__ = [
    "PublicCatalogService",
    "SortOrder",
    "active_skus",
    "image_url",
    "summary_data",
]
