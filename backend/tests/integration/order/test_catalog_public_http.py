"""Published catalog wire shapes against committed MySQL fixture rows."""

from __future__ import annotations

import pytest

from app.modules.catalog.models import Product
from app.shared.db.session import session_scope

from .conftest import Shop

pytestmark = pytest.mark.integration


def test_published_search_detail_and_unpublish_visibility(client, shop: Shop) -> None:
    for resource in ("categories", "brands"):
        lookup = client.get(f"/api/v1/catalog/public/{resource}")
        assert lookup.status_code == 200
        assert set(lookup.json()["data"]) == {"items", "meta"}

    search = client.get("/api/v1/catalog/public/products", params={"keyword": "Nova Phone 15 Pro"})
    assert search.status_code == 200, search.text
    data = search.json()["data"]
    assert data["meta"]["total"] >= 1
    summary = next(row for row in data["items"] if row["id"] == shop.product_id)
    assert summary["min_price_amount"] == min(shop.sku_prices)
    filtered = client.get(
        "/api/v1/catalog/public/products",
        params={"keyword": "Nova Phone 15 Pro", "min_price_amount": 1000},
    )
    assert shop.product_id not in [row["id"] for row in filtered.json()["data"]["items"]]

    detail = client.get(f"/api/v1/catalog/public/products/{shop.product_id}")
    assert detail.status_code == 200, detail.text
    product = detail.json()["data"]
    assert {sku["id"] for sku in product["skus"]} == set(shop.sku_ids)
    assert all(sku["available_stock"] == 1000 for sku in product["skus"])
    assert all("cost_amount" not in sku for sku in product["skus"])

    with session_scope() as session:
        session.get(Product, shop.product_id).status = "UNPUBLISHED"
    hidden = client.get(f"/api/v1/catalog/public/products/{shop.product_id}")
    assert hidden.status_code == 404
