"""The payment serializers - ORM rows to the frozen wire shape.

Spec references: API_CONTRACT 15.2, INV-005, section 2, section 14.6.

## Why these run against a stand-in object rather than a database row

The serializer is deliberately written against **attribute access only** - it imports no
model class - so it can be tested without a session, a fixture or a database. That is not
convenience: it is the property that makes the read path unable to break when a model
changes, and a test that needed a real row would not exercise it.

The two assertions that matter most:

* :func:`test_pay_url_is_present_only_for_the_mock_channel` - section 15.2 freezes "present
  only for ``channel = "MOCK"``". A real provider's redirect URL is produced client-side by
  the provider's SDK, so echoing one from this API would mean the server had *invented* a
  payment URL, which is the one thing in this domain that must never be approximated.
* :func:`test_refundable_amount_is_read_from_the_model_not_recomputed` - INV-005 has one
  owner. A serializer that computed ``paid - refunded`` would be a second implementation of
  the refund cap, and the one that goes silently wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from app.core.redaction import MASK
from app.modules.payment.providers import PAYLOAD_REDACTED
from app.modules.payment.serializers import (
    MOCK_PAY_URL_TEMPLATE,
    pay_url_for,
    to_callback_ack,
    to_payment,
    to_payment_create,
    to_payment_list,
)

pytestmark = pytest.mark.unit

CREATED = datetime(2026, 9, 22, 23, 31, 7, 507000, tzinfo=UTC)


@dataclass
class Row:
    """A payment row as the serializer sees it: attributes, and nothing else.

    A dataclass rather than a mock framework, because the test should fail if the
    serializer reaches for anything the real model does not have - and a ``Mock`` would
    silently answer for attributes that do not exist, which is precisely the bug this
    guards against.
    """

    id: int = 41
    payment_no: str = "NVPAY20260923000041"
    order_id: int = 456
    order_no: str = "NV20260922000001"
    channel: str = "MOCK"
    status: str = "PAYING"
    amount: int = 279900
    paid_amount: int = 0
    refunded_amount: int = 0
    external_transaction_no: str | None = None
    idempotency_key: str = "key-1"
    expires_at: datetime | None = None
    paid_at: datetime | None = None
    created_at: datetime = CREATED
    updated_at: datetime | None = None
    #: The model's own derived property; the serializer must read it, not recompute it.
    refundable_amount: int = field(default=0)

    def __post_init__(self) -> None:
        # Mirrors ``Payment.refundable_amount``: floored at zero, because ``paid_amount``
        # and ``refunded_amount`` are written by different code paths and a transient
        # ordering that made the difference negative must not reach a customer as a
        # negative refundable balance.
        if "refundable_amount" not in self.__dataclass_fields__ or self.refundable_amount == 0:
            self.refundable_amount = max(self.paid_amount - self.refunded_amount, 0)


def test_the_frozen_shape_is_produced_field_for_field() -> None:
    out = to_payment(Row())
    dumped = out.model_dump(mode="json")

    assert dumped["id"] == 41
    assert dumped["payment_no"] == "NVPAY20260923000041"
    assert dumped["order_id"] == 456
    assert dumped["order_no"] == "NV20260922000001"
    assert dumped["channel"] == "MOCK"
    assert dumped["status"] == "PAYING"
    assert dumped["amount"] == 279900
    assert dumped["paid_amount"] == 0
    assert dumped["refunded_amount"] == 0
    assert dumped["refundable_amount"] == 0
    assert dumped["external_transaction_no"] is None
    assert dumped["created_at"] == "2026-09-22T23:31:07.507Z"


def test_the_column_name_is_kept_on_the_wire() -> None:
    """``external_transaction_no``, not ``transaction_no``.

    Section 15.2's example uses the column's own name, so there is exactly one spelling to
    grep for across the migration, the model and the response. A rename at the edge would
    be a second name for a money fact.
    """
    out = to_payment(Row(external_transaction_no="ALI-99887766"))
    assert out.external_transaction_no == "ALI-99887766"
    assert "transaction_no" not in out.model_dump()


@pytest.mark.parametrize("channel", ["ALIPAY", "WECHAT"])
def test_pay_url_is_absent_for_a_real_provider(channel: str) -> None:
    """The frozen rule: present only for MOCK.

    Emitting a URL for a real provider would mean this API had invented a payment
    endpoint; a client that followed it would be sent to a page that does not exist, at the
    exact moment the customer is trying to pay.
    """
    assert to_payment(Row(channel=channel)).pay_url is None
    assert pay_url_for(channel, "NVPAY1") is None


def test_pay_url_is_present_only_for_the_mock_channel() -> None:
    out = to_payment(Row(channel="MOCK"))
    assert out.pay_url == MOCK_PAY_URL_TEMPLATE.format(payment_id="NVPAY20260923000041")
    # A **relative** API path: the backend cannot know the public hostname it is served
    # under, so an absolute URL invented here would work locally and 404 everywhere else.
    assert out.pay_url is not None and out.pay_url.startswith("/api/v1/payments/customer/")


def test_refundable_amount_is_read_from_the_model_not_recomputed() -> None:
    """INV-005 has one owner, and this proves the serializer asks rather than derives.

    The stand-in is deliberately given a ``refundable_amount`` that **disagrees** with
    ``paid - refunded``. A serializer that recomputed would produce the arithmetic answer
    and pass a naive test; the assertion is that the model's value wins, which is the only
    way the number the UI renders and the number the refund cap enforces cannot drift.
    """
    row = Row(paid_amount=279900, refunded_amount=100000)
    row.refundable_amount = 12345  # deliberately not 179900

    out = to_payment(row)

    assert out.refundable_amount == 12345
    assert out.refunded_amount == 100000
    assert out.paid_amount == 279900


def test_a_fully_refunded_payment_reports_zero_refundable() -> None:
    out = to_payment(Row(status="REFUNDED", paid_amount=279900, refunded_amount=279900))
    assert out.refundable_amount == 0


def test_the_create_projection_carries_the_replay_flag_and_the_order_axis() -> None:
    out = to_payment_create(Row(), replayed=True, order_payment_status="PAYING")
    dumped = out.model_dump(mode="json")
    assert dumped["replayed"] is True
    assert dumped["order_payment_status"] == "PAYING"
    # The whole payment object is still there - a replay answers with the *same* attempt,
    # so the client can render it exactly as it would a fresh one (INV-015).
    assert dumped["payment_no"] == "NVPAY20260923000041"


def test_a_list_projection_preserves_the_repository_order() -> None:
    """Ordering is not re-sorted at the edge.

    The repository orders by ``created_at DESC, id DESC`` where the id tiebreaker matters
    (two attempts created in the same millisecond share a ``DATETIME(3)`` value). A
    serializer that sorted would be a second, disagreeing opinion about which attempt is
    "latest" - and the list would reorder itself between requests.
    """
    rows = [
        Row(id=3, payment_no="NVPAY000003"),
        Row(id=2, payment_no="NVPAY000002"),
        Row(id=1, payment_no="NVPAY000001"),
    ]
    assert [item.payment_no for item in to_payment_list(rows)] == [
        "NVPAY000003",
        "NVPAY000002",
        "NVPAY000001",
    ]


def test_an_empty_page_is_an_empty_list_not_none() -> None:
    assert to_payment_list([]) == []


def test_the_callback_acknowledgement_accepts_no_row_and_no_dict() -> None:
    """Its parameters are keyword-only and explicit.

    This payload is the *only* thing a provider learns about the merchant's business, so
    it must be impossible to widen by handing the serializer a row: there is no ``Any``
    input and no ``**`` passthrough to copy unexplained fields from.
    """
    ack = to_callback_ack(
        event_id="evt-1",
        payment_no="NVPAY20260923000041",
        processed=True,
        replayed=True,
        duplicate=True,
        status="SUCCESS",
    )
    assert ack.event_id == "evt-1"
    assert ack.payment_no == "NVPAY20260923000041"
    assert set(ack.model_dump()) == {
        "event_id",
        "payment_no",
        "processed",
        "replayed",
        "duplicate",
        "status",
    }


def test_the_acknowledgement_exposes_none_of_the_order_s_figures() -> None:
    ack = to_callback_ack(event_id="evt-1", payment_no="NVPAY1", processed=True)
    rendered = ack.model_dump()
    for leaked in ("amount", "payable_amount", "order_id", "user_id", "merchant_id"):
        assert leaked not in rendered


def test_the_redaction_marker_the_provider_module_uses_is_the_frozen_one() -> None:
    """Section 5.3 freezes ``***REDACTED***``, and it is the same literal ``MASK``.

    Pinned here (rather than only in ``test_providers``) because the marker appears in
    stored evidence: two different literals would mean the evidence scanner and the filter
    disagree about what "already redacted" looks like, and a secret could then sit in a
    snapshot that reads as clean.
    """
    assert PAYLOAD_REDACTED == MASK == "***REDACTED***"
