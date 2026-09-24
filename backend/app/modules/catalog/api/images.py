"""Merchant image upload backed by object storage and catalog metadata."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import PayloadTooLargeError, envelope
from app.modules.catalog.api.public import _image_url
from app.modules.catalog.images import CatalogImageService
from app.modules.identity.dependencies import ConsolePrincipal
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


@router.post("/admin/products/{product_id}/images", summary="Upload a product image")
def upload_product_image(
    product_id: int,
    principal: ConsolePrincipal,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    role: Annotated[Literal["PRIMARY", "GALLERY", "DETAIL"], Form()] = "GALLERY",
    alt_text: Annotated[str | None, Form(max_length=255)] = None,
) -> dict:
    limit = get_settings().S3_MAX_UPLOAD_BYTES
    content = file.file.read(limit + 1)
    if len(content) > limit:
        raise PayloadTooLargeError("product image exceeds upload limit")
    image = CatalogImageService(session).upload(
        principal=principal,
        product_id=product_id,
        filename=file.filename or "upload",
        declared_content_type=file.content_type or "",
        content=content,
        role=role,
        alt_text=alt_text,
    )
    return envelope(data={
        "id": image.id,
        "url": _image_url(image),
        "alt": image.alt_text,
        "role": image.role,
        "sort_order": image.sort_order,
    })
