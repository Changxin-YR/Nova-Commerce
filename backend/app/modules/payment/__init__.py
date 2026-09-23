"""Payment domain - the money-in record and the credible callback.

PHASE5_DESIGN sections 3, 4.1, 5.1-5.3, 6.1. Import surface only; the use cases
live in :mod:`~app.modules.payment.service`, the transaction body in
:mod:`~app.modules.payment.workflow`, and the provider signature/redaction rules
in :mod:`~app.modules.payment.providers`.

The two sentences the package is arranged around (design section 2):

* a payment becomes ``SUCCESS`` only from a **verified** provider callback;
* that callback is idempotent **at the database**, through
  ``UNIQUE (provider, provider_event_id)`` on ``payment_callbacks``.

Nothing in this ``__init__`` performs I/O: importing the package must not open a
connection, so that model registration for Alembic stays free of side effects.
The use cases and the workflow are re-exported as names for the same reason a
package needs a public surface at all: a caller should not have to know which of
five modules a symbol lives in, and the surface is what the module-layout
diagram in design section 3 promises.
"""

from app.modules.payment.enums import (
    CALLBACK_PROCESS_STATUSES,
    PAYMENT_CHANNELS,
    PAYMENT_RECORD_STATUSES,
    CallbackProcessStatus,
    PaymentChannel,
    PaymentRecordStatus,
)
from app.modules.payment.models import Payment, PaymentCallback
from app.modules.payment.providers import (
    PAYLOAD_REDACTED,
    CallbackHeaders,
    SignatureVerdict,
    callback_secret_for,
    filter_callback_payload,
    sign_body,
    verify_signature,
)
from app.modules.payment.repository import (
    SETTLEABLE_STATUSES,
    InsertedCallback,
    PaymentCallbackRepository,
    PaymentRepository,
)
from app.modules.payment.schemas import (
    CallbackAckOut,
    CreatePaymentRequest,
    MockPayRequest,
    PaymentCreateOut,
    PaymentOut,
)
from app.modules.payment.serializers import pay_url_for, to_payment
from app.modules.payment.service import (
    PaymentCreateResult,
    PaymentPage,
    PaymentService,
    canonical_payment_request_hash,
    is_mock_allowed,
)
from app.modules.payment.workflow import (
    CallbackExecution,
    CallbackRequest,
    PaymentSuccessWorkflow,
    order_deduct_key,
)

__all__ = [
    "CALLBACK_PROCESS_STATUSES",
    "PAYLOAD_REDACTED",
    "PAYMENT_CHANNELS",
    "PAYMENT_RECORD_STATUSES",
    "SETTLEABLE_STATUSES",
    "CallbackAckOut",
    "CallbackExecution",
    "CallbackHeaders",
    "CallbackProcessStatus",
    "CallbackRequest",
    "CreatePaymentRequest",
    "InsertedCallback",
    "MockPayRequest",
    "Payment",
    "PaymentCallback",
    "PaymentCallbackRepository",
    "PaymentChannel",
    "PaymentCreateOut",
    "PaymentCreateResult",
    "PaymentOut",
    "PaymentPage",
    "PaymentRecordStatus",
    "PaymentRepository",
    "PaymentService",
    "PaymentSuccessWorkflow",
    "SignatureVerdict",
    "callback_secret_for",
    "canonical_payment_request_hash",
    "filter_callback_payload",
    "is_mock_allowed",
    "order_deduct_key",
    "pay_url_for",
    "sign_body",
    "to_payment",
    "verify_signature",
]
