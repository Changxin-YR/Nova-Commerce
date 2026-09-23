"""Payment provider boundary: payload filtering and callback signature verification.

Spec references:
    REQ-PAY-003  a callback snapshot is stored for audit/dispute but is
                 sensitive-field filtered first.
    REQ-PAY-005  a callback authenticates with a **provider signature**, never a
                 bearer token - a payment provider cannot hold a user's JWT.
    design 5.3   the exact sensitive key set, and why "redact everything" is a
                 failed implementation rather than a strict one.
    design 7     the callback headers and the HMAC scheme.
    HANDOFF 六    no secrets in stored/emitted data (the redaction rules).

## Why this module has no database import at all

Everything here is a pure function of (payload, headers, secret, clock). That is
deliberate and load-bearing rather than tidy: the routing of a payment is the part
an attacker probes hardest, so it must be testable **without** a transaction, a
fixture or a running MySQL. The FG-11 gate runs against real MySQL; the 60-odd
signature and redaction cases run in milliseconds as unit tests, and they run even
on a machine where docker is not up.

## The two functions answer two different questions

``filter_callback_payload`` decides **what may be stored**. ``verify_signature``
decides **who sent it**. Neither can do the other's job, and the callback handler
calls both before it opens the business transaction - an unsigned or unverifiable
body must never reach ``PaymentSuccessWorkflow``.

## ``filter_callback_payload`` is not ``app.core.redaction.redact``

``redact`` is *purpose-driven* and deliberately over-broad: it strips any key whose
normalised name contains a marker such as ``token`` or ``sign``, which is exactly
right for a log line and an LLM prompt, where a false positive costs nothing.

A callback snapshot is different in two ways:

* it is **evidence**. A dispute six weeks later is resolved by reading the
  provider's payload back. A filter that redacts ``trade_no`` (contains "no"?)
  or ``sign_type`` destroys the audit value while passing any test that only
  checks "no secret survived". Design 5.3 names that failure mode explicitly.
* its key set is **frozen by the design**. The provider's vocabulary is known, so
  the redaction set can be exact instead of heuristic.

So this module owns its own vocabulary, and it is the *narrower* one. The
``signature_valid`` verdict is stored as a bool next to the snapshot, and it is
computed from the raw body **before** filtering - so redaction can never
invalidate the signature it sits beside.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.core.config import Settings
from app.core.redaction import MASK, MAX_DEPTH

__all__ = [
    "CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED",
    "DEFAULT_MAX_SKEW_SECONDS",
    "PAYLOAD_REDACTED",
    "SENSITIVE_KEYS",
    "SENSITIVE_KEY_MARKERS",
    "CallbackHeaders",
    "SignatureVerdict",
    "callback_secret_for",
    "filter_callback_payload",
    "is_sensitive_key",
    "parse_amount_minor",
    "sign_body",
    "signature_reason_code",
    "verify_signature",
]


# ---------------------------------------------------------------------------
# The sensitive key set (design 5.3, verbatim)
# ---------------------------------------------------------------------------
#: The frozen set from design 5.3, normalised to lowercase alphanumerics. The
#: normalisation is what makes ``api_key`` and ``apiKey`` - and the design's own
#: ``card_no``/``cardno`` pair - collapse onto one entry instead of relying on
#: both spellings being listed. ``redaction._normalise_key`` uses the identical
#: rule, so the two modules cannot disagree about what one key *is*.
SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "authorization",
        "auth",
        "apikey",
        "sign",
        "signature",
        "privatekey",
        "cardno",
        "pan",
        "cvv",
        "cvv2",
        "cvc",
        "idcard",
        "bankaccount",
        "phone",
        "email",
        "key",
    }
)

#: The raw spellings, kept for the error message and for the test that reads the
#: design list back. A reader grepping for ``gateway_key`` should find something.
SENSITIVE_KEY_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "auth",
    "api_key",
    "apikey",
    "sign",
    "signature",
    "private_key",
    "card_no",
    "cardno",
    "pan",
    "cvv",
    "cvv2",
    "cvc",
    "id_card",
    "bank_account",
    "phone",
    "email",
    "key",
)

#: The exact marker design 5.3 freezes. Equal to
#: :data:`app.core.redaction.MASK` on purpose - one marker means one grep, and
#: the equality is asserted by a test rather than assumed by a comment.
PAYLOAD_REDACTED: Final[str] = MASK

#: Event type for a settlement notification. The mock provider sends this; a real
#: provider's own string is mapped onto it in ``api/callbacks.py``.
CALLBACK_EVENT_TYPE_PAYMENT_SUCCEEDED: Final[str] = "PAYMENT_SUCCEEDED"

#: Mirrors ``Settings.PAYMENT_CALLBACK_MAX_SKEW_SECONDS``'s own default. Used only
#: when a caller verifies against no settings object (unit tests, tooling).
DEFAULT_MAX_SKEW_SECONDS: Final[int] = 300


def _normalise_key(key: str) -> str:
    """Lowercase, drop every non-alphanumeric character.

    The same rule as ``app.core.redaction._normalise_key``. Duplicated as three
    lines rather than imported because that helper is private to the redaction
    module and a name-mangled import across modules is a worse dependency than a
    one-line normalisation with a test pinning the two together.
    """
    return "".join(character for character in key.lower() if character.isalnum())


def is_sensitive_key(key: str) -> bool:
    """Whether a callback payload key names a secret or cardholder datum.

    Exact match on the normalised key, not substring match. That is the deliberate
    difference from :func:`app.core.redaction.is_secret_key`: ``sign_type`` (the
    provider's signature *algorithm*, genuinely useful evidence) must survive, and
    a substring rule on ``sign`` would remove it. The frozen set is what makes the
    narrow rule safe - it is not a heuristic being applied to unknown data.
    """
    return _normalise_key(key) in SENSITIVE_KEYS


def filter_callback_payload(payload: Any, *, _depth: int = 0) -> Any:
    """Return a **new** structure with every sensitive value replaced by a marker.

    Recursive at every depth, through both mappings and sequences, because a real
    provider nests: ``{"data": {"card": {"pan": "..."}}}`` and
    ``{"items": [{"phone": "..."}]}`` are both ordinary shapes, and a filter that
    only walks the top level is a filter that leaks on the first list-of-dicts.

    Unknown keys are **kept**. That is the whole point of the function (design
    5.3): the snapshot exists to be read back during a dispute, so a filter that
    redacts everything has destroyed its own reason to exist while looking
    maximally safe.

    The input is never mutated. A caller holds the provider's live dict; redacting
    in place would corrupt it and make the signature over the raw body
    unverifiable afterwards.

    Depth is bounded by :data:`app.core.redaction.MAX_DEPTH`. A payload nested
    deeper than that is replaced wholesale by the marker rather than truncated:
    silently keeping a depth-limited subtree would be the "looks filtered but is
    not" failure this module exists to avoid, and 8 levels is far beyond any real
    provider body.
    """
    if _depth > MAX_DEPTH:
        # Not reachable from any provider body in V1. Replacing rather than
        # recursing is the safe direction: an unbounded walk of attacker-shaped
        # JSON is a stack-exhaustion primitive, and a redacted subtree is
        # recoverable from the provider on request.
        return PAYLOAD_REDACTED

    if isinstance(payload, Mapping):
        filtered: dict[Any, Any] = {}
        for key, value in payload.items():
            if isinstance(key, str) and is_sensitive_key(key):
                filtered[key] = PAYLOAD_REDACTED
            else:
                # A non-string key is JSON-impossible but dict-possible; it is
                # walked rather than rejected so a caller cannot smuggle a secret
                # past the filter by making its **key** a tuple.
                filtered[key] = filter_callback_payload(value, _depth=_depth + 1)
        return filtered

    if isinstance(payload, str | bytes):
        # A string is a Sequence; without this branch every string would be
        # exploded into a list of characters and the snapshot would be garbage.
        return payload

    if isinstance(payload, Sequence):
        return [filter_callback_payload(item, _depth=_depth + 1) for item in payload]

    return payload


# ---------------------------------------------------------------------------
# Signature verification (REQ-PAY-005, design 7)
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class CallbackHeaders:
    """The four provider headers, already extracted from the request.

    Modelled as a value object rather than read from ``Request`` inside
    :func:`verify_signature` so that the verification function stays a pure
    function and the HTTP layer stays the only place that knows about HTTP.
    """

    provider: str
    event_id: str
    timestamp: str
    signature: str


@dataclass(frozen=True, slots=True)
class SignatureVerdict:
    """The outcome of verification, with the reason rather than a bare bool.

    A bool would be enough to refuse, but not enough to *log*: "signature invalid"
    as the only record makes a misconfigured secret and an active replay attack
    look identical in the audit trail. ``reason`` is a stable short code (never
    the secret, never the expected digest - see the log-safety note below).
    """

    valid: bool
    reason: str
    provider: str = ""
    event_id: str = ""
    skew_seconds: int | None = None

    def __bool__(self) -> bool:
        return self.valid


#: Reason codes. Stable strings, safe to log and to assert on in tests.
REASON_OK: Final[str] = "OK"
REASON_MISSING_SECRET: Final[str] = "MISSING_SECRET"  # noqa: S105 - a refusal reason, not a credential
REASON_MISSING_HEADER: Final[str] = "MISSING_HEADER"
REASON_TIMESTAMP_NOT_NUMERIC: Final[str] = "TIMESTAMP_NOT_NUMERIC"
REASON_TIMESTAMP_STALE: Final[str] = "TIMESTAMP_STALE"
REASON_SIGNATURE_MISMATCH: Final[str] = "SIGNATURE_MISMATCH"

_MISSING: Final[str] = "MISSING"

#: The accepted header names. Providers differ in casing; HTTP header lookup is
#: case-insensitive on the way in, and these are the canonical spellings the
#: contract freezes (design 7).
CALLBACK_HEADER_NAMES: Final[tuple[str, ...]] = (
    "X-Provider",
    "X-Provider-Event-Id",
    "X-Provider-Timestamp",
    "X-Provider-Signature",
)


def callback_secret_for(settings: Settings, provider: str) -> str | None:
    """Resolve the HMAC secret for one provider, or ``None`` when unusable.

    ``None`` rather than the default secret when a **specific** per-provider
    override is configured as an empty string: a provider whose secret is blank is
    misconfigured, and falling back to the shared dev secret would authenticate
    that provider with a key its operator never chose. An empty
    ``PAYMENT_PROVIDER_SECRETS`` means "every provider uses the shared secret",
    which is the single-provider (mock) case and is not an error.

    The ``Settings`` accessor is used rather than reading the dict here, because a
    ``SecretStr`` that is logged by accident is a leaked key; one accessor that
    unwraps it is easier to audit than several.
    """
    if not provider:
        return None
    override = settings.PAYMENT_PROVIDER_SECRETS.get(provider.upper())
    if override is not None:
        value = override.get_secret_value()
        return value or None
    shared = settings.PAYMENT_CALLBACK_SECRET.get_secret_value()
    return shared or None


def sign_body(*, secret: str, timestamp: str, raw_body: bytes) -> str:
    """``HMAC-SHA256(secret, f"{timestamp}.{raw_body}")`` in lowercase hex.

    This is the single implementation of the scheme on both sides: the mock
    provider signs with it, the callback endpoint verifies with it, and the
    integration tests drive both. Two implementations of a signature scheme are
    how a suite passes against a verifier that no provider agrees with.

    ## Why the timestamp is inside the signed material

    Signing the body alone authenticates *content*, not *freshness*: a captured
    body stays valid forever and can be replayed at will. Binding the timestamp
    means a captured request is useless once the skew window closes, and the
    replay must then forge a new timestamp - which requires the secret. The dot
    separator is part of the frozen scheme; a provider computing ``ts+body``
    produces a different digest, and that is intentional, not an oversight.
    """
    message = f"{timestamp}.".encode() + raw_body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_signature(
    *,
    raw_body: bytes,
    headers: CallbackHeaders,
    secret: str | None,
    max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
    now: float | None = None,
) -> SignatureVerdict:
    """Verify a provider callback's signature and freshness.

    Order of the checks matters for the audit trail, not for security: the cheapest
    and most diagnostic reason is reported first, so a misconfigured deployment
    reads as ``MISSING_SECRET`` rather than as a mysterious mismatch.

    ## The five refusals

    1. no resolved secret - ``MISSING_SECRET``. A deployment with no key cannot
       authenticate anything, and treating "no key" as "skip verification" is the
       classic fail-open.
    2. any of the four headers absent/blank - ``MISSING_HEADER``.
    3. a non-numeric timestamp - ``TIMESTAMP_NOT_NUMERIC``. Refused rather than
       coerced: a timestamp the verifier cannot read is a timestamp whose skew it
       cannot bound.
    4. ``|now - timestamp| > max_skew_seconds`` - ``TIMESTAMP_STALE``. The window
       is checked in **both** directions, because a clock-skewed provider sending
       a future timestamp is the same class of defect as a replay from the past,
       and accepting it would widen the replay window by however far the clock is
       wrong.
    5. digest mismatch - ``SIGNATURE_MISMATCH``, compared with
       :func:`hmac.compare_digest` so the comparison cannot be timed.

    ## What is deliberately *not* in the verdict

    Neither the expected nor the received digest, and never the secret. A log line
    that carries the expected signature next to a rejected request turns every log
    reader into someone who can forge callbacks. The caller logs
    ``reason``/``provider``/``event_id`` and nothing else (HANDOFF 六).

    ``now`` is injectable so the stale-window test does not have to sleep, and so
    a future verifier can be driven from a clock source rather than
    ``time.time()`` directly.
    """
    if not secret:
        return SignatureVerdict(
            valid=False, reason=REASON_MISSING_SECRET, provider=headers.provider
        )

    if not all(
        (
            headers.provider.strip(),
            headers.event_id.strip(),
            headers.timestamp.strip(),
            headers.signature.strip(),
        )
    ):
        return SignatureVerdict(
            valid=False, reason=REASON_MISSING_HEADER, provider=headers.provider
        )

    try:
        # `int()` on a str accepts surrounding whitespace and a leading sign, and
        # rejects "1.5e9" and "" - which is exactly the strictness wanted. A float
        # timestamp is refused because the scheme the providers implement uses
        # integer epoch **seconds**.
        timestamp = int(headers.timestamp)
    except (TypeError, ValueError):
        return SignatureVerdict(
            valid=False,
            reason=REASON_TIMESTAMP_NOT_NUMERIC,
            provider=headers.provider,
            event_id=headers.event_id,
        )

    current = time.time() if now is None else now
    skew = int(abs(current - timestamp))
    if skew > max_skew_seconds:
        return SignatureVerdict(
            valid=False,
            reason=REASON_TIMESTAMP_STALE,
            provider=headers.provider,
            event_id=headers.event_id,
            skew_seconds=skew,
        )

    expected = sign_body(secret=secret, timestamp=headers.timestamp, raw_body=raw_body)
    if not hmac.compare_digest(expected, headers.signature.strip()):
        return SignatureVerdict(
            valid=False,
            reason=REASON_SIGNATURE_MISMATCH,
            provider=headers.provider,
            event_id=headers.event_id,
            skew_seconds=skew,
        )

    return SignatureVerdict(
        valid=True,
        reason=REASON_OK,
        provider=headers.provider,
        event_id=headers.event_id,
        skew_seconds=skew,
    )


def signature_reason_code(verdict: SignatureVerdict) -> str:
    """The reason as a compact code for logs, e.g. ``OK``/``SIGNATURE_MISMATCH``."""
    return verdict.reason


# ---------------------------------------------------------------------------
# Payload amount parsing
# ---------------------------------------------------------------------------
def parse_amount_minor(value: Any) -> int | None:
    """Read a provider amount as **integer minor units**, or ``None`` if unusable.

    The one place a provider's amount is turned into money, and it exists because
    this is where a payment is silently wrong instead of loudly refused:

    * ``"1000"`` and ``1000`` both mean 1000 minor units. A real PSP posts strings.
    * ``1000.0`` is accepted because JSON numbers lose their integrality in
      transit, and a provider's ``1000.0`` is not a different amount.
    * ``1000.5`` is **refused** (``None``). Rounding it would move money by an
      amount nobody agreed to, and truncating it would move it the other way; both
      are worse than a refused callback that an operator can inspect.
    * ``None``/``""``/``"abc"`` are refused rather than treated as 0, because a
      missing amount that reads as zero looks exactly like an amount mismatch the
      guard will catch - except when ``payments.amount`` is also zero, which cannot
      happen (the column carries ``CHECK (amount > 0)``).

    Returns ``None`` rather than raising so the caller can decide the business
    code: an unparseable amount is ``PAYMENT_AMOUNT_MISMATCH (60002)`` in the
    callback path, not a 500.
    """
    if value is None or isinstance(value, bool):
        # `bool` is an `int` in Python; `True` is not an amount of one minor unit.
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            value = Decimal(stripped)
        except InvalidOperation:
            return None
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            return None
        return int(value)
    if isinstance(value, float):
        if value != int(value):
            return None
        return int(value)
    return None
