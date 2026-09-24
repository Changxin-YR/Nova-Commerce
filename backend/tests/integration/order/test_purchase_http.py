"""Buyer HTTP path through login, catalog, preview, order, and payment attempt."""

from __future__ import annotations

import pytest

from .conftest import PASSWORD, Shop

pytestmark = pytest.mark.integration


def test_buyer_can_place_an_order_from_published_sku(client, shop: Shop) -> None:
    login = client.post(
        "/api/v1/auth/login",
        json={"username": shop.consumer_username, "password": PASSWORD},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

    catalog = client.get(f"/api/v1/catalog/public/products/{shop.product_id}")
    assert catalog.status_code == 200
    assert shop.sku_ids[0] in [sku["id"] for sku in catalog.json()["data"]["skus"]]

    body = {"items": [{"sku_id": shop.sku_ids[0], "quantity": 1}], "address_id": shop.address_id}
    preview = client.post("/api/v1/orders/preview", headers=headers, json=body)
    assert preview.status_code == 200, preview.text
    amount = preview.json()["data"]["payable_amount"]
    assert amount > 0

    request_id = shop.key("purchase-http")
    created = client.post(
        "/api/v1/orders",
        headers={**headers, "Idempotency-Key": request_id},
        json={**body, "client_request_id": request_id},
    )
    assert created.status_code == 200, created.text
    order = created.json()["data"]
    assert order["payable_amount"] == amount

    pay_id = shop.key("purchase-payment")
    payment = client.post(
        "/api/v1/payments/customer/payments",
        headers={**headers, "Idempotency-Key": pay_id},
        json={"order_no": order["order_no"], "channel": "MOCK", "client_request_id": pay_id},
    )
    assert payment.status_code == 200, payment.text
    assert payment.json()["data"]["amount"] == amount
