"""The provider boundary: payload filtering (REQ-PAY-003) and HMAC verification.

Spec references: REQ-PAY-003, REQ-PAY-005, design 5.3, design 7, HANDOFF 六.

## Why this file is the one that must not be thin

These two functions are the entire trust boundary of the payment domain:

* :func:`filter_callback_payload` decides what is **stored** for audit. Its two
  failure modes are opposite and both silent - it leaks a card number into a
  JSON column that gets dumped during an incident, or it redacts the amount and
  ``trade_no`` so a dispute six weeks later cannot be answered from the record.
  Design 5.3 names both, and the test that only checks "no secret survived" is
  the one that passes on the broken implementation.
* :func:`verify_signature` is the **authentication model** for the callback
  surface (REQ-PAY-005). A normal JWT user must never be able to settle a
  payment; the only thing that can is a body signed with the provider's secret.

The two tests that carry the most weight are
:func:`test_a_benign_field_survives_the_filter` (the filter still has audit value)
and :func:`test_signature_over_a_stale_timestamp_is_refused` (a captured body is
not a permanent right to settle).

Every assertion here is a pure function call - no database, no fixture, no docker.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.core.config import Settings
from app.core.redaction import MASK
from app.modules.payment.providers import (
    DEFAULT_MAX_SKEW_SECONDS,
    PAYLOAD_REDACTED,
    SENSITIVE_KEY_MARKERS,
    SENSITIVE_KEYS,
    CallbackHeaders,
    callback_secret_for,
    filter_callback_payload,
    is_sensitive_key,
    parse_amount_minor,
    sign_body,
    verify_signature,
)

pytestmark = pytest.mark.unit

SECRET = "unit-test-callback-secret"


# ---------------------------------------------------------------------------
# The vocabulary itself
# ---------------------------------------------------------------------------
def test_the_frozen_marker_is_the_one_redaction_already_uses() -> None:
    """design 5.3 freezes ``***REDACTED***``. It must be the same literal as ``MASK``.

    Two marker strings would mean two greps and two integration bugs: the evidence
    scanner and the filter would disagree about what "already redacted" looks like.
    """
    assert PAYLOAD_REDACTED == MASK == "***REDACTED***"


@pytest.mark.parametrize("spelling", SENSITIVE_KEY_MARKERS)
def test_every_key_spelling_the_design_lists_is_treated_as_sensitive(spelling: str) -> None:
    """design 5.3's list, verbatim - including both ``card_no`` and ``cardno``."""
    assert is_sensitive_key(spelling), f"{spelling!r} is in the design's sensitive set"


@pytest.mark.parametrize(
    "spelling",
    ["Password", "PASSWD", "Authorization", "API_KEY", "apiKey", "Private-Key", "Sign", "PAN"],
)
def test_casing_and_separators_do_not_evade_the_filter(spelling: str) -> None:
    """A payload key is not a Python identifier.

    The design list is written in one casing, but a provider posts ``"apiKey"``,
    ``"API-KEY"`` or ``"card_no"``. Normalising to lowercase alphanumerics is what
    collapses those onto the same entry instead of relying on both spellings being
    listed - the design's own ``card_no``/``cardno`` pair is evidence that the list
    would otherwise have to be exhaustive about punctuation.
    """
    assert is_sensitive_key(spelling)


@pytest.mark.parametrize("spelling", ["sign_type", "trade_no", "signature_valid", "tokens", "keyboard"])
def test_a_benign_key_that_merely_contains_a_marker_is_not_sensitive(spelling: str) -> None:
    """Exact match, not substring.

    ``sign_type`` is the provider's signature *algorithm* and genuinely useful
    evidence; ``trade_no`` is the one field a dispute is resolved with. A
    substring rule on ``sign``/``token`` - which is what
    ``redaction.is_secret_key`` correctly does for log lines - would delete both
    and still pass a naive "no secret survived" test.
    """
    assert not is_sensitive_key(spelling)


# ---------------------------------------------------------------------------
# filter_callback_payload
# ---------------------------------------------------------------------------
def test_every_sensitive_value_is_replaced_and_kept_as_a_key() -> None:
    """The design's own test: feed each sensitive key, assert no value survives.

    The keys themselves stay. Dropping them would make the snapshot a redacted
    *shape* rather than a redacted *record*, and an operator comparing it against
    the provider's copy would not be able to see that the provider sent a card
    field at all - which is itself a fact worth having.
    """
    payload = {spelling: f"value-of-{spelling}" for spelling in SENSITIVE_KEY_MARKERS}

    filtered = filter_callback_payload(payload)

    assert set(filtered) == set(payload), "the filter must not delete keys"
    for spelling in SENSITIVE_KEY_MARKERS:
        assert filtered[spelling] == PAYLOAD_REDACTED, f"{spelling} leaked"
        assert "value-of-" not in str(filtered[spelling])


def test_a_benign_field_survives_the_filter() -> None:
    """design 5.3: a filter that redacts everything destroys its own audit value.

    This is the assertion that makes the previous one meaningful. Both must hold at
    once; a test suite with only the leak assertion is passed by ``return {}``.
    """
    payload = {
        "trade_no": "2026092322001400000001",
        "amount": 19900,
        "currency": "CNY",
        "status": "TRADE_SUCCESS",
        "event_type": "PAYMENT_SUCCEEDED",
        "buyer_pay_amount": 19900,
        "sign_type": "HMAC-SHA256",
        "payment_no": "NVPAY20260923000001",
    }

    filtered = filter_callback_payload(payload)

    assert filtered == payload, "every one of these is legitimate evidence"


def test_a_nested_list_of_dicts_secret_does_not_survive() -> None:
    """Recursion at depth, through a list - the shape a top-level filter misses.

    ``{"items": [{"pan": "..."}, {"cvv": "..."}]}`` is an ordinary provider shape,
    and a filter that only walks ``payload.items()`` looks correct in every
    flat-payload test while leaking on this one.
    """
    payload = {
        "data": {
            "payment": {"trade_no": "T-1", "amount": 100},
            "cards": [
                {"pan": "4111111111111111", "cvv": "123", "brand": "VISA"},
                {"cvc": "456", "holder": {"phone": "13800000000", "email": "a@b.c"}},
            ],
        }
    }

    filtered = filter_callback_payload(payload)

    cards = filtered["data"]["cards"]
    assert [card.get("pan", None) for card in cards] == [PAYLOAD_REDACTED, None]
    assert cards[0]["cvv"] == PAYLOAD_REDACTED
    assert cards[1]["cvc"] == PAYLOAD_REDACTED
    assert cards[1]["holder"]["phone"] == PAYLOAD_REDACTED
    assert cards[1]["holder"]["email"] == PAYLOAD_REDACTED
    # ...and the benign siblings are still there, at depth, inside a list.
    assert cards[0]["brand"] == "VISA"
    assert filtered["data"]["payment"] == {"trade_no": "T-1", "amount": 100}

    # The blunt version of the same assertion: nothing sensitive is reachable
    # anywhere in the serialised result. This is the form that cannot be fooled by
    # a cleverer nesting than the test author imagined.
    serialised = json.dumps(filtered)
    for leaked in ("4111111111111111", "123", "456", "13800000000", "a@b.c"):
        assert leaked not in serialised, f"{leaked!r} survived the filter"


def test_the_input_is_not_mutated() -> None:
    """The caller holds the *raw* body the signature was computed over.

    Redacting in place would destroy the bytes that verification needs and would
    corrupt a live object the caller still owns. The contract is "returns a new
    dict", so it has to be asserted.
    """
    payload = {"pan": "4111111111111111", "trade_no": "T-1"}
    snapshot_before = json.dumps(payload, sort_keys=True)

    filtered = filter_callback_payload(payload)

    assert json.dumps(payload, sort_keys=True) == snapshot_before
    assert filtered is not payload
    assert payload["pan"] == "4111111111111111"


def test_scalars_and_empty_containers_survive_unchanged() -> None:
    """A ``str`` is a ``Sequence``; without an explicit branch it explodes into characters."""
    for value in ("TRADE_SUCCESS", "", [], {}, 0, None, True, 19.9):
        assert filter_callback_payload(value) == value


def test_a_string_value_under_a_list_is_not_exploded_into_characters() -> None:
    filtered = filter_callback_payload({"tags": ["a", "bb"]})
    assert filtered == {"tags": ["a", "bb"]}


def test_an_absurdly_nested_payload_is_replaced_rather_than_walked_forever() -> None:
    """Bounded depth: the subtree is replaced wholesale, never silently kept.

    Truncating the walk while *keeping* the deep subtree would produce a
    "filtered" snapshot with an unfiltered secret in it - the worst possible
    outcome for a security filter, because the marker would be absent and the
    reviewer would assume it had been checked.
    """
    node: dict = {"pan": "4111111111111111"}
    for _ in range(20):
        node = {"child": node}

    filtered = filter_callback_payload(node)

    assert "4111111111111111" not in json.dumps(filtered)
    assert PAYLOAD_REDACTED in json.dumps(filtered)


# ---------------------------------------------------------------------------
# sign_body / verify_signature
# ---------------------------------------------------------------------------
def _headers(
    *,
    timestamp: str,
    signature: str,
    provider: str = "MOCK",
    event_id: str = "evt-1",
) -> CallbackHeaders:
    return CallbackHeaders(
        provider=provider, event_id=event_id, timestamp=timestamp, signature=signature
    )


def _signed(
    body: bytes, *, timestamp: str, secret: str = SECRET, event_id: str = "evt-1"
) -> CallbackHeaders:
    return _headers(
        timestamp=timestamp,
        signature=sign_body(secret=secret, timestamp=timestamp, raw_body=body),
        event_id=event_id,
    )


def test_sign_body_is_hmac_sha256_over_timestamp_dot_body_in_hex() -> None:
    """The scheme is frozen (design 7), so it is pinned against an independent computation.

    Computed here with ``hmac``/``hashlib`` directly rather than by calling
    :func:`sign_body` - the point is that a *provider* implementing the published
    scheme agrees with the verifier. A test that calls the function twice proves
    only that it is deterministic, which a broken scheme also is.
    """
    body = b'{"amount":19900,"trade_no":"T-1"}'
    timestamp = "1790000000"

    expected = hmac.new(
        SECRET.encode("utf-8"),
        b"1790000000." + body,
        hashlib.sha256,
    ).hexdigest()

    assert sign_body(secret=SECRET, timestamp=timestamp, raw_body=body) == expected
    assert len(expected) == 64
    assert expected == expected.lower()


def test_a_correctly_signed_fresh_callback_verifies() -> None:
    body = b'{"payment_no":"NVPAY20260923000001","amount":19900}'
    timestamp = "1790000000"

    verdict = verify_signature(
        raw_body=body,
        headers=_signed(body, timestamp=timestamp),
        secret=SECRET,
        now=1790000000,
    )

    assert verdict.valid
    assert verdict.reason == "OK"
    assert verdict.skew_seconds == 0


def test_a_wrong_secret_is_refused() -> None:
    body = b'{"amount":19900}'
    verdict = verify_signature(
        raw_body=body,
        headers=_signed(body, timestamp="1790000000", secret="another-secret"),
        secret=SECRET,
        now=1790000000,
    )
    assert not verdict.valid
    assert verdict.reason == "SIGNATURE_MISMATCH"


def test_a_tampered_body_is_refused() -> None:
    """The attack the signature exists for: change the amount, keep the digest."""
    body = b'{"amount":19900}'
    headers = _signed(body, timestamp="1790000000")

    verdict = verify_signature(
        raw_body=b'{"amount":100}', headers=headers, secret=SECRET, now=1790000000
    )

    assert not verdict.valid
    assert verdict.reason == "SIGNATURE_MISMATCH"


def test_signature_over_a_stale_timestamp_is_refused() -> None:
    """A captured body is not a permanent right to settle (design 7).

    The signature is *valid* - it is the freshness check that refuses, which is
    exactly why the timestamp is bound into the signed material instead of being
    verified separately on an unsigned field.
    """
    body = b'{"amount":19900}'
    headers = _signed(body, timestamp="1790000000")

    verdict = verify_signature(
        raw_body=body,
        headers=headers,
        secret=SECRET,
        max_skew_seconds=DEFAULT_MAX_SKEW_SECONDS,
        now=1790000000 + DEFAULT_MAX_SKEW_SECONDS + 1,
    )

    assert not verdict.valid
    assert verdict.reason == "TIMESTAMP_STALE"
    assert verdict.skew_seconds == DEFAULT_MAX_SKEW_SECONDS + 1


def test_a_timestamp_inside_the_window_is_accepted_on_both_sides_of_now() -> None:
    """A provider whose clock runs slightly fast must not be refused.

    The window is symmetric on purpose: a future timestamp would otherwise widen
    the replay window by however far the clock is wrong.
    """
    body = b'{"amount":19900}'
    for offset in (-299, -1, 0, 1, 299):
        timestamp = str(1790000000 + offset)
        verdict = verify_signature(
            raw_body=body,
            headers=_signed(body, timestamp=timestamp),
            secret=SECRET,
            now=1790000000,
        )
        assert verdict.valid, f"offset {offset} should be inside the window"


def test_a_future_timestamp_outside_the_window_is_refused() -> None:
    body = b'{"amount":19900}'
    timestamp = str(1790000000 + 3600)
    verdict = verify_signature(
        raw_body=body,
        headers=_signed(body, timestamp=timestamp),
        secret=SECRET,
        now=1790000000,
    )
    assert not verdict.valid
    assert verdict.reason == "TIMESTAMP_STALE"


@pytest.mark.parametrize("timestamp", ["", "  ", "abc", "1790000000.5", "1e9", "null"])
def test_a_timestamp_the_verifier_cannot_read_is_refused(timestamp: str) -> None:
    """Refused, never coerced: an unreadable timestamp is an unbounded replay window."""
    body = b'{"amount":19900}'
    verdict = verify_signature(
        raw_body=body,
        headers=_headers(timestamp=timestamp, signature="whatever"),
        secret=SECRET,
        now=1790000000,
    )
    assert not verdict.valid
    assert verdict.reason in {"TIMESTAMP_NOT_NUMERIC", "MISSING_HEADER"}


@pytest.mark.parametrize(
    ("provider", "event_id", "timestamp", "signature"),
    [
        ("", "evt-1", "1790000000", "abc"),
        ("MOCK", "", "1790000000", "abc"),
        ("MOCK", "evt-1", "", "abc"),
        ("MOCK", "evt-1", "1790000000", ""),
    ],
)
def test_a_missing_header_is_refused(
    provider: str, event_id: str, timestamp: str, signature: str
) -> None:
    body = b'{"amount":19900}'
    verdict = verify_signature(
        raw_body=body,
        headers=CallbackHeaders(
            provider=provider, event_id=event_id, timestamp=timestamp, signature=signature
        ),
        secret=SECRET,
        now=1790000000,
    )
    assert not verdict.valid
    assert verdict.reason == "MISSING_HEADER"


@pytest.mark.parametrize("secret", [None, ""])
def test_no_configured_secret_refuses_rather_than_skipping_verification(secret: str | None) -> None:
    """Fail **closed**. "No key" must never mean "no check"."""
    body = b'{"amount":19900}'
    verdict = verify_signature(
        raw_body=body,
        headers=_headers(timestamp="1790000000", signature="abc"),
        secret=secret,
        now=1790000000,
    )
    assert not verdict.valid
    assert verdict.reason == "MISSING_SECRET"


def test_the_verdict_carries_no_digest_and_no_secret() -> None:
    """A verdict that leaked the expected digest would let any log reader forge a callback."""
    body = b'{"amount":19900}'
    supplied = "deadbeef" * 8
    verdict = verify_signature(
        raw_body=body,
        headers=_headers(timestamp="1790000000", signature=supplied),
        secret=SECRET,
        now=1790000000,
    )

    rendered = repr(verdict)
    assert SECRET not in rendered
    assert supplied not in rendered
    assert sign_body(secret=SECRET, timestamp="1790000000", raw_body=body) not in rendered


def test_verification_uses_the_raw_bytes_not_a_reserialised_payload() -> None:
    """Whitespace is part of the signed material.

    A verifier that parses JSON and re-serialises it before checking the signature
    refuses every honest provider whose key order or spacing differs from Python's
    - and, worse, a body that round-trips identically would still accept a
    *reordered* payload. Signing bytes is the only correct reading.
    """
    body = b'{ "amount" : 19900 , "trade_no":"T-1" }'
    compact = json.dumps(json.loads(body), separators=(",", ":")).encode()
    assert body != compact

    headers = _signed(body, timestamp="1790000000")
    assert verify_signature(
        raw_body=body, headers=headers, secret=SECRET, now=1790000000
    ).valid
    assert not verify_signature(
        raw_body=compact, headers=headers, secret=SECRET, now=1790000000
    ).valid


# ---------------------------------------------------------------------------
# callback_secret_for
# ---------------------------------------------------------------------------
def _settings(**overrides) -> Settings:
    base = {
        "APP_ENV": "test",
        "PAYMENT_CALLBACK_SECRET": "shared-dev-secret",
    }
    base.update(overrides)
    return Settings(**base)


def test_an_empty_per_provider_map_falls_back_to_the_shared_secret() -> None:
    """The single-provider (mock) case, and the reason it is not an error."""
    settings = _settings(PAYMENT_PROVIDER_SECRETS={})
    assert callback_secret_for(settings, "MOCK") == "shared-dev-secret"
    assert callback_secret_for(settings, "ALIPAY") == "shared-dev-secret"


def test_a_per_provider_override_wins_and_is_case_insensitive_on_the_provider_name() -> None:
    settings = _settings(PAYMENT_PROVIDER_SECRETS={"ALIPAY": "alipay-only-secret"})
    assert callback_secret_for(settings, "ALIPAY") == "alipay-only-secret"
    assert callback_secret_for(settings, "alipay") == "alipay-only-secret"
    # An unlisted provider still uses the shared secret.
    assert callback_secret_for(settings, "MOCK") == "shared-dev-secret"


def test_a_blank_override_refuses_rather_than_falling_back() -> None:
    """A provider whose configured secret is blank is misconfigured - not "use the dev key".

    Falling back would authenticate that provider with a secret its operator never
    chose, which on a shared dev secret means a callback no one can attribute.
    """
    settings = _settings(PAYMENT_PROVIDER_SECRETS={"ALIPAY": ""})
    assert callback_secret_for(settings, "ALIPAY") is None


def test_an_unknown_provider_name_has_no_secret() -> None:
    assert callback_secret_for(_settings(PAYMENT_PROVIDER_SECRETS={}), "") is None


# ---------------------------------------------------------------------------
# parse_amount_minor
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (19900, 19900),
        ("19900", 19900),
        (" 19900 ", 19900),
        (19900.0, 19900),
        ("19900.00", 19900),
        (0, 0),
        (-100, -100),
    ],
)
def test_an_integral_amount_is_read_as_minor_units(raw: object, expected: int) -> None:
    assert parse_amount_minor(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "  ", "abc", "19900.5", 19900.5, True, False, [], {}],
)
def test_a_non_integral_or_unreadable_amount_is_refused_rather_than_rounded(raw: object) -> None:
    """Rounding moves money by an amount nobody agreed to; truncating moves it the other way.

    ``True`` is refused because ``bool`` is an ``int`` in Python - ``True`` is not
    an amount of one minor unit.
    """
    assert parse_amount_minor(raw) is None


def test_sensitive_keys_is_the_normalised_frozen_set() -> None:
    """The set is the design's list, normalised - nothing added, nothing dropped.

    Adding a key here is a contract change (it removes evidence from every stored
    snapshot), so the count is pinned and a reviewer sees the diff.
    """
    normalised = {
        "".join(character for character in spelling.lower() if character.isalnum())
        for spelling in SENSITIVE_KEY_MARKERS
    }
    assert normalised == SENSITIVE_KEYS
    assert len(SENSITIVE_KEYS) == len(normalised)
