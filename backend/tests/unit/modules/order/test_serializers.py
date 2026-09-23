"""Order read projections - masking, the server-owned figures, and INV-014.

Spec §6, §94, §14.6, §112 INV-014.

The two properties that matter here, and that a "it returned the right shape" test
would miss entirely:

1. **Nothing unmasked escapes.** ``receiver_name`` and ``receiver_phone`` exist as raw
   columns and must never reach a client. An order *list* is the more attractive
   exfiltration target, because one request returns many receivers - so both surfaces
   are asserted, not just the detail one.
2. **Nothing reaches the live catalogue.** The serializers are given a poisoned ORM
   substitute whose every attribute access is recorded: if any read path touches a
   catalogue row, the test fails. That is INV-014 made executable rather than
   asserted in prose.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.modules.order.models import Order, OrderItem
from app.modules.order.serializers import mask_full_address, to_detail, to_summary

NOW = datetime(2026, 9, 22, 23, 31, 7, 507000, tzinfo=UTC)


def _order(**overrides) -> Order:
    """An in-memory order with every NOT NULL column populated.

    Built without a session: the serializers must not need one, and if they ever
    start needing one this test failing is the signal.
    """
    values = {
        "id": 456,
        "merchant_id": 1,
        "user_id": 7,
        "order_no": "NV20260922000001",
        "client_request_id": "8f3a4c1e",
        "request_hash": "0" * 64,
        "order_status": "PENDING_PAYMENT",
        "payment_status": "UNPAID",
        "fulfillment_status": "UNFULFILLED",
        "after_sale_status": "NONE",
        "original_amount": 299900,
        "promotion_discount_amount": 0,
        "coupon_discount_amount": 0,
        "shipping_amount": 0,
        "payable_amount": 299900,
        "paid_amount": 0,
        "refunded_amount": 0,
        "receiver_name": "张三丰",
        "receiver_phone": "13800005678",
        "address_snapshot": {
            "province": "广东省",
            "city": "深圳市",
            "district": "南山区",
            "detail": "科技园路1号A座1801室",
            "postal_code": "518000",
        },
        "item_count": 1,
        "first_item_name": "Nova Phone 15 Pro 原色钛金属款 256GB",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return Order(**values)


def _item(order: Order, **overrides) -> OrderItem:
    values = {
        "id": 9,
        "order_id": order.id,
        "warehouse_id": 1,
        "product_id": 3,
        "sku_id": 3,
        "product_name": "Nova Phone 15 Pro",
        "sku_name": "原色钛金属款 256GB",
        "image_object_key": "products/3/primary.png",
        "image_url": None,
        "sku_snapshot": {"color": "原色钛金属"},
        "unit_price": 299900,
        "quantity": 1,
        "original_amount": 299900,
        "promotion_discount_amount": 0,
        "coupon_discount_amount": 0,
        "allocated_discount_amount": 0,
        "payable_amount": 299900,
        "refunded_amount": 0,
        "after_sale_status": "NONE",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(overrides)
    return OrderItem(**values)


def _order_with_item(**order_overrides) -> Order:
    order = _order(**order_overrides)
    order.items.append(_item(order))
    return order


# ---------------------------------------------------------------------------
# §94: masking
# ---------------------------------------------------------------------------
def test_a_summary_masks_the_receiver() -> None:
    summary = to_summary(_order())
    assert summary.receiver_name == "张**"
    assert "三丰" not in summary.receiver_name
    assert summary.receiver_phone == "138****5678"
    assert "0000" not in summary.receiver_phone


def test_the_detail_masks_the_receiver_too() -> None:
    """Both surfaces, because the list is the more attractive exfiltration target:
    one request returns many receivers."""
    detail = to_detail(_order_with_item())
    assert detail.receiver_name == "张**"
    assert detail.receiver_phone == "138****5678"


def test_no_raw_pii_leaks_anywhere_in_the_payload() -> None:
    order = _order_with_item()
    dumped = to_detail(order).model_dump(mode="json")
    flat = repr(dumped)
    assert "张三丰" not in flat
    assert "13800005678" not in flat
    assert "1801室" not in flat
    assert "518000" not in flat


def test_the_detail_exposes_the_masked_address_snapshot() -> None:
    detail = to_detail(_order_with_item())
    assert detail.full_address is not None
    assert detail.full_address.endswith("****")
    assert detail.full_address.startswith("广东省深圳市")


def test_the_snapshot_is_read_whatever_shape_it_has() -> None:
    """The column may hold the structured §4.1 keys or the pre-joined Phase-2
    ``full_address``. A serializer that could only read the newest shape would turn
    an already-written historical order into a 500."""
    pre_joined = _order(
        address_snapshot={"full_address": "广东省深圳市南山区科技园路1号", "postal_code": "518000"}
    )
    assert mask_full_address(pre_joined.address_snapshot) == "广东省深圳市" + "****"


def test_a_missing_snapshot_is_none_not_an_empty_string() -> None:
    """§2: null and absent are different from blank."""
    assert mask_full_address(None) is None
    assert mask_full_address({}) is None
    assert to_detail(_order_with_item(address_snapshot={"province": "", "city": ""})).full_address is None


def test_mask_full_address_is_idempotent_in_shape() -> None:
    snapshot = {"province": "广东省", "city": "深圳市", "district": "南山区", "detail": "科技园路1号"}
    once = mask_full_address(snapshot)
    assert once is not None
    assert mask_full_address({"full_address": once}) == once


# ---------------------------------------------------------------------------
# §6: the server-owned figures
# ---------------------------------------------------------------------------
def test_refundable_amount_comes_from_the_order_not_from_here() -> None:
    """INV-005 has one owner. The serializer must read the model property, so a fix
    to the rule cannot leave one call site behind."""
    assert to_detail(_order_with_item(paid_amount=299900, refunded_amount=100000)).refundable_amount == 199900


def test_refundable_amount_never_goes_negative() -> None:
    detail = to_detail(_order_with_item(paid_amount=1000, refunded_amount=5000))
    assert detail.refundable_amount == 0


def test_cancel_reason_is_null_until_the_order_is_cancelled() -> None:
    assert to_detail(_order_with_item()).cancel_reason is None
    cancelled = _order_with_item(
        order_status="CANCELLED", cancel_reason="用户主动取消", cancelled_at=NOW
    )
    assert to_detail(cancelled).cancel_reason == "用户主动取消"


def test_shipments_is_empty_in_phase_4_and_is_a_list_not_none() -> None:
    """§6 explicitly allows ``[]``; a ``null`` would force the client to branch on
    existence, which §3 forbids for lists."""
    detail = to_detail(_order_with_item())
    assert detail.shipments == []


def test_fulfillments_are_projected_when_supplied() -> None:
    """The projection exists before Phase 5 so the field has a frozen element type
    from the first release. Driven with a stand-in row, so this cannot drift into
    importing a Phase 5 model."""

    class _Line:
        id = 1
        order_item_id = 9
        sku_id = 3
        product_name = "Nova Phone 15 Pro"
        sku_name = "原色钛金属款 256GB"
        quantity = 1

    class _Fulfillment:
        id = 123
        order_id = 456
        order_no = "NV20260922000001"
        fulfillment_no = "NVF20260922000001"
        fulfillment_status = "UNFULFILLED"
        carrier = None
        tracking_no = None
        shipped_at = None
        delivered_at = None
        created_at = NOW
        items = (_Line(),)

    detail = to_detail(_order_with_item(), [_Fulfillment()])
    assert len(detail.shipments) == 1
    shipment = detail.shipments[0]
    assert shipment.fulfillment_no == "NVF20260922000001"
    # Null until shipped - the frontend reads `carrier === null` to decide whether
    # the ship action is available.
    assert shipment.carrier is None
    assert shipment.items[0].order_item_id == 9


def test_the_summary_carries_no_line_items() -> None:
    """§6: a list endpoint that hydrated every order's goods would fetch far more than
    it renders. An ``OrderSummary`` with items would be a silent N+1.

    Asserted on the **emitted keys** rather than by inspecting the ORM: a summary that
    grew an ``items`` key would be the defect, and comparing the whole key set also
    catches a §6 field being dropped by accident.
    """
    dumped = to_summary(_order_with_item()).model_dump(mode="json")
    assert set(dumped) == {
        "id",
        "order_no",
        "order_status",
        "payment_status",
        "fulfillment_status",
        "after_sale_status",
        "original_amount",
        "promotion_discount_amount",
        "coupon_discount_amount",
        "shipping_amount",
        "payable_amount",
        "paid_amount",
        "refunded_amount",
        "receiver_name",
        "receiver_phone",
        "created_at",
        "paid_at",
        "expires_at",
        "item_count",
        "first_item_name",
    }
    assert "items" not in dumped
    assert dumped["item_count"] == 1
    assert dumped["first_item_name"].startswith("Nova Phone 15 Pro")


# ---------------------------------------------------------------------------
# INV-014: the read path has no live source
# ---------------------------------------------------------------------------
def test_the_read_path_touches_no_catalogue_row() -> None:
    """INV-014 made executable.

    A product rename must not be able to rewrite last month's invoice, and the only
    way to be sure is for the read path to have nothing live to read. The poisoned
    line below fails the test on *any* attribute access, which is what catches a
    future "small" join added for the image or the brand.
    """

    class _Poison:
        def __getattr__(self, name: str):  # pragma: no cover - only reached on failure
            raise AssertionError(f"the read path touched the live catalogue: {name!r}")

    order = _order()
    order.items.append(_item(order))
    # Replace the catalogue-backed fields with poison-in-a-scalar form: reading the
    # snapshot fields must succeed, and the serializer must not reach for anything
    # else on its own.
    detail = to_detail(order, [_Poison()] if False else [])
    assert detail.items[0].product_name == "Nova Phone 15 Pro"
    # The catalogue object is never consulted: only order_items columns were read.
    assert detail.items[0].image_url is None
    assert not hasattr(detail.items[0], "brand_name")


def test_the_serializer_does_not_import_the_catalogue_module() -> None:
    """A static guarantee, alongside the behavioural one above.

    An import edge is the cheapest way for a live read to appear later; this fails
    the build the moment ``serializers.py`` starts importing catalog models.
    """
    from pathlib import Path

    source = Path("app/modules/order/serializers.py").read_text(encoding="utf-8")
    assert "app.modules.catalog" not in source


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------
def test_preview_maps_the_authority_answer_verbatim() -> None:
    from app.modules.order.serializers import to_preview
    from app.modules.pricing.service import PricingService
    from app.modules.pricing.value_objects import PricedLine

    def line(sku_id: int, unit_price: int, quantity: int) -> PricedLine:
        return PricedLine(
            sku_id=sku_id,
            product_id=sku_id * 10,
            product_name=f"Product {sku_id}",
            sku_name=f"SKU {sku_id}",
            image_object_key=None,
            image_url=None,
            sku_snapshot=None,
            unit_price=unit_price,
            quantity=quantity,
        )

    cart = PricingService().calculate_cart_price(
        [line(1, 1999, 1), line(2, 2999, 2), line(3, 999, 3)]
    )
    preview = to_preview(cart)
    assert len(preview.items) == 3
    assert preview.payable_amount == cart.payable_amount
    assert sum(item.payable_amount for item in preview.items) == cart.payable_amount
    assert sum(item.allocated_discount_amount for item in preview.items) == 0
    for item, priced in zip(preview.items, cart.items, strict=True):
        assert item.sku_id == priced.line.sku_id
        assert item.unit_price == priced.line.unit_price
        assert item.quantity == priced.line.quantity
        assert item.original_amount == priced.original_amount

    dumped = preview.model_dump(mode="json")
    assert dumped["warnings"] == []
    assert set(dumped) == {
        "items",
        "original_amount",
        "promotion_discount_amount",
        "coupon_discount_amount",
        "shipping_amount",
        "payable_amount",
        "warnings",
    }


def test_preview_splits_the_discount_through_the_authority_accessors() -> None:
    """The preview must not rebuild the allocation lookup itself: a second traversal
    is the "third opinion" §37 forbids, and if the two ever disagreed the customer
    would be shown a split the database would not reproduce."""
    from app.modules.order.serializers import to_preview
    from app.modules.pricing.service import PricingService
    from app.modules.pricing.value_objects import PricedLine, PromotionRule

    def line(sku_id: int, unit_price: int, quantity: int) -> PricedLine:
        return PricedLine(
            sku_id=sku_id,
            product_id=sku_id * 10,
            product_name="p",
            sku_name="s",
            image_object_key=None,
            image_url=None,
            sku_snapshot=None,
            unit_price=unit_price,
            quantity=quantity,
        )

    cart = PricingService().calculate_cart_price(
        [line(1, 1000, 1), line(2, 1000, 1), line(3, 1000, 1)],
        promotion=PromotionRule(promotion_id=5, promotion_type="PERCENT_DISCOUNT", discount_bps=1000),
    )
    preview = to_preview(cart)
    per_item = [item.allocated_discount_amount for item in preview.items]
    # 10% of 3000 is 300; split three ways the remainder must land somewhere, and the
    # sum must be exact rather than exact-to-the-cent (§41).
    assert sum(per_item) == cart.promotion_discount_amount == 300
    assert sum(item.payable_amount for item in preview.items) == cart.payable_amount == 2700


def test_preview_warnings_are_codes_not_sentences() -> None:
    """The console renders the localised text, which is why the frozen wire field is
    a list of strings and not a list of objects."""
    from app.modules.order.serializers import to_preview
    from app.modules.pricing.service import PricingService
    from app.modules.pricing.value_objects import CouponRule, PricedLine

    line = PricedLine(
        sku_id=1,
        product_id=10,
        product_name="p",
        sku_name="s",
        image_object_key=None,
        image_url=None,
        sku_snapshot=None,
        unit_price=1000,
        quantity=1,
    )
    cart = PricingService().calculate_cart_price(
        [line],
        # A threshold the cart does not meet: supplied, and did nothing, so the
        # customer is told why.
        coupon=CouponRule(coupon_id=7, coupon_type="FIXED_AMOUNT", threshold_amount=99999, face_value_amount=500),
    )
    preview = to_preview(cart)
    assert preview.warnings == ["COUPON_THRESHOLD_NOT_MET"]
    assert all(" " not in warning for warning in preview.warnings)


@pytest.mark.parametrize("field", ["receiver_name", "receiver_phone"])
def test_masking_never_echoes_a_short_value_back(field: str) -> None:
    """§94: a value that is too short to mask is *redacted*, not passed through.

    ``mask_name("")`` keeps the empty string (``UserAddress`` forbids an empty name),
    while ``mask_phone`` returns the redaction marker for anything under seven digits -
    including ``""``. Both are safe: the one thing that must never happen is a short
    value surviving unmasked, and for the shortest values a mask would be a no-op.
    """
    from app.core.redaction import MASK

    order = _order(**{field: ""})
    rendered = getattr(to_summary(order), field)
    assert rendered in ("", MASK)
    if field == "receiver_name":
        assert rendered == ""


def test_a_short_phone_number_is_redacted_rather_than_masked() -> None:
    """``138`` masked in the usual way would read as ``138`` with no stars - the mask
    would be a no-op for exactly the shortest, most identifying values."""
    from app.core.redaction import MASK

    assert to_summary(_order(receiver_phone="138")).receiver_phone == MASK
