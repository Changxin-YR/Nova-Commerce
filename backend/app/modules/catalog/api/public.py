"""Published catalog reads; checkout still reprices from the database.

Transport only, and deliberately so: the storefront payloads are assembled by
:class:`~app.modules.catalog.public_service.PublicCatalogService`, which is also what
the interface contract tests exercise directly. Nothing in this file opens a query,
and the frozen wire shapes are unchanged - `envelope(data=<the service's dict>)`.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.catalog.public_service import PublicCatalogService
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


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
    return envelope(
        data=PublicCatalogService(session).list_products(
            page=page,
            page_size=page_size,
            keyword=keyword,
            category_id=category_id,
            brand_id=brand_id,
            min_price_amount=min_price_amount,
            max_price_amount=max_price_amount,
            sort=sort,
        )
    )


@router.get("/public/products/{product_id}", summary="Published product detail")
def product_detail(product_id: int, session: SessionDep) -> dict:
    return envelope(data=PublicCatalogService(session).product_detail(product_id))


@router.get("/public/categories", summary="Browse categories")
def categories(
    session: SessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 100,
) -> dict:
    return envelope(
        data=PublicCatalogService(session).categories(page=page, page_size=page_size)
    )


@router.get("/public/brands", summary="Browse brands")
def brands(
    session: SessionDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 100,
) -> dict:
    return envelope(data=PublicCatalogService(session).brands(page=page, page_size=page_size))
