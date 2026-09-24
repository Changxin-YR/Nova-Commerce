"""Persist validated product images with a best-effort object rollback."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from urllib3.exceptions import HTTPError

from app.core.config import get_settings
from app.core.errors import (
    ImageUploadRejectedError,
    ObjectStorageUnavailableError,
    ProductStateInvalidError,
)
from app.core.logging import get_logger
from app.modules.catalog.models import ProductImage
from app.modules.catalog.service import CatalogService
from app.modules.identity.enums import PermissionCode
from app.modules.identity.service import Principal
from app.shared.storage.factory import get_object_storage
from app.shared.storage.port import (
    StorageError,
    StorageObjectTooLargeError,
    StorageUnsupportedContentTypeError,
    build_object_key,
    guess_content_type,
    validate_upload,
)

IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp", "image/avif"})
logger = get_logger(__name__)


def _verified_image_type(filename: str, declared: str, content: bytes) -> str:
    try:
        content_type = validate_upload(
            filename=filename,
            declared_content_type=declared,
            head=content[:32],
            size=len(content),
            max_bytes=get_settings().S3_MAX_UPLOAD_BYTES,
        )
    except (StorageObjectTooLargeError, StorageUnsupportedContentTypeError) as exc:
        raise ImageUploadRejectedError(str(exc)) from exc
    extension_type = guess_content_type(filename)
    if content_type not in IMAGE_TYPES or extension_type != content_type:
        raise ImageUploadRejectedError("image filename and content type must agree")
    signatures = {
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/gif": content.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
        "image/avif": content[4:12] in (b"ftypavif", b"ftypavis"),
    }
    if not signatures.get(content_type):
        raise ImageUploadRejectedError("image bytes do not match the declared format")
    return content_type


class CatalogImageService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upload(
        self, *, principal: Principal, product_id: int, filename: str,
        declared_content_type: str, content: bytes, role: str, alt_text: str | None,
    ) -> ProductImage:
        principal.require_permission(PermissionCode.PRODUCT_WRITE.value)
        # Check ownership before I/O, then take the row lock only for the short
        # metadata transaction. A MinIO request must not hold a product lock.
        product = CatalogService(self.session)._owned(principal, product_id)
        if product.status == "ARCHIVED":
            raise ProductStateInvalidError("archived product cannot receive images")
        content_type = _verified_image_type(filename, declared_content_type, content)
        storage = get_object_storage()
        bucket = storage.bucket_product_images
        key = build_object_key(
            prefix=f"products/{product.merchant_id}", filename=filename,
            owner_id=product.id, unique=uuid4().hex,
        )
        try:
            stored = storage.put_object(
                bucket=bucket, key=key, data=content, content_type=content_type,
            )
        except (StorageError, HTTPError, OSError) as exc:
            raise ObjectStorageUnavailableError("product image store failed") from exc
        try:
            product = CatalogService(self.session)._owned(principal, product_id, lock=True)
            if product.status == "ARCHIVED":
                raise ProductStateInvalidError("archived product cannot receive images")
            if role == "PRIMARY":
                for previous in self.session.execute(select(ProductImage).where(
                    ProductImage.product_id == product.id,
                    ProductImage.role == "PRIMARY",
                )).scalars():
                    previous.role = "GALLERY"
                product.primary_image_object_key = key
            image = ProductImage(
                merchant_id=product.merchant_id,
                product_id=product.id,
                bucket=stored.bucket,
                object_key=stored.object_key,
                checksum=stored.checksum,
                content_type=stored.content_type,
                size_bytes=stored.size,
                role=role,
                alt_text=alt_text,
            )
            self.session.add(image)
            self.session.commit()
        except Exception:
            self.session.rollback()
            # The row did not commit; remove the freshly written object.
            try:
                storage.delete_object(bucket=bucket, key=key)
            except Exception as cleanup_error:  # noqa: BLE001 - transport exceptions vary by backend
                logger.warning(
                    "product image rollback could not remove object",
                    error_type=type(cleanup_error).__name__,
                )
            raise
        return image
