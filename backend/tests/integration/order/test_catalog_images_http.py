"""Merchant image upload crosses HTTP, MySQL, and real MinIO."""

from __future__ import annotations

import base64
import hashlib

import pytest
from sqlalchemy import delete, select, update

from app.core.config import get_settings
from app.modules.catalog.models import Product, ProductImage
from app.shared.db.session import session_scope
from app.shared.storage.backends.minio_backend import MinioObjectStorage
from app.shared.storage.factory import get_object_storage, set_object_storage

from .conftest import PASSWORD, Shop
from .test_catalog_admin_http import _staff_headers

pytestmark = pytest.mark.integration

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6km8AAAAASUVORK5CYII="
)


def test_product_image_upload_is_scoped_and_persisted_in_minio(client, shop: Shop) -> None:
    storage_before = get_object_storage()
    storage = MinioObjectStorage(get_settings())
    storage.ensure_buckets()
    set_object_storage(storage)
    prefix = f"products/{shop.merchant_id}/{shop.product_id}/"
    endpoint = f"/api/v1/catalog/admin/products/{shop.product_id}/images"
    try:
        limited_login = client.post(
            "/api/v1/auth/login",
            json={"username": shop.staff_username, "password": PASSWORD},
        )
        assert limited_login.status_code == 200
        limited = {"Authorization": f"Bearer {limited_login.json()['data']['access_token']}"}
        denied = client.post(
            endpoint, headers=limited,
            files={"file": ("front.png", PNG_BYTES, "image/png")},
            data={"role": "PRIMARY"},
        )
        assert denied.status_code == 403

        headers = _staff_headers(client, shop)
        invalid = client.post(
            endpoint, headers=headers,
            files={"file": ("front.png", b"not an image", "image/png")},
            data={"role": "PRIMARY"},
        )
        assert invalid.status_code == 400
        assert invalid.json()["code"] == 30008

        missing = client.post(
            "/api/v1/catalog/admin/products/999999999999/images", headers=headers,
            files={"file": ("front.png", PNG_BYTES, "image/png")},
            data={"role": "PRIMARY"},
        )
        assert missing.status_code == 404

        response = client.post(
            endpoint, headers=headers,
            files={"file": ("front.png", PNG_BYTES, "image/png")},
            data={"role": "PRIMARY", "alt_text": "Front view"},
        )
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["role"] == "PRIMARY"
        assert data["alt"] == "Front view"
        assert data["url"].startswith(get_settings().S3_PUBLIC_ENDPOINT)
        with session_scope() as session:
            image = session.execute(select(ProductImage).where(ProductImage.id == data["id"])).scalar_one()
            assert image.object_key.startswith(prefix)
            assert image.bucket == storage.bucket_product_images
            assert image.checksum == hashlib.sha256(PNG_BYTES).hexdigest()
            assert image.size_bytes == len(PNG_BYTES)
            assert storage.get_object(bucket=image.bucket, key=image.object_key) == PNG_BYTES
            product = session.get(Product, shop.product_id)
            assert product is not None
            assert product.primary_image_object_key == image.object_key
            assert sum(row.role == "PRIMARY" for row in product.images) == 1

        storefront = client.get(f"/api/v1/catalog/public/products/{shop.product_id}")
        assert storefront.status_code == 200, storefront.text
        assert storefront.json()["data"]["cover_url"] is not None
        assert any(row["id"] == data["id"] for row in storefront.json()["data"]["images"])
    finally:
        try:
            with session_scope() as session:
                uploaded = list(session.execute(select(ProductImage).where(
                    ProductImage.product_id == shop.product_id,
                    ProductImage.object_key.like(f"{prefix}%"),
                )).scalars())
                for image in uploaded:
                    storage.delete_object(bucket=image.bucket, key=image.object_key)
                session.execute(delete(ProductImage).where(ProductImage.id.in_([row.id for row in uploaded])))
                session.execute(update(ProductImage).where(
                    ProductImage.product_id == shop.product_id,
                ).values(role="PRIMARY"))
                session.execute(update(Product).where(Product.id == shop.product_id).values(
                    primary_image_object_key=None,
                ))
        finally:
            set_object_storage(storage_before)
