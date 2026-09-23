"""Payment request/response schemas - the wire shapes of API_CONTRACT section 15.

Spec references:
    section 38   business inputs only: a client may not name an amount, a status,
                 a provider transaction id or a merchant. This module is where
                 that rule is enforced at the edge.
    section 110  mass assignment: every request model is ``extra="forbid"``, so a
                 body carrying ``amount``/``status``/``paid_amount``/
                 ``external_transaction_no`` is **rejected with 422** rather than
                 having the extra key ignored. Ignoring it is worse than rejecting
                 it: the client would believe it had set the amount and only find
                 out from the response - and in the payment domain the response
                 would show the *server's* figure, so the mistake would look like
                 it worked.
    section 2    scalar encodings: money is an integer in minor units, enums are
                 SCREAMING_SNAKE strings, timestamps are ISO-8601 UTC with
                 milliseconds, and ``null`` is explicit rather than absent.
    section 3    lists are ``{items, meta}``, never a bare array.
    section 15.2 the Payment object, transcribed field for field.
    REQ-PAY-005  the mock channel is dev/demo only, so no mock body may gain an
                 input the real callback path does not have.

## The single most important rule in this file

``channel`` is the only business input the create request has, next to the order it
names. There is no ``amount`` field and there cannot be: the server reads
``orders.payable_amount`` and writes that. A request that could name its own amount
would let a customer settle a 10000-minor-unit order by paying 1 - and the amount
guard inside ``PaymentSuccessWorkflow`` compares the *provider's* figure against the
*payment row*, so a client-settable amount would defeat every check downstream of
it at once.

``extra="forbid"`` is what makes that rule executable rather than aspirational, and
``test_schemas.py`` posts each forbidden field and asserts 422.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.order.schemas import MetaOut, UtcTimestamp, iso_millis, page_meta
from app.modules.payment.enums import PaymentChannel, PaymentRecordStatus

__all__ = [
    "MAX_CHANNEL_LENGTH",
    "CallbackAckOut",
    "CreatePaymentRequest",
    "MetaOut",
    "MockPayRequest",
    "PaymentCreateOut",
    "PaymentOut",
    "iso_millis",
    "page_meta",
]

#: Mirrors ``payments.channel``'s ``VARCHAR(16)``. Stated here so an over-long
#: channel is a 422 naming the field rather than a database truncation error (or,
#: under MySQL's non-strict modes, a silent truncation).
MAX_CHANNEL_LENGTH = 16


class CreatePaymentRequest(BaseModel):
    """``POST /api/v1/payments/customer/payments`` (section 15.2).

    Three fields, and only one of them is a business input. The shape is the
    frontend's frozen ``CreatePaymentRequest`` (``frontend/src/types/
    api-contract.ts``) - ``order_no`` and ``client_request_id`` rather than an
    id/amount pair - because the backend conforms to the paths and bodies the frozen
    frontend already calls.

    ## Why the order is named by its public number and not by its id

    ``order_no`` is what the client already holds (it is the URL segment of the
    order detail page) and what a support agent can read off a receipt. An internal
    id in a request body also invites a client to walk neighbouring ids, which is
    the class of exposure the repository's in-query ownership filter exists to
    avoid.
    """

    model_config = ConfigDict(extra="forbid")

    order_no: str = Field(
        min_length=1,
        max_length=32,
        description="The caller's own order; ownership is enforced in the query, not post-load.",
    )
    channel: PaymentChannel = Field(
        description="MOCK | ALIPAY | WECHAT. Must also be present in PAYMENT_ENABLED_CHANNELS.",
    )
    client_request_id: str = Field(
        min_length=1,
        max_length=64,
        description="Client-generated idempotency token; unique per customer (second guard).",
    )

    @field_validator("order_no", "client_request_id")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        # "" and "   " are the same intent as omitting the field, and section 2
        # treats an omitted field as different from a blank one. Normalising here
        # also keeps a blank identifier out of a UNIQUE index, where every blank one
        # would collide with every other.
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class MockPayRequest(BaseModel):
    """``POST /api/v1/payments/customer/payments/{payment_id}/mock-pay`` (section 15.2).

    Deliberately almost empty. The mock channel exists so the demo can prove the
    payment invariants without a PSP (section 100 MockPay), and the endpoint drives
    the *same* ``PaymentSuccessWorkflow`` a real provider callback does - so it must
    not gain inputs that the real callback path does not have.

    What is **absent** is the point: no ``status``, no ``paid_at``, no ``amount``. A
    caller cannot describe the settlement it wants - only ask for one, and let the
    server decide whether the business facts allow it.

    ``client_request_id`` is optional here (unlike create) because a mock payment is
    naturally idempotent on the payment row itself: a second mock-pay for an
    already-``SUCCESS`` payment is answered as a replay by the workflow's state
    guard. The field exists so a client can send the same token it always sends, not
    because the endpoint needs it to be safe.
    """

    model_config = ConfigDict(extra="forbid")

    client_request_id: str | None = Field(
        default=None,
        max_length=64,
        description="Optional; a mock settlement is idempotent on the payment row regardless.",
    )


class PaymentOut(BaseModel):
    """One payment attempt as a client sees it (section 15.2).

    Transcribed from the frozen example: ``id``, ``payment_no``, ``order_id``,
    ``order_no``, ``channel``, ``status``, ``amount``, ``paid_amount``,
    ``refunded_amount``, ``external_transaction_no``, ``pay_url``, ``expires_at``,
    ``paid_at``, ``created_at``.

    Three additions beyond that example, each for a stated reason:

    * ``refundable_amount`` - server-owned, like ``OrderDetail.refundable_amount``
      and for the same reason (INV-005). ``paid - refunded`` computed at the edge is
      a second implementation of the refund cap, and the section 15.2 prose says
      this figure exists so a payment page "does not have to sum the refund
      history".
    * ``idempotency_key`` - the frozen frontend ``Payment`` type declares it, and a
      client debugging a duplicate attempt needs to see which key produced this row.
    * ``updated_at`` - the row is mutated by settlement and by refunds, so
      ``created_at`` alone cannot answer "when did this last change".

    ## ``external_transaction_no`` keeps the column's name on the wire

    No rename to ``transaction_no``: one spelling to grep for across the migration,
    the model and the response. It is ``None`` until a **verified** callback writes
    it, because a row carrying one while its status was ``PAYING`` is the exact shape
    of a forged settlement.

    ``pay_url`` is non-null **only** for ``channel = "MOCK"``, where it points at
    this API's own mock-pay endpoint so the demo can settle from the browser with no
    PSP. A real provider's redirect URL is produced by the provider's SDK on the
    client and is never echoed by this API.
    """

    model_config = ConfigDict(extra="forbid")

    id: int
    payment_no: str
    order_id: int
    order_no: str
    channel: str
    status: str
    amount: int
    paid_amount: int
    refunded_amount: int
    refundable_amount: int
    external_transaction_no: str | None = None
    pay_url: str | None = None
    idempotency_key: str | None = None
    expires_at: UtcTimestamp | None = None
    paid_at: UtcTimestamp | None = None
    created_at: UtcTimestamp
    updated_at: UtcTimestamp | None = None

    @field_validator("channel")
    @classmethod
    def _known_channel(cls, value: str) -> str:
        # Defensive rather than decorative: the stored value comes from a CHECK
        # constraint, so an unknown one means the Python vocabulary has drifted from
        # the migration. Failing loudly here beats emitting a channel the frontend's
        # exhaustive switch has no branch for.
        if value not in {member.value for member in PaymentChannel}:
            raise ValueError(f"unknown channel {value!r}")
        return value

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        if value not in {member.value for member in PaymentRecordStatus}:
            raise ValueError(f"unknown payment status {value!r}")
        return value


class PaymentCreateOut(PaymentOut):
    """The create response: the Payment object plus the two facts a retry needs.

    ``replayed`` is the whole point of the distinction. A client that cannot tell
    "we just created this attempt" from "this is the attempt your retry already
    created" has to guess whether its retry was safe (INV-015), and guessing is how
    a client ends up with two payment attempts for one order.

    ``order_payment_status`` is included because creating an attempt is a **write to
    the order**: it moves the order's axis ``UNPAID -> PAYING`` (section 15.1). The
    customer's page needs that new state without a second request, and printing it
    here makes the transition observable rather than implied.
    """

    model_config = ConfigDict(extra="forbid")

    replayed: bool = False
    order_payment_status: str


class CallbackAckOut(BaseModel):
    """The provider-facing acknowledgement (section 15.3).

    A provider is answered with facts about **its own delivery** and nothing about
    the order: the event id, ``processed``/``replayed``/``duplicate``, the resolved
    ``payment_no`` and the payment's status. It must not learn the order's total, its
    contents or anything else about the merchant's business - a callback endpoint is
    reachable by anyone who knows the URL, and the signature proves *who* is calling,
    not that they are entitled to read the shop.

    ``duplicate`` is the HTTP-200 idempotent case
    (``PAYMENT_CALLBACK_DUPLICATE``, 60004): a provider retries until it sees
    success, so answering an error to a delivery that was already applied would make
    it retry forever.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str
    payment_no: str | None = None
    processed: bool
    replayed: bool = False
    duplicate: bool = False
    status: str | None = None
