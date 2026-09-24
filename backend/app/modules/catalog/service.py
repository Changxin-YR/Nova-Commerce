"""Merchant-scoped product writes and explicit publication transitions."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    BrandNotFoundError,
    CategoryNotFoundError,
    ConflictError,
    PermissionDeniedError,
    ProductAlreadyPublishedError,
    ProductNotFoundError,
    ProductStateInvalidError,
)
from app.modules.catalog.models import Brand, Category, Product, ProductSku
from app.modules.catalog.schemas import ProductCreate, ProductUpdate, SkuCreate
from app.modules.identity.enums import PermissionCode
from app.modules.identity.service import Principal
from app.shared.db.base import utc_now


class CatalogService:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _merchant(principal: Principal) -> int:
        if not principal.is_staff or principal.merchant_id is None:
            raise PermissionDeniedError("merchant staff account required")
        return principal.merchant_id

    def _owned(self, principal: Principal, product_id: int, *, lock: bool = False) -> Product:
        merchant_id = self._merchant(principal)
        query = select(Product).where(
            Product.id == product_id,
            Product.merchant_id == merchant_id,
            Product.deleted_at.is_(None),
        )
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        product = self.session.execute(query).scalar_one_or_none()
        if product is None:
            raise ProductNotFoundError("product not found")
        return product

    def _references(self, merchant_id: int, category_id: int | None, brand_id: int | None) -> None:
        if category_id is not None and self.session.execute(
            select(Category.id).where(
                Category.id == category_id,
                Category.merchant_id == merchant_id,
                Category.deleted_at.is_(None),
            )
        ).scalar_one_or_none() is None:
            raise CategoryNotFoundError("category not found")
        if brand_id is not None and self.session.execute(
            select(Brand.id).where(
                Brand.id == brand_id,
                Brand.merchant_id == merchant_id,
                Brand.deleted_at.is_(None),
            )
        ).scalar_one_or_none() is None:
            raise BrandNotFoundError("brand not found")

    def list_admin(
        self, *, principal: Principal, page: int, page_size: int,
        keyword: str | None, status: str | None,
    ) -> tuple[list[Product], int]:
        principal.require_permission(PermissionCode.PRODUCT_READ.value)
        merchant_id = self._merchant(principal)
        filters = [Product.merchant_id == merchant_id, Product.deleted_at.is_(None)]
        if keyword:
            escaped = keyword.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append(Product.name.like(f"%{escaped}%", escape="\\"))
        if status:
            filters.append(Product.status == status)
        total = int(self.session.execute(select(func.count()).select_from(Product).where(*filters)).scalar_one())
        rows = list(self.session.execute(
            select(Product).where(*filters)
            .order_by(Product.created_at.desc(), Product.id.desc())
            .limit(page_size).offset((page - 1) * page_size)
        ).scalars())
        return rows, total

    def detail(self, *, principal: Principal, product_id: int) -> Product:
        principal.require_permission(PermissionCode.PRODUCT_READ.value)
        return self._owned(principal, product_id)

    def _new_sku(self, *, merchant_id: int, product_id: int, body: SkuCreate) -> ProductSku:
        exists = self.session.execute(
            select(ProductSku.id).where(ProductSku.sku_code == body.sku_code)
        ).scalar_one_or_none()
        if exists is not None:
            raise ConflictError("SKU code already exists")
        return ProductSku(
            merchant_id=merchant_id,
            product_id=product_id,
            sku_no=f"S{uuid4().hex[:24]}",
            sku_code=body.sku_code,
            name=body.name,
            price_amount=body.price_amount,
            market_price_amount=body.original_price_amount,
            cost_amount=0,
            attribute_snapshot=body.specs,
            purchase_limit=body.purchase_limit,
            status="ACTIVE",
        )

    def _commit_unique(self) -> None:
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            if getattr(exc.orig, "args", ())[:1] == (1062,):
                raise ConflictError("catalog identifier already exists") from exc
            raise

    @staticmethod
    def _refresh_prices(product: Product) -> None:
        prices = [sku.price_amount for sku in product.skus if sku.deleted_at is None and sku.status == "ACTIVE"]
        product.min_price = min(prices, default=0)
        product.max_price = max(prices, default=0)

    def create(self, *, principal: Principal, body: ProductCreate) -> Product:
        principal.require_permission(PermissionCode.PRODUCT_WRITE.value)
        merchant_id = self._merchant(principal)
        self._references(merchant_id, body.category_id, body.brand_id)
        codes = [sku.sku_code for sku in body.skus]
        if len(codes) != len(set(codes)):
            raise ConflictError("duplicate SKU code in product")
        unique = uuid4().hex
        product = Product(
            merchant_id=merchant_id,
            product_no=f"P{unique[:24]}",
            slug=f"product-{unique}",
            name=body.title,
            subtitle=body.subtitle,
            description=body.description,
            category_id=body.category_id,
            brand_id=body.brand_id,
            status="DRAFT",
        )
        self.session.add(product)
        self.session.flush()
        for sku_body in body.skus:
            self.session.add(self._new_sku(merchant_id=merchant_id, product_id=product.id, body=sku_body))
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            if getattr(exc.orig, "args", ())[:1] == (1062,):
                raise ConflictError("SKU code already exists") from exc
            raise
        self.session.refresh(product)
        self._refresh_prices(product)
        self._commit_unique()
        return product

    def update(self, *, principal: Principal, product_id: int, body: ProductUpdate) -> Product:
        principal.require_permission(PermissionCode.PRODUCT_WRITE.value)
        product = self._owned(principal, product_id, lock=True)
        if product.status == "ARCHIVED":
            raise ProductStateInvalidError("archived product cannot be edited")
        changes = body.model_dump(exclude_unset=True)
        self._references(product.merchant_id, changes.get("category_id"), changes.get("brand_id"))
        for field, value in changes.items():
            setattr(product, "name" if field == "title" else field, value)
        self.session.commit()
        return product

    def add_sku(self, *, principal: Principal, product_id: int, body: SkuCreate) -> ProductSku:
        principal.require_permission(PermissionCode.PRODUCT_WRITE.value)
        product = self._owned(principal, product_id, lock=True)
        if product.status == "ARCHIVED":
            raise ProductStateInvalidError("archived product cannot receive SKUs")
        sku = self._new_sku(merchant_id=product.merchant_id, product_id=product.id, body=body)
        self.session.add(sku)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            if getattr(exc.orig, "args", ())[:1] == (1062,):
                raise ConflictError("SKU code already exists") from exc
            raise
        self.session.refresh(product)
        self._refresh_prices(product)
        self._commit_unique()
        return sku

    def transition(self, *, principal: Principal, product_id: int, publish: bool) -> Product:
        principal.require_permission(PermissionCode.PRODUCT_PUBLISH.value)
        product = self._owned(principal, product_id, lock=True)
        if publish:
            if product.status == "PUBLISHED":
                raise ProductAlreadyPublishedError("product already published")
            if product.status not in {"DRAFT", "UNPUBLISHED"}:
                raise ProductStateInvalidError("product cannot be published from this state")
            if not any(sku.deleted_at is None and sku.status == "ACTIVE" and sku.price_amount > 0 for sku in product.skus):
                raise ProductStateInvalidError("product needs an active priced SKU")
            self._refresh_prices(product)
            product.status = "PUBLISHED"
            product.published_at = utc_now()
        else:
            if product.status != "PUBLISHED":
                raise ProductStateInvalidError("only published products can be unpublished")
            product.status = "UNPUBLISHED"
            product.unpublished_at = utc_now()
        self.session.commit()
        return product
