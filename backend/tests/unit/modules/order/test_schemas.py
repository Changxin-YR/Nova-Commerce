"""The order wire schemas - §38's "business inputs only" rule, made executable.

Spec §38, §110, §2, §3, API_CONTRACT §14.2/§14.3.

The single most important test in this file is
:func:`test_a_client_cannot_name_its_own_price`. §38 says the server recomputes every
figure because "a client that can name its own price has bought the shop" - and the
enforcement is ``extra="forbid"``. Without a test, a later author adding a field, or
relaxing the config "to be lenient", would remove the protection silently: an ignored
key looks exactly like a working request from the client's side.

The second-most important is the timestamp encoding. Pydantic's default for a
``datetime`` is six fractional digits; §2 freezes three. Both parse in JavaScript,
which is exactly why it would go unnoticed for a long time.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.modules.order.schemas import (
    CreateOrderRequest,
    OrderDetailOut,
    OrderItemOut,
    OrderPreviewOut,
    OrderPreviewRequest,
    OrderSummaryOut,
    UtcTimestamp,
    iso_millis,
    page_meta,
)


def _valid_preview(**overrides) -> dict:
    body = {"items": [{"sku_id": 3, "quantity": 1}], "address_id": 9}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# §38 / §110: a client may not name a price
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "forbidden",
    [
        "unit_price",
        "price_amount",
        "promotion_discount_amount",
        "coupon_discount_amount",
        "allocated_discount_amount",
        "payable_amount",
        "original_amount",
        "shipping_amount",
        "paid_amount",
        "refunded_amount",
        "discount_amount",
    ],
)
def test_a_client_cannot_name_its_own_price(forbidden: str) -> None:
    """Every money field is rejected, not ignored.

    Ignoring it would be worse than rejecting it: the client would believe it had
    set the price and only discover otherwise from the response - or from a
    support ticket.
    """
    with pytest.raises(ValidationError) as caught:
        OrderPreviewRequest.model_validate(_valid_preview(**{forbidden: 1}))
    assert forbidden in str(caught.value)


@pytest.mark.parametrize(
    "server_owned", ["user_id", "merchant_id", "order_status", "payment_status", "order_no", "id"]
)
def test_a_client_cannot_set_server_owned_fields(server_owned: str) -> None:
    """§110 mass assignment: identity and status are resolved server-side."""
    with pytest.raises(ValidationError):
        OrderPreviewRequest.model_validate(_valid_preview(**{server_owned: 1}))


def test_a_line_carries_only_a_sku_and_a_quantity() -> None:
    with pytest.raises(ValidationError):
        OrderPreviewRequest.model_validate({"items": [{"sku_id": 3, "quantity": 1, "unit_price": 1}]})


def test_create_forbids_a_price_on_the_line_too() -> None:
    with pytest.raises(ValidationError):
        CreateOrderRequest.model_validate(
            {
                "items": [{"sku_id": 3, "quantity": 1, "payable_amount": 0}],
                "address_id": 9,
                "client_request_id": "abc",
            }
        )


# ---------------------------------------------------------------------------
# §14.3: client_request_id is required on create and absent from preview
# ---------------------------------------------------------------------------
def test_client_request_id_is_required_on_create() -> None:
    with pytest.raises(ValidationError) as caught:
        CreateOrderRequest.model_validate(_valid_preview())
    assert "client_request_id" in str(caught.value)


def test_client_request_id_is_not_part_of_preview() -> None:
    """A preview creates nothing, so there is nothing to de-duplicate."""
    with pytest.raises(ValidationError):
        OrderPreviewRequest.model_validate(_valid_preview(client_request_id="abc"))


def test_a_blank_client_request_id_is_refused() -> None:
    with pytest.raises(ValidationError):
        CreateOrderRequest.model_validate(_valid_preview(client_request_id="   "))


def test_client_request_id_is_stripped_not_merely_accepted() -> None:
    request = CreateOrderRequest.model_validate(_valid_preview(client_request_id="  abc  "))
    assert request.client_request_id == "abc"


def test_client_request_id_longer_than_the_column_is_refused() -> None:
    """64 characters is the column width; a longer value would truncate in MySQL."""
    with pytest.raises(ValidationError):
        CreateOrderRequest.model_validate(_valid_preview(client_request_id="x" * 65))


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------
def test_an_order_needs_at_least_one_line() -> None:
    with pytest.raises(ValidationError):
        OrderPreviewRequest.model_validate({"items": []})
    with pytest.raises(ValidationError):
        OrderPreviewRequest.model_validate({"items": [], "address_id": 9})


def test_a_non_positive_quantity_is_refused() -> None:
    for quantity in (0, -1):
        with pytest.raises(ValidationError):
            OrderPreviewRequest.model_validate(
                {"items": [{"sku_id": 3, "quantity": quantity}], "address_id": 9}
            )


def test_the_line_count_is_bounded() -> None:
    """Each line becomes a row lock, so an unbounded list is an unbounded
    transaction (MAX_ORDER_LINES)."""
    from app.modules.order.schemas import MAX_ORDER_LINES

    too_many = [{"sku_id": index + 1, "quantity": 1} for index in range(MAX_ORDER_LINES + 1)]
    with pytest.raises(ValidationError):
        OrderPreviewRequest.model_validate({"items": too_many, "address_id": 9})


def test_duplicate_skus_are_accepted_here_and_merged_later() -> None:
    """The schema does not merge - §14.2 says the merge happens before pricing.

    Accepting duplicates at the edge is deliberate: rejecting them would surface a
    client-side cart bug as a 422 on a basket the customer can legitimately have
    built, and the merge is a correctness step that belongs to the workflow.
    """
    request = OrderPreviewRequest.model_validate(
        {"items": [{"sku_id": 3, "quantity": 1}, {"sku_id": 3, "quantity": 2}], "address_id": 9}
    )
    assert len(request.items) == 2


# ---------------------------------------------------------------------------
# §2: the frozen timestamp encoding
# ---------------------------------------------------------------------------
def test_iso_millis_matches_the_frozen_example() -> None:
    assert iso_millis(datetime(2026, 9, 22, 23, 31, 7, 507000, tzinfo=UTC)) == (
        "2026-09-22T23:31:07.507Z"
    )


def test_iso_millis_truncates_rather_than_rounds() -> None:
    """Truncation, so the value never moves forward past the stored DATETIME(3)."""
    assert iso_millis(datetime(2026, 9, 22, 23, 31, 7, 507999, tzinfo=UTC)) == (
        "2026-09-22T23:31:07.507Z"
    )


def test_iso_millis_normalises_a_non_utc_offset() -> None:
    from datetime import timedelta, timezone

    plus_eight = timezone(timedelta(hours=8))
    assert iso_millis(datetime(2026, 9, 23, 7, 31, 7, 507000, tzinfo=plus_eight)) == (
        "2026-09-22T23:31:07.507Z"
    )


def test_iso_millis_treats_a_naive_value_as_utc() -> None:
    """§2 forbids a local time; normalising beats emitting one, and beats a 500."""
    naive = datetime(2026, 9, 22, 23, 31, 7, 507000)  # noqa: DTZ001 - the naive value is the input under test
    assert iso_millis(naive) == "2026-09-22T23:31:07.507Z"


def test_the_wire_field_uses_the_three_digit_encoding() -> None:
    """Pydantic's default is six digits; this asserts the override is actually wired."""
    summary = OrderSummaryOut(
        id=1,
        order_no="NV20260922000001",
        order_status="PENDING_PAYMENT",
        payment_status="UNPAID",
        fulfillment_status="UNFULFILLED",
        after_sale_status="NONE",
        original_amount=299900,
        promotion_discount_amount=0,
        coupon_discount_amount=0,
        shipping_amount=0,
        payable_amount=299900,
        paid_amount=0,
        refunded_amount=0,
        receiver_name="张*",
        receiver_phone="138****5678",
        created_at=datetime(2026, 9, 22, 23, 31, 7, 507000, tzinfo=UTC),
        paid_at=None,
        expires_at=None,
        item_count=1,
        first_item_name="Nova Phone 15 Pro",
    )
    dumped = summary.model_dump(mode="json")
    assert dumped["created_at"] == "2026-09-22T23:31:07.507Z"
    assert dumped["paid_at"] is None  # §2: null is explicit, never absent
    assert "paid_at" in dumped


def test_the_annotated_alias_exists_for_direct_use() -> None:
    assert UtcTimestamp is not None


# ---------------------------------------------------------------------------
# §3: the paging meta
# ---------------------------------------------------------------------------
def test_total_pages_is_zero_for_an_empty_result() -> None:
    """Otherwise the UI renders "1 of 0 pages" (§3)."""
    meta = page_meta(page=1, page_size=20, total=0)
    assert meta.model_dump() == {"page": 1, "page_size": 20, "total": 0, "total_pages": 0}


def test_total_pages_uses_ceiling_division() -> None:
    assert page_meta(page=1, page_size=20, total=137).total_pages == 7
    assert page_meta(page=1, page_size=20, total=20).total_pages == 1
    assert page_meta(page=1, page_size=20, total=21).total_pages == 2


# ---------------------------------------------------------------------------
# Status vocabulary guards on the response models
# ---------------------------------------------------------------------------
def test_a_summary_with_an_unknown_order_status_fails_loudly() -> None:
    """The stored value comes from a CHECK constraint, so an unknown one means the
    vocabulary drifted from the migration - and the frontend's exhaustive switch has
    no branch for it. Failing here beats emitting it."""
    with pytest.raises(ValidationError):
        OrderSummaryOut(
            id=1,
            order_no="NV1",
            order_status="SHIPPED",  # a fulfillment status, not an order status (§31)
            payment_status="UNPAID",
            fulfillment_status="UNFULFILLED",
            after_sale_status="NONE",
            original_amount=1,
            promotion_discount_amount=0,
            coupon_discount_amount=0,
            shipping_amount=0,
            payable_amount=1,
            paid_amount=0,
            refunded_amount=0,
            receiver_name="张*",
            receiver_phone="138****5678",
            created_at=datetime(2026, 9, 22, tzinfo=UTC),
            item_count=1,
            first_item_name="x",
        )


def test_an_item_with_an_unknown_after_sale_status_fails_loudly() -> None:
    with pytest.raises(ValidationError):
        OrderItemOut(
            id=1,
            product_id=1,
            sku_id=1,
            product_name="p",
            sku_name="s",
            unit_price=1,
            quantity=1,
            original_amount=1,
            promotion_discount_amount=0,
            coupon_discount_amount=0,
            allocated_discount_amount=0,
            payable_amount=1,
            refunded_amount=0,
            after_sale_status="WEIRD",
        )


# ---------------------------------------------------------------------------
# §6: OrderDetail is OrderSummary plus lines, shipments and the address
# ---------------------------------------------------------------------------
def test_order_detail_requires_the_server_owned_refundable_amount() -> None:
    """The frontend deriving this is the silent failure §6 exists to prevent, so the
    field is required rather than defaulted - a default of 0 would silently disable
    every refund affordance, which is precisely the bug being avoided."""
    detail_fields = OrderDetailOut.model_fields
    assert "refundable_amount" in detail_fields
    assert detail_fields["refundable_amount"].is_required()


def test_order_detail_carries_no_status_logs_and_no_raw_address_snapshot() -> None:
    """Phase 4 does not freeze a status-log shape, and §94 forbids shipping the
    unmasked snapshot next to the masked name."""
    fields = set(OrderDetailOut.model_fields)
    assert "status_logs" not in fields
    assert "address_snapshot" not in fields
    assert "postal_code" not in fields
    assert "full_address" in fields


def test_preview_out_carries_warnings_as_codes() -> None:
    preview = OrderPreviewOut(
        items=[],
        original_amount=0,
        promotion_discount_amount=0,
        coupon_discount_amount=0,
        shipping_amount=0,
        payable_amount=0,
        warnings=["PROMOTION_THRESHOLD_NOT_MET"],
    )
    assert preview.model_dump(mode="json")["warnings"] == ["PROMOTION_THRESHOLD_NOT_MET"]
