"""FG-10 (5/5) - the four read paths: ownership, filters, masking, server-owned figures.

Spec §94, §109, §114, API_CONTRACT §3/§6/§14.6.

The one that matters most is
:func:`test_a_stranger_gets_50003_and_never_403`. §109 is explicit: distinguishing
"not yours" from "does not exist" turns the endpoint into an existence oracle, so a
consumer asking for someone else's order must be told the order is not found. The
filter is applied **in the SQL**, so the foreign row is never fetched - which is tested
here from the outside, and is also why the repository takes ``user_id`` as a query
argument rather than checking after the load.

Ownership is a property of the principal, so the stranger is a principal whose
``user_id`` owns nothing. That needs no extra row, and it is exactly the case under
test: the query, not a post-load ``if``, is what must refuse.

The masking assertions run on **both** surfaces. An order list is the more attractive
exfiltration target, because one request returns many receivers.
"""

from __future__ import annotations

import pytest
from sqlalchemy import update

from app.core.errors import OrderNotFoundError, PermissionDeniedError, ValidationError
from app.modules.identity.enums import DataScope
from app.modules.identity.service import Principal
from app.modules.order.enums import OrderStatus
from app.modules.order.models import Order
from app.modules.order.service import OrderService
from app.modules.order.workflow import OrderLineInput
from app.shared.db.base import utc_now
from app.shared.db.session import get_session_factory

from .conftest import Shop

pytestmark = [pytest.mark.integration]


def _create(shop: Shop, *, suffix: str, quantity: int = 1):
    session = get_session_factory()()
    try:
        return OrderService(session).create_order(
            principal=shop.consumer,
            items=[OrderLineInput(shop.sku_ids[0], quantity)],
            address_id=shop.address_id,
            client_request_id=shop.client_request_id(suffix),
            idempotency_key=shop.key(suffix),
        )
    finally:
        session.close()


def _stranger(shop: Shop) -> Principal:
    return Principal(
        user_id=shop.consumer_id + 10_000,
        user_type="CONSUMER",
        merchant_id=None,
        roles=(),
        permissions=frozenset(),
        data_scope=DataScope.SELF,
        session_id="stranger",
        is_staff=False,
    )


# ---------------------------------------------------------------------------
# Ownership (§109)
# ---------------------------------------------------------------------------
def test_a_stranger_gets_50003_and_never_403(shop: Shop) -> None:
    created = _create(shop, suffix="own")

    session = get_session_factory()()
    try:
        with pytest.raises(OrderNotFoundError) as caught:
            OrderService(session).get_customer_order(
                principal=_stranger(shop), order_no=created.order.order_no
            )
        # Not 403: distinguishing "not yours" from "does not exist" would confirm the
        # order exists, which is the oracle §109 forbids.
        assert caught.value.status_code == 404
        assert int(caught.value.code) == 50_003
    finally:
        session.close()


def test_the_owner_can_read_their_own_order(shop: Shop) -> None:
    created = _create(shop, suffix="owner-read")
    session = get_session_factory()()
    try:
        order = OrderService(session).get_customer_order(
            principal=shop.consumer, order_no=created.order.order_no
        )
        assert order.order_no == created.order.order_no
        assert order.items[0].sku_id == shop.sku_ids[0]
    finally:
        session.close()


def test_a_stranger_sees_an_empty_list_not_somebody_elses_orders(shop: Shop) -> None:
    _create(shop, suffix="hidden")
    session = get_session_factory()()
    try:
        page = OrderService(session).list_customer_orders(principal=_stranger(shop))
        assert page.rows == ()
        assert page.total == 0
    finally:
        session.close()


def test_an_unknown_order_number_is_50003_for_its_owner_too(shop: Shop) -> None:
    session = get_session_factory()()
    try:
        with pytest.raises(OrderNotFoundError):
            OrderService(session).get_customer_order(
                principal=shop.consumer, order_no="NV19990101000001"
            )
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The paged envelope (§3)
# ---------------------------------------------------------------------------
def test_the_list_is_paged_and_ordered_newest_first(shop: Shop) -> None:
    first = _create(shop, suffix="page-1")
    second = _create(shop, suffix="page-2")
    third = _create(shop, suffix="page-3")

    session = get_session_factory()()
    try:
        service = OrderService(session)
        whole = service.list_customer_orders(principal=shop.consumer)
        assert whole.total == 3
        assert len(whole.rows) == 3
        # created_at DESC, id DESC: the newest create is first even when the three
        # share a millisecond, which they routinely do in a test.
        assert {row.order_no for row in whole.rows} == {
            first.order.order_no,
            second.order.order_no,
            third.order.order_no,
        }
        assert whole.rows[0].order_no == third.order.order_no

        page_one = service.list_customer_orders(principal=shop.consumer, page=1, page_size=2)
        page_two = service.list_customer_orders(principal=shop.consumer, page=2, page_size=2)
        assert len(page_one.rows) == 2
        assert len(page_two.rows) == 1
        assert page_one.total == page_two.total == 3
        assert {row.order_no for row in page_one.rows}.isdisjoint(
            {row.order_no for row in page_two.rows}
        )
    finally:
        session.close()


def test_an_empty_result_is_an_empty_page_not_a_404(shop: Shop) -> None:
    """§3: the empty state is driven by ``items.length === 0``, never by a missing
    field and never by a 404."""
    session = get_session_factory()()
    try:
        page = OrderService(session).list_customer_orders(
            principal=_stranger(shop), page=1, page_size=20
        )
        assert page.total == 0
        from app.modules.order.schemas import page_meta

        assert page_meta(page=1, page_size=20, total=page.total).model_dump() == {
            "page": 1,
            "page_size": 20,
            "total": 0,
            "total_pages": 0,
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Filters are validated against their own vocabulary
# ---------------------------------------------------------------------------
def test_a_fulfillment_status_is_not_accepted_as_an_order_status_filter(shop: Shop) -> None:
    """``SHIPPED`` is a *fulfillment* status (§31), and it is exactly the plausible
    mistake. Silently returning nothing for it would tell a customer they have no orders
    in the state they meant to filter by."""
    session = get_session_factory()()
    try:
        with pytest.raises(ValidationError) as caught:
            OrderService(session).list_customer_orders(
                principal=shop.consumer, order_status="SHIPPED"
            )
        assert caught.value.context["order_status"] == "SHIPPED"
        assert "PENDING_PAYMENT" in caught.value.context["allowed"]
    finally:
        session.close()


def test_an_unknown_admin_filter_is_refused(shop: Shop) -> None:
    session = get_session_factory()()
    try:
        with pytest.raises(ValidationError):
            OrderService(session).list_admin_orders(
                principal=shop.staff, payment_status="PAID_AND_REFUNDED"
            )
        with pytest.raises(ValidationError):
            OrderService(session).list_admin_orders(
                principal=shop.staff, fulfillment_status="PENDING"
            )
        with pytest.raises(ValidationError):
            OrderService(session).list_admin_orders(principal=shop.staff, order_no="   ")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Console reads (§14.6)
# ---------------------------------------------------------------------------
def test_the_admin_list_filters_by_order_status_and_order_no(shop: Shop) -> None:
    created = _create(shop, suffix="admin")

    session = get_session_factory()()
    try:
        service = OrderService(session)
        by_number = service.list_admin_orders(principal=shop.staff, order_no=created.order.order_no)
        assert by_number.total == 1
        assert by_number.rows[0].order_no == created.order.order_no

        pending = service.list_admin_orders(
            principal=shop.staff, order_status=OrderStatus.PENDING_PAYMENT.value
        )
        assert created.order.order_no in {row.order_no for row in pending.rows}

        completed = service.list_admin_orders(
            principal=shop.staff, order_status=OrderStatus.COMPLETED.value
        )
        assert created.order.order_no not in {row.order_no for row in completed.rows}

        by_payment = service.list_admin_orders(principal=shop.staff, payment_status="UNPAID")
        assert created.order.order_no in {row.order_no for row in by_payment.rows}

        by_fulfillment = service.list_admin_orders(
            principal=shop.staff, fulfillment_status="UNFULFILLED"
        )
        assert created.order.order_no in {row.order_no for row in by_fulfillment.rows}

        by_missing = service.list_admin_orders(principal=shop.staff, order_no="NV19990101000001")
        assert by_missing.total == 0
        assert by_missing.rows == ()
    finally:
        session.close()


def test_the_admin_detail_masks_the_receiver_too(shop: Shop) -> None:
    """§14.6: masked on **both** surfaces.

    The masking is applied on the way *out* - the service returns the row, and
    ``serializers.to_detail`` is what a client ever sees - so the assertion is on the
    serialized payload. The raw columns stay readable inside the process, which is what
    lets the cancel and (Phase 5) fulfilment paths use them.
    """
    created = _create(shop, suffix="admin-mask")
    session = get_session_factory()()
    try:
        order = OrderService(session).get_admin_order(
            principal=shop.staff, order_no=created.order.order_no
        )
        # The row itself is unmasked...
        assert order.receiver_name == "张三丰"
        assert order.address_snapshot["detail"] == "科技园路1号A座1801室"
        # ... and nothing a client receives is.
        from app.modules.order.serializers import to_detail, to_summary

        for payload in (
            to_detail(order).model_dump(mode="json"),
            to_summary(order).model_dump(mode="json"),
        ):
            assert payload["receiver_name"] == "张**"
            assert payload["receiver_phone"] == "138****5678"
            assert "张三丰" not in repr(payload)
            assert "13800005678" not in repr(payload)
        dumped = to_detail(order).model_dump(mode="json")
        assert dumped["full_address"].endswith("****")
        assert "1801室" not in repr(dumped)
    finally:
        session.close()


def test_the_admin_read_requires_the_order_read_permission(shop: Shop) -> None:
    """§8 puts this on the service, so a non-HTTP caller (Phase 9's tool gateway) cannot
    skip it by not going through a route decorator."""
    created = _create(shop, suffix="admin-perm")
    unprivileged = Principal(
        user_id=shop.staff_id,
        user_type="STAFF",
        merchant_id=shop.merchant_id,
        roles=(),
        permissions=frozenset(),
        data_scope=DataScope.MERCHANT,
        session_id="no-perm",
        is_staff=True,
    )

    session = get_session_factory()()
    try:
        with pytest.raises(PermissionDeniedError):
            OrderService(session).get_admin_order(
                principal=unprivileged, order_no=created.order.order_no
            )
        with pytest.raises(PermissionDeniedError):
            OrderService(session).list_admin_orders(principal=unprivileged)
    finally:
        session.close()


def test_a_merchant_scoped_console_cannot_read_another_merchants_order(shop: Shop) -> None:
    """The scope is applied **in the query**, so a foreign order is never fetched and
    cannot be confirmed to exist."""
    created = _create(shop, suffix="admin-scope")
    other_merchant_staff = Principal(
        user_id=shop.staff_id,
        user_type="STAFF",
        merchant_id=shop.merchant_id + 999_999,
        roles=("ORDER_OPERATOR",),
        permissions=frozenset({"order:read"}),
        data_scope=DataScope.MERCHANT,
        session_id="other-merchant",
        is_staff=True,
    )

    session = get_session_factory()()
    try:
        with pytest.raises(OrderNotFoundError):
            OrderService(session).get_admin_order(
                principal=other_merchant_staff, order_no=created.order.order_no
            )
        page = OrderService(session).list_admin_orders(principal=other_merchant_staff)
        assert created.order.order_no not in {row.order_no for row in page.rows}
    finally:
        session.close()


# ---------------------------------------------------------------------------
# The server-owned figures (§6)
# ---------------------------------------------------------------------------
def test_refundable_amount_is_paid_minus_refunded_and_never_negative(shop: Shop) -> None:
    """INV-005's figure, owned by the server. The frontend found that deriving it
    client-side silently disables every refund affordance (``undefined > 0`` is
    ``false``), so a money figure that decides whether a control renders belongs next to
    the rule it enforces."""
    from app.modules.order.serializers import to_detail

    created = _create(shop, suffix="refundable", quantity=2)
    expected_payable = shop.sku_prices[0] * 2

    session = get_session_factory()()
    try:
        order = OrderService(session).get_customer_order(
            principal=shop.consumer, order_no=created.order.order_no
        )
        # Nothing is paid yet: Phase 5 owns paid_amount, and seeding it from
        # payable_amount would make INV-005 unfalsifiable.
        assert order.paid_amount == 0
        assert to_detail(order).refundable_amount == 0
    finally:
        session.close()

    # Simulate a partial payment and partial refund, as Phase 5 would write them.
    session = get_session_factory()()
    try:
        session.execute(
            update(Order)
            .where(Order.order_no == created.order.order_no)
            .values(paid_amount=expected_payable, refunded_amount=500, paid_at=utc_now())
        )
        session.commit()
    finally:
        session.close()

    session = get_session_factory()()
    try:
        order = OrderService(session).get_customer_order(
            principal=shop.consumer, order_no=created.order.order_no
        )
        detail = to_detail(order)
        assert detail.refundable_amount == expected_payable - 500
        assert detail.paid_amount == expected_payable
        assert detail.refunded_amount == 500
    finally:
        session.close()

    # An over-refund (which INV-005 forbids, and whose CHECK would reject it) is floored
    # rather than surfacing as a negative balance.
    session = get_session_factory()()
    try:
        order = OrderService(session).get_customer_order(
            principal=shop.consumer, order_no=created.order.order_no
        )
        order.refunded_amount = expected_payable
        session.flush()
        from app.modules.order.serializers import to_detail as serialize

        assert serialize(order).refundable_amount == 0
        session.rollback()
    finally:
        session.close()


def test_the_summary_fields_are_present_and_backend_owned(shop: Shop) -> None:
    from app.modules.order.serializers import to_summary

    created = _create(shop, suffix="summary", quantity=3)
    session = get_session_factory()()
    try:
        order = OrderService(session).get_customer_order(
            principal=shop.consumer, order_no=created.order.order_no
        )
        summary = to_summary(order)
        assert summary.item_count == 3
        assert summary.first_item_name.startswith("Nova Phone 15 Pro")
        assert len(summary.first_item_name) <= 200
        # §6: the list row deliberately carries no lines.
        assert "items" not in summary.model_dump()
    finally:
        session.close()


def test_the_detail_carries_the_lines_and_the_masked_address(shop: Shop) -> None:
    from app.modules.order.serializers import to_detail

    created = _create(shop, suffix="detail", quantity=2)
    session = get_session_factory()()
    try:
        order = OrderService(session).get_customer_order(
            principal=shop.consumer, order_no=created.order.order_no
        )
        detail = to_detail(order)
        assert len(detail.items) == 1
        assert detail.items[0].quantity == 2
        assert detail.items[0].unit_price == shop.sku_prices[0]
        assert detail.items[0].payable_amount == shop.sku_prices[0] * 2
        # §6 explicitly allows [] here and §3 forbids null for a list.
        assert detail.shipments == []
        assert detail.full_address is not None
        assert detail.full_address.endswith("****")
        assert detail.cancel_reason is None
        assert detail.remark is None
    finally:
        session.close()
