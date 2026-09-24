"""Cookie session and owner-scoped address routes against real MySQL."""

from __future__ import annotations

import pytest

from app.modules.identity.models import UserAddress
from app.shared.db.session import session_scope

from .conftest import PASSWORD, Shop

pytestmark = pytest.mark.integration


def _login(http_client, shop: Shop) -> tuple[str, str]:
    response = http_client.post(
        "/api/v1/auth/login",
        json={"username": shop.consumer_username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert "refresh_token" not in payload
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    return payload["access_token"], http_client.cookies["nova_rt"]


def test_refresh_cookie_rotates_and_logout_revokes(client, shop: Shop) -> None:
    http_client = client
    access, original_cookie = _login(http_client, shop)
    me = http_client.get("/api/v1/auth/users/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert me.json()["data"]["id"] == shop.consumer_id

    cross_origin = http_client.post(
        "/api/v1/auth/refresh", headers={"Origin": "https://foreign.example"}
    )
    assert cross_origin.status_code == 403
    assert http_client.cookies["nova_rt"] == original_cookie

    response = http_client.post("/api/v1/auth/refresh")
    assert response.status_code == 200, response.text
    assert http_client.cookies["nova_rt"] != original_cookie
    assert "refresh_token" not in response.json()["data"]
    rotated_access = response.json()["data"]["access_token"]

    logout = http_client.post("/api/v1/auth/logout")
    assert logout.status_code == 200
    assert "nova_rt" not in http_client.cookies
    after = http_client.get(
        "/api/v1/auth/users/me", headers={"Authorization": f"Bearer {rotated_access}"}
    )
    assert after.status_code == 401


def test_address_routes_enforce_owner_scope(client, shop: Shop) -> None:
    http_client = client
    access, _ = _login(http_client, shop)
    headers = {"Authorization": f"Bearer {access}"}
    created = http_client.post(
        "/api/v1/auth/users/addresses",
        headers=headers,
        json={
            "receiver_name": "New Receiver", "receiver_phone": "13800000000",
            "province": "A", "city": "B", "district": "C", "detail": "Street 1",
        },
    )
    assert created.status_code == 200, created.text
    address_id = created.json()["data"]["id"]
    assert address_id in [row["id"] for row in http_client.get(
        "/api/v1/auth/users/addresses", headers=headers
    ).json()["data"]["items"]]

    with session_scope() as session:
        foreign = UserAddress(
            user_id=shop.staff_id, merchant_id=shop.merchant_id,
            receiver_name="Foreign", receiver_phone="13800000001",
            province="A", city="B", district="C", detail="Street 2",
        )
        session.add(foreign)
        session.flush()
        foreign_id = foreign.id
    denied = http_client.put(
        f"/api/v1/auth/users/addresses/{foreign_id}",
        headers=headers, json={"detail": "Changed"},
    )
    assert denied.status_code == 404

    null_update = http_client.put(
        f"/api/v1/auth/users/addresses/{address_id}", headers=headers,
        json={"receiver_name": None},
    )
    assert null_update.status_code == 422

    removed = http_client.delete(f"/api/v1/auth/users/addresses/{address_id}", headers=headers)
    assert removed.status_code == 200
