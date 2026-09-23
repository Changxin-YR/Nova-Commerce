"""Payment domain vocabularies - spec section 19 (``VARCHAR`` + Python Enum).

    PaymentChannel, PaymentRecordStatus, CallbackProcessStatus

PHASE5_DESIGN section 4.1 freezes these three. All are ``StrEnum`` for the same
reason as :mod:`app.modules.order.enums`: the values are stored verbatim in
``VARCHAR`` columns that also carry hand-written ``CHECK (col IN (...))``
constraints written by the migration, so the Python vocabulary and the SQL
vocabulary must be byte-identical, and ``StrEnum`` makes
``PaymentChannel.MOCK == "MOCK"`` true without a ``.value`` dance.

## Why a payment record has its own status and does not reuse ``PaymentStatus``

``orders.payment_status`` (UNPAID/PAYING/PAID/PARTIAL_REFUNDED/REFUNDED) is the
**order's** view: one axis describing the order's money position. A ``payments``
row is a different object - one *attempt* at one provider - and it needs states
the order axis must not carry:

* ``INITIATED`` - the attempt exists but has not been handed to a provider.
  There is no sensible order-level meaning for "an attempt exists", because the
  order's axis already says ``PAYING``;
* ``FAILED`` / ``CLOSED`` - this attempt died or its window shut. The order is
  still payable, so the order axis stays ``UNPAID``/``PAYING`` and a *new*
  attempt is the correct next step. Collapsing the two vocabularies would make
  "the order is unpaid" and "this attempt failed" the same string, and the
  customer's retry path unexpressible.

The one place they touch is ``SUCCESS`` -> the order's ``PAID``, written by
``PaymentSuccessWorkflow`` as two separate writes of two separate columns.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "CALLBACK_PROCESS_STATUSES",
    "PAYMENT_CHANNELS",
    "PAYMENT_RECORD_STATUSES",
    "CallbackProcessStatus",
    "PaymentChannel",
    "PaymentRecordStatus",
]


class PaymentChannel(StrEnum):
    """How the customer was asked to pay (PHASE5_DESIGN section 4.1).

    The membership is not a free choice: it mirrors the frontend's frozen union
    in ``frontend/src/types/domain.ts`` (``'MOCK' | 'ALIPAY' | 'WECHAT'``), and
    ``PAYMENT_ENABLED_CHANNELS`` filters this set at runtime rather than
    redefining it. Adding a provider is therefore a two-sided change (frontend
    union + this enum + a migration for the ``CHECK``), never a config edit
    alone.

    ``MOCK`` is the DEV/DEMO-only channel that FG-11 drives. It is a real
    channel *member* but its surface is refused outright when ``APP_ENV`` is not
    dev/test/demo (REQ-PAY-005), because a mock channel reachable in production
    is a way to mark an order paid without money moving.
    """

    MOCK = "MOCK"
    ALIPAY = "ALIPAY"
    WECHAT = "WECHAT"


class PaymentRecordStatus(StrEnum):
    """The lifecycle of one payment *attempt* (PHASE5_DESIGN section 4.1).

    ``SUCCESS`` is writeable by exactly one code path: a **verified** provider
    callback, inside ``PaymentSuccessWorkflow`` (REQ-PAY-001, INV-008). No JWT
    user can reach it - that is the property the mock-pay endpoint's DEV/DEMO
    guard and the callback's HMAC signature both exist to protect.

    ``REFUNDED`` / ``PARTIAL_REFUNDED`` are written by ``RefundWorkflow`` and are
    derived from the two amounts rather than set independently: ``REFUNDED`` iff
    ``refunded_amount == paid_amount``. They are present here so a console can
    render one status instead of doing that subtraction at the edge.
    """

    #: Created, provider not yet answered. Distinct from ``PAYING`` so a crash
    #: between "row exists" and "provider called" is visible rather than being
    #: reported as an attempt that is in flight.
    INITIATED = "INITIATED"
    #: Handed to the provider; the client has a payment URL/QR. This is the
    #: status the create endpoint writes, and it is what moves the *order's*
    #: axis ``UNPAID -> PAYING``.
    PAYING = "PAYING"
    #: Only a verified callback writes this.
    SUCCESS = "SUCCESS"
    #: The provider refused, or the amount guard rejected the callback
    #: (``PAYMENT_AMOUNT_MISMATCH``). The order stays payable; a new attempt is
    #: the correct recovery, which is why this is not terminal for the order.
    FAILED = "FAILED"
    #: The payment window shut without settlement (``expires_at``). Reached by
    #: Phase 6's reconciliation; recorded here because the row must be able to
    #: say so.
    CLOSED = "CLOSED"
    #: Fully refunded - ``refunded_amount == paid_amount``.
    REFUNDED = "REFUNDED"
    #: Partially refunded - ``0 < refunded_amount < paid_amount``.
    PARTIAL_REFUNDED = "PARTIAL_REFUNDED"


class CallbackProcessStatus(StrEnum):
    """What happened to one provider callback delivery (section 5.2).

    This is a *processing* status, not a payment status: a callback can be
    ``FAILED`` (bad signature - it never reaches the workflow at all) while the
    payment it names stays ``PAYING``, and it can be ``IGNORED`` (a duplicate
    delivery, or a ``payment_no`` that resolves to nothing) which is a
    completely successful outcome for the system.

    The distinction is what makes the FG-11 evidence readable: "the losers
    answered ``IGNORED``" is a fact about the table, not an inference from logs.
    """

    #: Row inserted; the business effect is not committed yet. A row left in this
    #: state after a crash is the signal that a workflow died mid-transaction.
    RECEIVED = "RECEIVED"
    #: The business effect committed (payment SUCCESS, order PAID, deduction,
    #: fulfillment shell).
    PROCESSED = "PROCESSED"
    #: Signature invalid, or processing refused for a business reason recorded in
    #: ``process_error``. **Invalid signatures must not produce a row that
    #: claims success** (design section 7).
    FAILED = "FAILED"
    #: A duplicate delivery, or an unknown ``payment_no`` - no business effect,
    #: and correctly so. HTTP 200 at the edge, because an idempotent no-op is a
    #: success and telling a provider "error" would make it retry forever.
    IGNORED = "IGNORED"


#: Vocabulary tuples, in the exact order the migration's ``CHECK`` constraints
#: list them. Derived from the enums rather than re-typed so a new member cannot
#: be added in Python while the database still rejects it (the "Alembic does not
#: autogenerate CHECK changes on MySQL" trap - HANDOFF section 6).
PAYMENT_CHANNELS: tuple[str, ...] = tuple(member.value for member in PaymentChannel)
PAYMENT_RECORD_STATUSES: tuple[str, ...] = tuple(member.value for member in PaymentRecordStatus)
CALLBACK_PROCESS_STATUSES: tuple[str, ...] = tuple(member.value for member in CallbackProcessStatus)
