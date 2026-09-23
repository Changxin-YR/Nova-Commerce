"""The payment wire schemas - section 38's "business inputs only" rule, made executable.

Spec references: section 38, section 110, section 2, section 3, API_CONTRACT 15.2.

## The test that carries the weight

:func:`test_a_client_cannot_name_its_own_amount`. The single most important rule in the
payment domain is that the server reads ``orders.payable_amount`` and never a number the
client sent - a request that could name its own amount would let a customer settle a
10000-minor-unit order by paying 1, and the callback's amount guard compares the
*provider's* figure against the *payment row*, so a client-settable amount would defeat
every check downstream of it at once.

The enforcement is ``extra="forbid"``, and without a test a later author relaxing that
config "to be lenient" would remove the protection silently: an ignored key looks
*exactly* like a working request from the client's side, and in this domain the response
shows the server's own figure, so the mistake would look like it worked.

The second-most important is the timestamp encoding - Pydantic's default is six
fractional digits and section 2 freezes three. Both parse in JavaScript, which is exactly
why it would go unnoticed for a long time.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.modules.payment.enums import PaymentChannel, PaymentRecordStatus
from app.modules.payment.schemas import (
    CallbackAckOut,
    CreatePaymentRequest,
    MockPayRequest,
    PaymentCreateOut,
    PaymentOut,
)

pytestmark = pytest.mark.unit


def _valid_create(**overrides) -> dict:
    body = {
        "order_no": "NV20260922000001",
        "channel": "MOCK",
        "client_request_id": "client-1",
    }
    body.update(overrides)
    return body


def _valid_payment(**overrides) -> dict:
    body = {
        "id": 41,
        "payment_no": "NVPAY20260923000041",
        "order_id": 456,
        "order_no": "NV20260922000001",
        "channel": "MOCK",
        "status": "PAYING",
        "amount": 279900,
        "paid_amount": 0,
        "refunded_amount": 0,
        "refundable_amount": 0,
        "created_at": datetime(2026, 9, 22, 23, 31, 7, 507000, tzinfo=UTC),
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
def test_the_frozen_create_request_parses() -> None:
    """The exact body ``frontend/src/api/payment.ts`` sends."""
    payload = CreatePaymentRequest.model_validate(_valid_create())
    assert payload.order_no == "NV20260922000001"
    assert payload.channel is PaymentChannel.MOCK
    assert payload.client_request_id == "client-1"


@pytest.mark.parametrize(
    "forbidden",
    [
        "amount",
        "paid_amount",
        "refunded_amount",
        "status",
        "external_transaction_no",
        "transaction_no",
        "merchant_id",
        "user_id",
        "payment_no",
        "idempotency_key",
        "request_hash",
        "pay_url",
    ],
)
def test_a_client_cannot_name_its_own_amount_or_any_server_owned_field(
    forbidden: str,
) -> None:
    """Section 38 + section 110, field by field.

    ``extra="forbid"`` is asserted for **every** server-owned field rather than just
    ``amount``, because the tempting mistake is to relax it once for a field that "looks
    harmless" - ``external_transaction_no`` is the one that would be a forged settlement,
    and ``status`` is the one that would mark an order paid.
    """
    with pytest.raises(ValidationError):
        CreatePaymentRequest.model_validate(_valid_create(**{forbidden: 1}))


def test_the_amount_is_not_even_a_field_on_the_request_model() -> None:
    """Belt and braces, and the one assertion a reader will check first.

    ``extra="forbid"`` is the mechanism; this is the statement of intent. A model that
    gained an optional ``amount: int | None = None`` would still reject an unknown key
    while silently accepting a known one, so the *field list* is pinned separately.
    """
    assert set(CreatePaymentRequest.model_fields) == {
        "order_no",
        "channel",
        "client_request_id",
    }


@pytest.mark.parametrize("channel", ["MOCK", "ALIPAY", "WECHAT"])
def test_every_frozen_channel_parses(channel: str) -> None:
    """The union must match ``frontend/src/types/domain.ts`` exactly."""
    assert CreatePaymentRequest.model_validate(
        _valid_create(channel=channel)
    ).channel.value == channel


@pytest.mark.parametrize("channel", ["PAYPAL", "mock", "", "CASH"])
def test_a_channel_outside_the_frozen_vocabulary_is_rejected(channel: str) -> None:
    """A 422, not a 60005.

    The distinction is deliberate: a channel **outside** the vocabulary is a malformed
    request, while a channel the deployment has switched off is
    ``PAYMENT_CHANNEL_UNSUPPORTED`` (60005), which is a policy answer the client can act
    on. Both end in a refusal; only the second is a business outcome.
    """
    with pytest.raises(ValidationError):
        CreatePaymentRequest.model_validate(_valid_create(channel=channel))


@pytest.mark.parametrize("field", ["order_no", "client_request_id"])
def test_a_blank_identifier_is_refused(field: str) -> None:
    """A blank identifier would collide with every other blank one in a UNIQUE index."""
    with pytest.raises(ValidationError):
        CreatePaymentRequest.model_validate(_valid_create(**{field: "   "}))


def test_identifiers_are_stripped() -> None:
    payload = CreatePaymentRequest.model_validate(
        _valid_create(order_no="  NV20260922000001  ")
    )
    assert payload.order_no == "NV20260922000001"


def test_the_mock_body_accepts_only_its_documented_fields() -> None:
    """A mock settlement must not gain an input the real callback path does not have.

    ``status``/``paid_at``/``transaction_no``/``amount`` are all refused. A caller can ask
    for a settlement; it cannot describe one.
    """
    assert set(MockPayRequest.model_fields) == {"client_request_id"}
    for forbidden in ("status", "amount", "paid_at", "transaction_no", "paid_amount"):
        with pytest.raises(ValidationError):
            MockPayRequest.model_validate({forbidden: 1})


def test_the_mock_body_is_optional_and_empty_by_default() -> None:
    assert MockPayRequest.model_validate({}).client_request_id is None


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------
def test_the_frozen_payment_shape_round_trips() -> None:
    out = PaymentOut.model_validate(_valid_payment())
    assert out.payment_no == "NVPAY20260923000041"
    assert out.external_transaction_no is None
    assert out.pay_url is None
    assert out.refundable_amount == 0


def test_timestamps_are_iso8601_utc_with_three_fractional_digits() -> None:
    """Section 2 freezes the encoding, and Pydantic's default is six digits.

    Both are valid ISO-8601 and both parse in JavaScript, which is why a mismatch would
    survive a long time unnoticed - and why the assertion is on the literal string rather
    than on a parsed value.
    """
    dumped = PaymentOut.model_validate(_valid_payment()).model_dump(mode="json")
    assert dumped["created_at"] == "2026-09-22T23:31:07.507Z"
    assert dumped["paid_at"] is None


def test_a_naive_timestamp_is_normalised_not_rejected() -> None:
    """``DateTimeMS`` guarantees aware-UTC on the way out, so a naive value can only come
    from a hand-built object - and a *silently local* time is exactly what section 2
    forbids. Normalising is the safe reading; raising here would turn a serialisation
    detail into a 500 in the middle of a payment response."""
    out = PaymentOut.model_validate(
        # Naive **on purpose**: that is the input this branch exists to normalise.
        _valid_payment(created_at=datetime(2026, 9, 22, 23, 31, 7, 507000))  # noqa: DTZ001
    )
    assert out.model_dump(mode="json")["created_at"] == "2026-09-22T23:31:07.507Z"


def test_an_unknown_status_is_refused_rather_than_emitted() -> None:
    """The stored value comes from a CHECK constraint, so an unknown one means the Python
    vocabulary has drifted from the migration. Emitting it would hand the frontend's
    exhaustive switch a case it has no branch for."""
    with pytest.raises(ValidationError):
        PaymentOut.model_validate(_valid_payment(status="SETTLED"))


def test_an_unknown_channel_is_refused_rather_than_emitted() -> None:
    with pytest.raises(ValidationError):
        PaymentOut.model_validate(_valid_payment(channel="PAYPAL"))


def test_every_payment_record_status_is_representable() -> None:
    for member in PaymentRecordStatus:
        assert PaymentOut.model_validate(_valid_payment(status=member.value)).status == member.value


def test_the_create_response_carries_the_replay_flag_and_the_order_axis() -> None:
    out = PaymentCreateOut.model_validate(
        _valid_payment(replayed=True, order_payment_status="PAYING")
    )
    dumped = out.model_dump(mode="json")
    assert dumped["replayed"] is True
    assert dumped["order_payment_status"] == "PAYING"


def test_a_callback_acknowledgement_leaks_nothing_about_the_order() -> None:
    """The provider learns about its own delivery, not about the merchant's business.

    The callback URL is reachable by anyone who knows it; the signature proves *who* is
    calling, not that they are entitled to read the shop. So the field list is pinned
    here rather than left to review: adding ``amount`` or ``order_status`` to this model
    would be a data leak that no other test would catch.
    """
    assert set(CallbackAckOut.model_fields) == {
        "event_id",
        "payment_no",
        "processed",
        "replayed",
        "duplicate",
        "status",
    }


def test_the_duplicate_acknowledgement_is_marked_as_both_processed_and_duplicate() -> None:
    """A duplicate is a **success** (HTTP 200, code 60004): a provider retries until it
    sees success, so answering an error to a delivery that was already applied would make
    it retry forever. ``processed`` therefore stays true, and ``duplicate`` is the flag
    that tells a human what happened."""
    ack = CallbackAckOut.model_validate(
        {
            "event_id": "evt-1",
            "payment_no": "NVPAY20260923000041",
            "processed": True,
            "replayed": True,
            "duplicate": True,
            "status": "SUCCESS",
        }
    )
    assert ack.duplicate and ack.processed and ack.replayed
    assert ack.model_dump(mode="json")["event_id"] == "evt-1"


def test_the_create_request_has_no_default_for_the_order_or_the_channel() -> None:
    """Both are required: a default would let a client omit the order and receive an
    attempt against whatever the default happened to be."""
    for field in ("order_no", "channel", "client_request_id"):
        with pytest.raises(ValidationError):
            CreatePaymentRequest.model_validate(
                {key: value for key, value in _valid_create().items() if key != field}
            )
