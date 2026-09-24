"""Consumer discovery shows only coupon templates they can still claim."""

from __future__ import annotations

import pytest

from app.modules.marketing.coupon_schemas import CouponCreate
from app.modules.marketing.coupon_service import CouponService
from app.shared.db.session import session_scope

from .conftest import PASSWORD, Shop
from .test_marketing_coupons import _draft, _staff

pytestmark = pytest.mark.integration


def test_customer_discovers_and_claims_a_coupon_once(client, shop: Shop) -> None:
    draft = _draft(shop).model_copy(update={"total_quota": 2, "per_user_limit": 1})
    with session_scope() as session:
        service = CouponService(session)
        token = service.preview(principal=_staff(shop), draft=draft)["preview_token"]
        template = service.create(
            principal=_staff(shop),
            payload=CouponCreate.model_validate({**draft.model_dump(), "preview_token": token}),
        )
        template_id = template.id
        service.transition(principal=_staff(shop), template_id=template_id, publish=True)

    endpoint = "/api/v1/marketing/coupons/available"
    assert client.get(endpoint).status_code == 401
    staff_login = client.post(
        "/api/v1/auth/login",
        json={"username": shop.staff_username, "password": PASSWORD},
    )
    staff_headers = {"Authorization": f"Bearer {staff_login.json()['data']['access_token']}"}
    assert client.get(endpoint, headers=staff_headers).status_code == 403

    customer_login = client.post(
        "/api/v1/auth/login",
        json={"username": shop.consumer_username, "password": PASSWORD},
    )
    headers = {"Authorization": f"Bearer {customer_login.json()['data']['access_token']}"}
    available = client.get(endpoint, headers=headers)
    assert available.status_code == 200, available.text
    items = available.json()["data"]["items"]
    assert [row["id"] for row in items] == [template_id]
    assert items[0]["face_value_amount"] == draft.face_value_amount
    assert available.json()["data"]["meta"]["total"] == 1

    claimed = client.post(
        f"/api/v1/marketing/coupons/{template_id}/claim", headers=headers,
    )
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["data"]["status"] == "UNUSED"
    after = client.get(endpoint, headers=headers)
    assert after.status_code == 200
    assert after.json()["data"]["items"] == []
    assert after.json()["data"]["meta"]["total"] == 0
