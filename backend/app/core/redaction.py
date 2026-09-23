"""Purpose-driven redaction of sensitive values.

Spec references:
    §43  payment callback snapshots must be sensitive-field filtered and must
         never store secrets.
    §48  idempotency ``response_snapshot`` stores only non-sensitive summaries.
    §64  agent tool-call snapshots are redacted *before* persistence.
    §85  no hidden chain-of-thought may be emitted.
    §94  minimise by purpose before sending to an LLM, MCP, trace or audit
         snapshot; mask phone / email / address by default; never record
         passwords, access tokens, refresh tokens, API keys, client secrets or
         database passwords.
    §132 logs must never contain any of the above.

Design notes:
    Redaction is applied **on the way in**, not on the way out. A snapshot
    helper returns a *new* structure; the caller persists the returned value.
    Mutating a live ORM object in place to "sanitise" it would corrupt business
    data, so nothing here writes back.

    Two independent concerns are handled:
      1. **Secret removal** - key-based, because a token's *value* has no
         reliable shape. Anything whose key looks secret is dropped entirely
         (replaced by a marker), not masked.
      2. **PII masking** - value-based and purpose-aware, because a phone number
         may appear under many different keys or inside free text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Substrings that mark a key as secret-bearing. Matched case-insensitively
#: against the *normalised* key (lowercased, non-alphanumerics collapsed).
_SECRET_KEY_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "apikey",
    "api_key",
    "accesskey",
    "secretkey",
    "privatekey",
    "clientsecret",
    "credential",
    "authorization",
    "auth",
    "cookie",
    "sessionid",
    "signature",
    "sign",
    "salt",
    "otp",
    "pin",
    "cvv",
    "cardnumber",
    "refreshtoken",
    "idtoken",
    "bearertoken",
    "jwt",
)

#: Keys that are *safe* even though a marker above would otherwise catch them.
#: Kept deliberately tiny and reviewed: over-broad allow-lists are how secrets
#: leak.
_SECRET_KEY_ALLOWLIST: frozenset[str] = frozenset(
    {
        "authorization_status",  # a status enum, not a header
        "auth_sessions_count",  # an aggregate
        "token_input",  # LLM usage counters, not credentials
        "token_output",
        "token_count",
        "tokens_used",
        "signature_valid",  # a boolean verdict about a signature
        "sign_verified",
    }
)

#: Keys that indicate hidden chain-of-thought, which must never be emitted (§85).
_COT_KEY_MARKERS: tuple[str, ...] = (
    "chain_of_thought",
    "chainofthought",
    "reasoning_trace",
    "raw_reasoning",
    "scratchpad",
    "internal_thought",
    "hidden_reasoning",
    "thought_process",
)


class RedactionPurpose(StrEnum):
    """Why a payload is being redacted - drives how aggressive to be."""

    #: Going to a third-party LLM: strongest masking, no raw PII at all.
    LLM = "llm"
    #: Going out over MCP to an external client.
    MCP = "mcp"
    #: Attached to an OTel span / structured log line.
    TRACE = "trace"
    #: Written to the append-only audit table (spec §133).
    AUDIT = "audit"
    #: Persisted as a tool-call snapshot (spec §64).
    TOOL_SNAPSHOT = "tool_snapshot"
    #: Persisted as an idempotency response summary (spec §48).
    IDEMPOTENCY = "idempotency"
    #: Persisted as a payment callback payload (spec §43).
    CALLBACK = "callback"


#: Purposes that must not retain *any* PII value, even masked.
_NO_PII_PURPOSES: frozenset[RedactionPurpose] = frozenset(
    {RedactionPurpose.LLM, RedactionPurpose.MCP, RedactionPurpose.TRACE}
)

MASK = "***REDACTED***"
#: Marker for a secret key whose value was removed. Distinct from ``MASK`` so
#: operators can tell "we threw away a credential" from "we masked a phone".
#: The identifier merely *names* a redaction marker (ruff S105 false positive).
SECRET_REMOVED = "[SECRET_REMOVED]"  # noqa: S105
COT_REMOVED = "[REASONING_WITHHELD]"
TRUNCATED = "[TRUNCATED]"

#: Hard caps so a snapshot can never become an unbounded blob (§48, §64).
MAX_DEPTH = 8
MAX_STRING_LEN = 4096
MAX_SEQUENCE_LEN = 200
MAX_KEYS_PER_MAPPING = 200


# ---------------------------------------------------------------------------
# Value patterns
# ---------------------------------------------------------------------------
# Deliberately conservative: a false positive costs a masked digit, a false
# negative leaks a phone number.

_RE_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]{2,}\b")

# Mainland China mobile (11 digits starting 1[3-9]) plus a permissive
# international form. Both are covered because demo data uses +86.
_RE_PHONE_CN = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")
_RE_PHONE_INTL = re.compile(r"(?<!\d)\+\d{7,15}(?!\d)")

# 18-digit CN ID card with the X check digit.
_RE_ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")

# 16-19 digit bank card, optionally space/dash grouped.
_RE_BANK_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){15,19}\d(?!\d)")

_RE_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")
_RE_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_RE_AWS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")
_RE_OPENAI_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_RE_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*)://([^:/@\s]+):([^@\s]+)@")

# Chinese address heuristic: province/city/street markers plus a run of digits.
_RE_ADDRESS_CN = re.compile(
    r"[\u4e00-\u9fa5]{2,10}(?:省|市|自治区)[\u4e00-\u9fa5]{0,15}"
    r"(?:区|县|市)[\u4e00-\u9fa5]{0,20}(?:路|街|道|巷|号)[\u4e00-\u9fa50-9\-]{0,40}"
)

_FREE_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_RE_EMAIL, "***@***"),
    (_RE_JWT, "[TOKEN]"),
    (_RE_BEARER, r"\1 [TOKEN]"),
    (_RE_AWS_KEY, "[AWS_KEY]"),
    (_RE_OPENAI_KEY, "[API_KEY]"),
    (_RE_URL_CREDENTIALS, r"\1://***:***@"),
    (_RE_ID_CARD, "[ID_CARD]"),
    (_RE_PHONE_CN, "[PHONE]"),
    (_RE_PHONE_INTL, "[PHONE]"),
    (_RE_ADDRESS_CN, "[ADDRESS]"),
)


def _normalise_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


#: Normalised once at import so lookups compare like with like. (The keys below
#: are written with underscores for readability, but ``_normalise_key`` strips
#: non-alphanumerics - so an un-normalised allow-list silently never matches,
#: which is exactly the bug this comment exists to prevent.)
_NORMALISED_ALLOWLIST: frozenset[str] = frozenset(
    re.sub(r"[^a-z0-9]", "", entry.lower()) for entry in _SECRET_KEY_ALLOWLIST
)


def is_secret_key(key: str) -> bool:
    """True when a mapping key names a credential rather than business data."""
    normalised = _normalise_key(key)
    if normalised in _NORMALISED_ALLOWLIST:
        return False
    return any(marker.replace("_", "") in normalised for marker in _SECRET_KEY_MARKERS)


def is_chain_of_thought_key(key: str) -> bool:
    normalised = _normalise_key(key)
    return any(marker.replace("_", "") in normalised for marker in _COT_KEY_MARKERS)


# ---------------------------------------------------------------------------
# Scalar redaction
# ---------------------------------------------------------------------------
def mask_email(value: str) -> str:
    local, _, domain = value.partition("@")
    if not domain:
        return MASK
    head = local[:1] if local else ""
    return f"{head}***@{domain}"


def mask_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) < 7:
        return MASK
    return f"{digits[:3]}****{digits[-4:]}"


def mask_address(value: str) -> str:
    """Keep only coarse geography; drop the specific dwelling."""
    if len(value) <= 6:
        return MASK
    return f"{value[:6]}****"


def mask_name(value: str) -> str:
    """Keep only the first character of a personal name.

    The order API returns receiver names already masked (§94): an order list is a
    common exfiltration target and an operator rarely needs the full value. A
    one-character name still loses something - leaving it untouched would make the
    mask a no-op for exactly the shortest (and most identifying) names.
    """
    if not value:
        return value
    stars = max(1, min(len(value) - 1, 3))
    return f"{value[:1]}{'*' * stars}"


def redact_text(value: str, purpose: RedactionPurpose) -> str:
    """Scrub credentials - and, where the purpose demands it, PII - from free text.

    Credentials are always removed regardless of purpose; there is no situation
    in which persisting a bearer token in a log or snapshot is correct.
    """
    if not value:
        return value
    result = value
    for pattern, replacement in _FREE_TEXT_PATTERNS:
        result = pattern.sub(replacement, result)
    if purpose in _NO_PII_PURPOSES:
        # Already handled by the patterns above; nothing further to strip.
        return result
    return result


# ---------------------------------------------------------------------------
# Structured redaction
# ---------------------------------------------------------------------------
def redact(
    payload: Any,
    *,
    purpose: RedactionPurpose = RedactionPurpose.AUDIT,
    max_depth: int = MAX_DEPTH,
    _depth: int = 0,
) -> Any:
    """Return a redacted *copy* of ``payload``.

    Guarantees:
      * mappings are copied, never mutated in place;
      * secret-named keys are replaced by :data:`SECRET_REMOVED`;
      * chain-of-thought keys are replaced by :data:`COT_REMOVED` (§85);
      * free-text values are scrubbed for credentials in every purpose;
      * depth, string length, sequence length and key count are bounded so a
        snapshot cannot grow without limit.
    """
    if _depth > max_depth:
        return TRUNCATED

    if payload is None or isinstance(payload, bool | int | float):
        return payload

    if isinstance(payload, str):
        scrubbed = redact_text(payload, purpose)
        if len(scrubbed) > MAX_STRING_LEN:
            return scrubbed[:MAX_STRING_LEN] + TRUNCATED
        return scrubbed

    if isinstance(payload, bytes | bytearray):
        return f"<bytes:{len(payload)}>"

    if isinstance(payload, Mapping):
        out: dict[str, Any] = {}
        for index, (raw_key, raw_value) in enumerate(payload.items()):
            if index >= MAX_KEYS_PER_MAPPING:
                out["__truncated__"] = f"{len(payload) - MAX_KEYS_PER_MAPPING} more keys"
                break
            key = str(raw_key)
            if is_chain_of_thought_key(key):
                out[key] = COT_REMOVED
                continue
            if is_secret_key(key):
                out[key] = SECRET_REMOVED
                continue
            # Purpose-aware value masking for well-known PII field names, so
            # `phone` is masked even when the value does not match a pattern.
            out[key] = _redact_field(key, raw_value, purpose, max_depth, _depth)
        return out

    if isinstance(payload, Sequence):
        items: list[Any] = []
        for index, item in enumerate(payload):
            if index >= MAX_SEQUENCE_LEN:
                items.append(f"{TRUNCATED} +{len(payload) - MAX_SEQUENCE_LEN}")
                break
            items.append(redact(item, purpose=purpose, max_depth=max_depth, _depth=_depth + 1))
        return items

    if isinstance(payload, Iterable):
        return redact(list(payload), purpose=purpose, max_depth=max_depth, _depth=_depth + 1)

    return f"<{type(payload).__name__}>"


_FIELD_MASKERS: tuple[tuple[tuple[str, ...], Any], ...] = (
    (("email", "mail", "emailaddress"), mask_email),
    (("phone", "mobile", "tel", "telephone", "phonenumber", "contactphone"), mask_phone),
    (("address", "addr", "shippingaddress", "receiveraddress", "fulladdress"), mask_address),
    (("idcard", "idnumber", "cardno", "bankcard"), lambda _v: MASK),
)


def _redact_field(
    key: str,
    value: Any,
    purpose: RedactionPurpose,
    max_depth: int,
    depth: int,
) -> Any:
    normalised = _normalise_key(key)
    if isinstance(value, str) and value:
        for markers, masker in _FIELD_MASKERS:
            if normalised in markers or any(marker in normalised for marker in markers):
                if purpose in _NO_PII_PURPOSES:
                    return MASK
                return masker(value)
    return redact(value, purpose=purpose, max_depth=max_depth, _depth=depth + 1)


# ---------------------------------------------------------------------------
# Audit snapshots
# ---------------------------------------------------------------------------
def audit_snapshot(payload: Any) -> Any:
    """Redact a before/after snapshot for the append-only audit table (§133)."""
    return redact(payload, purpose=RedactionPurpose.AUDIT)


def tool_snapshot(payload: Any) -> Any:
    """Redact an agent tool-call input/output before persistence (§64)."""
    return redact(payload, purpose=RedactionPurpose.TOOL_SNAPSHOT)


def llm_payload(payload: Any) -> Any:
    """Redact before handing data to a model provider (§94)."""
    return redact(payload, purpose=RedactionPurpose.LLM)


def mcp_payload(payload: Any) -> Any:
    """Redact before returning data over MCP (§94)."""
    return redact(payload, purpose=RedactionPurpose.MCP)


def trace_payload(payload: Any) -> Any:
    """Redact before attaching to a span attribute or log record (§131, §132)."""
    return redact(payload, purpose=RedactionPurpose.TRACE)


def assertion_that_no_secret_remains(payload: Any) -> list[str]:
    """Return the key paths still considered secret-bearing.

    Used by tests and by the evidence scanner: an empty list means clean.
    """
    problems: list[str] = []

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            return
        if isinstance(node, Mapping):
            for key, value in node.items():
                key_str = str(key)
                if is_secret_key(key_str) and value not in (SECRET_REMOVED, None):
                    problems.append(f"{path}.{key_str}")
                walk(value, f"{path}.{key_str}", depth + 1)
        elif isinstance(node, Sequence) and not isinstance(node, str | bytes):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]", depth + 1)

    walk(payload, "$", 0)
    return problems


__all__ = [
    "COT_REMOVED",
    "MASK",
    "SECRET_REMOVED",
    "RedactionPurpose",
    "assertion_that_no_secret_remains",
    "audit_snapshot",
    "is_chain_of_thought_key",
    "is_secret_key",
    "llm_payload",
    "mask_address",
    "mask_email",
    "mask_name",
    "mask_phone",
    "mcp_payload",
    "redact",
    "redact_text",
    "tool_snapshot",
    "trace_payload",
]
