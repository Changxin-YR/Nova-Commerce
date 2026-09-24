"""Frozen root-level product publication task routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.errors import envelope
from app.modules.catalog.api.admin import product_data
from app.modules.catalog.service import CatalogService
from app.modules.identity.dependencies import ConsolePrincipal
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


@router.post("/products/{product_id}/publish", summary="Publish a product")
def publish_product(product_id: int, principal: ConsolePrincipal, session: SessionDep) -> dict:
    product = CatalogService(session).transition(principal=principal, product_id=product_id, publish=True)
    return envelope(data=product_data(product))


@router.post("/products/{product_id}/unpublish", summary="Unpublish a product")
def unpublish_product(product_id: int, principal: ConsolePrincipal, session: SessionDep) -> dict:
    product = CatalogService(session).transition(principal=principal, product_id=product_id, publish=False)
    return envelope(data=product_data(product))
