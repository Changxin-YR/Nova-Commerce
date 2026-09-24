"""Merchant product authoring, publication, and scope over real MySQL HTTP."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, text

from app.modules.catalog.models import Category, Product, ProductSku
from app.modules.identity.enums import PermissionCode
from app.modules.identity.models import Merchant, Permission
from app.modules.identity.repository import RoleRepository
from app.shared.db.session import session_scope

from .conftest import PASSWORD, Shop

pytestmark = pytest.mark.integration


def _staff_headers(client, shop: Shop) -> dict[str, str]:
    with session_scope() as session:
        role_id = session.execute(
            text("SELECT role_id FROM user_roles WHERE user_id = :id"), {"id": shop.staff_id}
        ).scalar_one()
        roles = RoleRepository(session)
        for code in (
            PermissionCode.PRODUCT_READ.value,
            PermissionCode.PRODUCT_WRITE.value,
            PermissionCode.PRODUCT_PUBLISH.value,
        ):
            permission = roles.get_permission_by_code(code)
            if permission is None:
                resource, action = code.split(":")
                permission = roles.add_permission(Permission(
                    code=code, resource=resource, action=action, description="Catalog fixture",
                ))
            roles.grant_permission(role_id=role_id, permission_id=permission.id)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": shop.staff_username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def test_merchant_creates_and_publishes_product(client, shop: Shop) -> None:
    headers = _staff_headers(client, shop)
    marker = uuid4().hex
    created_id: int | None = None
    try:
        created = client.post(
            "/api/v1/catalog/admin/products", headers=headers,
            json={
                "title": f"Catalog {marker}",
                "skus": [{
                    "sku_code": f"CAT-{marker}", "name": "Base", "price_amount": 12900,
                    "original_price_amount": 18900, "specs": {"colour": "black"},
                }],
            },
        )
        assert created.status_code == 200, created.text
        payload = created.json()["data"]
        created_id = payload["id"]
        assert payload["status"] == "DRAFT"
        assert payload["min_price_amount"] == 12900
        assert len(payload["skus"]) == 1
        assert "cost_amount" not in str(payload)

        duplicate = client.post(
            f"/api/v1/catalog/admin/products/{created_id}/skus", headers=headers,
            json={"sku_code": f"CAT-{marker}", "name": "Duplicate", "price_amount": 9900},
        )
        assert duplicate.status_code == 409
        added = client.post(
            f"/api/v1/catalog/admin/products/{created_id}/skus", headers=headers,
            json={
                "sku_code": f"CAT-B-{marker}", "name": "Plus", "price_amount": 15900,
                "original_price_amount": 16000,
            },
        )
        assert added.status_code == 200, added.text
        assert added.json()["data"]["price_amount"] == 15900
        assert client.put(
            f"/api/v1/catalog/admin/products/{created_id}", headers=headers,
            json={"title": None},
        ).status_code == 422
        edited = client.put(
            f"/api/v1/catalog/admin/products/{created_id}", headers=headers,
            json={"title": f"Edited {marker}"},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["data"]["title"] == f"Edited {marker}"

        listing = client.get("/api/v1/catalog/admin/products", headers=headers).json()["data"]
        assert created_id in [row["id"] for row in listing["items"]]
        assert client.get(f"/api/v1/catalog/public/products/{created_id}").status_code == 404

        published = client.post(f"/api/v1/products/{created_id}/publish", headers=headers)
        assert published.status_code == 200, published.text
        assert published.json()["data"]["status"] == "PUBLISHED"
        assert client.post(f"/api/v1/products/{created_id}/publish", headers=headers).json()["code"] == 30006
        storefront = client.get(f"/api/v1/catalog/public/products/{created_id}")
        assert storefront.status_code == 200, storefront.text
        assert {sku["price_amount"] for sku in storefront.json()["data"]["skus"]} == {12900, 15900}
        assert storefront.json()["data"]["original_price_amount"] == 18900

        unpublished = client.post(f"/api/v1/products/{created_id}/unpublish", headers=headers)
        assert unpublished.status_code == 200, unpublished.text
        assert client.get(f"/api/v1/catalog/public/products/{created_id}").status_code == 404
    finally:
        if created_id is not None:
            with session_scope() as session:
                session.execute(delete(ProductSku).where(ProductSku.product_id == created_id))
                session.execute(delete(Product).where(Product.id == created_id))


def test_product_scope_and_task_permissions(client, shop: Shop) -> None:
    limited_login = client.post(
        "/api/v1/auth/login",
        json={"username": shop.staff_username, "password": PASSWORD},
    )
    assert limited_login.status_code == 200
    limited_headers = {
        "Authorization": f"Bearer {limited_login.json()['data']['access_token']}"
    }
    assert client.get("/api/v1/catalog/admin/products", headers=limited_headers).status_code == 403
    assert client.post(
        f"/api/v1/products/{shop.product_id}/publish", headers=limited_headers,
    ).status_code == 403
    headers = _staff_headers(client, shop)
    marker = uuid4().hex
    with session_scope() as session:
        foreign_merchant = Merchant(code=f"CAT{marker[:16]}", name=f"Foreign {marker}")
        session.add(foreign_merchant)
        session.flush()
        foreign_id = foreign_merchant.id
        foreign_product = Product(
            merchant_id=foreign_id, product_no=f"P{marker[:24]}",
            slug=f"foreign-{marker}", name=f"Foreign {marker}", status="DRAFT",
        )
        session.add(foreign_product)
        foreign_category = Category(
            merchant_id=foreign_id, code=f"C{marker[:24]}", name="Foreign category",
        )
        session.add(foreign_category)
        session.flush()
        product_id = foreign_product.id
        category_id = foreign_category.id
    try:
        own_list = client.get("/api/v1/catalog/admin/products", headers=headers)
        assert own_list.status_code == 200
        assert product_id not in [row["id"] for row in own_list.json()["data"]["items"]]
        assert client.get(f"/api/v1/catalog/admin/products/{product_id}", headers=headers).status_code == 404
        assert client.post(f"/api/v1/products/{product_id}/publish", headers=headers).status_code == 404
        assert client.post(
            "/api/v1/catalog/admin/products", headers=headers,
            json={"title": "Wrong merchant category", "category_id": category_id},
        ).status_code == 404

        consumer = client.post(
            "/api/v1/auth/login", json={"username": shop.consumer_username, "password": PASSWORD},
        ).json()["data"]["access_token"]
        response = client.post(
            f"/api/v1/products/{shop.product_id}/publish",
            headers={"Authorization": f"Bearer {consumer}"},
        )
        assert response.status_code == 403
        assert client.put(
            f"/api/v1/catalog/admin/products/{shop.product_id}", headers=headers,
            json={"status": "PUBLISHED"},
        ).status_code == 422
    finally:
        with session_scope() as session:
            session.execute(delete(Product).where(Product.id == product_id))
            session.execute(delete(Category).where(Category.id == category_id))
            session.execute(delete(Merchant).where(Merchant.id == foreign_id))
