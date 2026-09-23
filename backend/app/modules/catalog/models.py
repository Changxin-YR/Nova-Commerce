"""Catalog domain models - the seven tables required by spec §25.

    categories, brands, products, product_skus, product_images,
    product_attributes, sku_attribute_values

The load-bearing requirement here is §25's last line: **product_images stores
``object_key``, not a local filesystem path** (ADR-011). A path column would tie
the catalogue to one container's disk and break the moment the API is scaled to
two replicas.

The second is INV-014: a product may be edited, withdrawn or repriced at any
time, and a historical order must be completely unaffected. That is enforced by
*materialising* the trade snapshot into ``order_items`` (Phase 4) rather than by
reading live catalogue rows from the order path. Nothing in this file needs to
know about it, which is the point - the protection lives at the boundary.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.shared.db.base import (
    Base,
    MerchantScopedMixin,
    PkMixin,
    SoftDeleteMixin,
    TimestampMixin,
    short_str,
    status_column,
)
from app.shared.db.types import BigIntUnsigned, DateTimeMS, MoneyMinor

# ---------------------------------------------------------------------------
# Enums as plain strings (spec §19: VARCHAR + Python Enum)
# ---------------------------------------------------------------------------
PRODUCT_STATUSES = ("DRAFT", "PUBLISHED", "UNPUBLISHED", "ARCHIVED")
SKU_STATUSES = ("ACTIVE", "INACTIVE", "DISCONTINUED")
ATTRIBUTE_VALUE_TYPES = ("TEXT", "NUMBER", "SELECT", "BOOLEAN")
IMAGE_ROLES = ("PRIMARY", "GALLERY", "DETAIL")

#: ``product_images`` bucket selector. Kept as a constant so the storage layer is
#: never handed a bucket name assembled ad hoc.
PRODUCT_IMAGE_BUCKET_KEY = "S3_BUCKET_PRODUCT_IMAGES"


class Category(Base, PkMixin, TimestampMixin, MerchantScopedMixin, SoftDeleteMixin):
    """A self-referencing category tree."""

    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("merchant_id", "code", name="uq_categories_merchant_code"),
        CheckConstraint("depth >= 0", name="depth_non_negative"),
        Index("ix_categories_parent_sort", "parent_id", "sort_order"),
    )

    parent_id: Mapped[int | None] = mapped_column(
        BigIntUnsigned, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    code: Mapped[str] = mapped_column(short_str(64), nullable=False)
    name: Mapped[str] = mapped_column(short_str(128), nullable=False)
    #: Denormalised depth so a tree render does not need a recursive query.
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    is_leaf: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")
    icon_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    products: Mapped[list[Product]] = relationship(back_populates="category", lazy="noload")


class Brand(Base, PkMixin, TimestampMixin, MerchantScopedMixin, SoftDeleteMixin):
    """A manufacturer / marque (3C: Apple, Huawei, Xiaomi, ...)."""

    __tablename__ = "brands"
    __table_args__ = (UniqueConstraint("merchant_id", "name", name="uq_brands_merchant_name"),)

    name: Mapped[str] = mapped_column(short_str(128), nullable=False)
    name_en: Mapped[str | None] = mapped_column(short_str(128), nullable=True)
    logo_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    products: Mapped[list[Product]] = relationship(back_populates="brand", lazy="noload")


class Product(Base, PkMixin, TimestampMixin, MerchantScopedMixin, SoftDeleteMixin):
    """An SPU - the sellable *concept*; the SKU is the sellable *unit*.

    Price lives on the SKU, never here. A SPU-level price is the classic modelling
    mistake that makes "the 256 GB model costs more" unrepresentable.
    """

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("merchant_id", "product_no", name="uq_products_merchant_no"),
        UniqueConstraint("merchant_id", "slug", name="uq_products_merchant_slug"),
        CheckConstraint(
            "status IN ('DRAFT','PUBLISHED','UNPUBLISHED','ARCHIVED')",
            name="status_valid",
        ),
        Index("ix_products_status_published", "status", "published_at"),
        Index("ix_products_category_status", "category_id", "status"),
        Index("ix_products_brand_status", "brand_id", "status"),
    )

    product_no: Mapped[str] = mapped_column(short_str(32), nullable=False)
    slug: Mapped[str] = mapped_column(short_str(160), nullable=False)
    name: Mapped[str] = mapped_column(short_str(200), nullable=False)
    subtitle: Mapped[str | None] = mapped_column(short_str(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    category_id: Mapped[int | None] = mapped_column(
        BigIntUnsigned, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    brand_id: Mapped[int | None] = mapped_column(
        BigIntUnsigned, ForeignKey("brands.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[str] = mapped_column(
        status_column(), nullable=False, default="DRAFT", server_default="DRAFT"
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    unpublished_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    # --- denormalised display/sort fields ---------------------------------
    #: Cached "from" price so a listing can sort and display without joining
    #: every SKU. It is a *cache*: PricingService is the authority, and the
    #: checkout path never reads this (§37, §38).
    min_price: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    max_price: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    primary_image_object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    sales_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    #: Soft metadata only: spec §19 permits JSON for snapshots/config/extension.
    #: Filterable facts must be real columns, never keys inside this blob.
    extra_json: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )

    category: Mapped[Category | None] = relationship(back_populates="products", lazy="joined")
    brand: Mapped[Brand | None] = relationship(back_populates="products", lazy="joined")
    skus: Mapped[list[ProductSku]] = relationship(
        back_populates="product", lazy="selectin", cascade="all, delete-orphan"
    )
    images: Mapped[list[ProductImage]] = relationship(
        back_populates="product", lazy="selectin", cascade="all, delete-orphan"
    )
    attributes: Mapped[list[ProductAttribute]] = relationship(
        back_populates="product", lazy="selectin", cascade="all, delete-orphan"
    )

    @property
    def is_published(self) -> bool:
        return self.status == "PUBLISHED"


class ProductSku(Base, PkMixin, TimestampMixin, MerchantScopedMixin, SoftDeleteMixin):
    """A concrete sellable unit, and the **only** place a price lives.

    ``price_amount`` is BIGINT minor units (spec §19). ``market_price_amount`` is
    the struck-through reference price; ``cost_amount`` is the private landed
    cost - it must never be exposed to a consumer-facing response, which the
    schema makes possible but not automatic, so the service layer is responsible
    for keeping it out of public DTOs.
    """

    __tablename__ = "product_skus"
    __table_args__ = (
        UniqueConstraint("merchant_id", "sku_no", name="uq_product_skus_merchant_no"),
        UniqueConstraint("sku_code", name="uq_product_skus_code"),
        CheckConstraint("price_amount >= 0", name="price_non_negative"),
        CheckConstraint("cost_amount >= 0", name="cost_non_negative"),
        CheckConstraint("market_price_amount >= 0", name="market_price_non_negative"),
        CheckConstraint("status IN ('ACTIVE','INACTIVE','DISCONTINUED')", name="status_valid"),
        Index("ix_product_skus_product_status", "product_id", "status"),
    )

    product_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )

    sku_no: Mapped[str] = mapped_column(short_str(32), nullable=False)
    #: Machine-readable spec signature, e.g. ``iphone15-256g-black``. Used to match
    #: a cart line to a SKU without relying on the numeric id.
    sku_code: Mapped[str] = mapped_column(short_str(96), nullable=False)
    name: Mapped[str] = mapped_column(short_str(200), nullable=False)

    price_amount: Mapped[int] = mapped_column(MoneyMinor, nullable=False)
    market_price_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )
    #: PRIVATE. Never returned on a consumer endpoint.
    cost_amount: Mapped[int] = mapped_column(
        MoneyMinor, nullable=False, default=0, server_default="0"
    )

    status: Mapped[str] = mapped_column(
        status_column(), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    #: Per-SKU purchase limit. 0 means unlimited. Bounds a single order so one
    #: buyer cannot drain stock in a single transaction.
    purchase_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    weight_grams: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    #: Materialised snapshot of this SKU's attribute values, e.g.
    #: ``{"颜色": "原色钛金属", "容量": "256GB"}``. Denormalised deliberately: the
    #: order snapshot (INV-014) needs the values as they were at purchase time,
    #: and joining live attribute rows on the order path would make historical
    #: orders change when an attribute is renamed.
    attribute_snapshot: Mapped[dict | None] = mapped_column(
        JSON, nullable=True
    )

    product: Mapped[Product] = relationship(back_populates="skus", lazy="joined")
    attribute_values: Mapped[list[SkuAttributeValue]] = relationship(
        back_populates="sku", lazy="selectin", cascade="all, delete-orphan"
    )

    @property
    def display_price(self) -> str:
        from app.shared.db.types import money_to_display

        return money_to_display(self.price_amount)


class ProductImage(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    """An image held in object storage.

    Spec §25: stores ``object_key``, **not** a filesystem path, and spec §20
    requires ``checksum``/``content_type``/``size`` alongside it so the row can be
    verified against the bucket rather than trusted.
    """

    __tablename__ = "product_images"
    __table_args__ = (
        UniqueConstraint("object_key", name="uq_product_images_object_key"),
        CheckConstraint("role IN ('PRIMARY','GALLERY','DETAIL')", name="role_valid"),
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        Index("ix_product_images_product_role_sort", "product_id", "role", "sort_order"),
        CheckConstraint(
            "object_key NOT LIKE '/%' AND object_key NOT LIKE '%..%'",
            name="object_key_relative_and_safe",
        ),
    )

    product_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: NULL means the image belongs to the product as a whole rather than one SKU.
    sku_id: Mapped[int | None] = mapped_column(
        BigIntUnsigned, ForeignKey("product_skus.id", ondelete="CASCADE"), nullable=True
    )

    bucket: Mapped[str] = mapped_column(short_str(96), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str] = mapped_column(short_str(96), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigIntUnsigned, nullable=False)

    role: Mapped[str] = mapped_column(
        status_column(), nullable=False, default="GALLERY", server_default="GALLERY"
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    alt_text: Mapped[str | None] = mapped_column(short_str(255), nullable=True)

    product: Mapped[Product] = relationship(back_populates="images", lazy="noload")


class ProductAttribute(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    """A named attribute of a product, e.g. 屏幕尺寸 / 处理器.

    ``is_variant_axis`` marks the attributes that SKUs differ along. That single
    flag is what makes the SKU selector (colour / capacity / version) derivable
    from data instead of hardcoded per category.
    """

    __tablename__ = "product_attributes"
    __table_args__ = (
        UniqueConstraint("product_id", "name", name="uq_product_attributes_product_name"),
        CheckConstraint(
            "value_type IN ('TEXT','NUMBER','SELECT','BOOLEAN')", name="value_type_valid"
        ),
        Index("ix_product_attributes_product_variant", "product_id", "is_variant_axis"),
    )

    product_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(short_str(96), nullable=False)
    value_type: Mapped[str] = mapped_column(
        status_column(), nullable=False, default="TEXT", server_default="TEXT"
    )
    is_variant_axis: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    is_searchable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    unit: Mapped[str | None] = mapped_column(short_str(24), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    product: Mapped[Product] = relationship(back_populates="attributes", lazy="noload")
    values: Mapped[list[SkuAttributeValue]] = relationship(
        back_populates="attribute", lazy="selectin", cascade="all, delete-orphan"
    )


class SkuAttributeValue(Base, PkMixin, TimestampMixin):
    """The value of one attribute for one SKU - the cell of the variant matrix."""

    __tablename__ = "sku_attribute_values"
    __table_args__ = (
        UniqueConstraint("sku_id", "attribute_id", name="uq_sku_attribute_values_pair"),
        Index("ix_sku_attribute_values_attribute", "attribute_id"),
    )

    sku_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("product_skus.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attribute_id: Mapped[int] = mapped_column(
        BigIntUnsigned,
        ForeignKey("product_attributes.id", ondelete="CASCADE"),
        nullable=False,
    )
    value: Mapped[str] = mapped_column(short_str(200), nullable=False)
    #: Optional per-value swatch (a hex colour for a colour axis).
    display_value: Mapped[str | None] = mapped_column(short_str(64), nullable=True)

    sku: Mapped[ProductSku] = relationship(back_populates="attribute_values", lazy="noload")
    attribute: Mapped[ProductAttribute] = relationship(back_populates="values", lazy="joined")


__all__ = [
    "ATTRIBUTE_VALUE_TYPES",
    "IMAGE_ROLES",
    "PRODUCT_IMAGE_BUCKET_KEY",
    "PRODUCT_STATUSES",
    "SKU_STATUSES",
    "Brand",
    "Category",
    "Product",
    "ProductAttribute",
    "ProductImage",
    "ProductSku",
    "SkuAttributeValue",
]
