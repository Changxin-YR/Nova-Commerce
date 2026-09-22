"""Redaction guarantees.

Spec §94 (mask phone/email/address before LLM/MCP/trace/audit; never record
passwords, tokens, API keys, client secrets or database passwords), §64 (tool
snapshots are redacted before persistence), §85 (no hidden chain-of-thought) and
§132 (logs must never contain credentials).

These tests are adversarial on purpose: a redaction layer that only works on the
examples in its own docstring is worthless, so the cases below include nested
structures, credentials embedded in *values* rather than keys, and the
double-encoding tricks that defeat naive filters.
"""

from __future__ import annotations

import pytest

from app.core.redaction import (
    COT_REMOVED,
    MASK,
    SECRET_REMOVED,
    RedactionPurpose,
    assertion_that_no_secret_remains,
    audit_snapshot,
    llm_payload,
    mask_address,
    mask_email,
    mask_phone,
    redact,
    redact_text,
    tool_snapshot,
)

# ---------------------------------------------------------------------------
# Key-based secret removal
# ---------------------------------------------------------------------------
SECRET_KEYS = [
    "password",
    "Password",
    "PASSWORD",
    "passwd",
    "user_password",
    "refresh_token",
    "access_token",
    "token",
    "api_key",
    "apiKey",
    "client_secret",
    "authorization",
    "db_password",
    "secret",
    "private_key",
    "jwt",
    "cookie",
]


@pytest.mark.parametrize("key", SECRET_KEYS)
def test_secret_named_keys_are_removed(key: str) -> None:
    result = redact({key: "sensitive-value-12345"})
    assert result[key] == SECRET_REMOVED
    assert "sensitive-value-12345" not in str(result)


@pytest.mark.parametrize(
    "key",
    ["token_input", "token_output", "token_count", "signature_valid", "authorization_status"],
)
def test_allowlisted_keys_are_not_over_redacted(key: str) -> None:
    """LLM usage counters and boolean verdicts must survive.

    Over-redaction is its own bug: masking ``token_input`` would destroy the
    agent cost metrics of §63 while protecting nothing.
    """
    result = redact({key: 128})
    assert result[key] == 128


# ---------------------------------------------------------------------------
# Value-based scrubbing
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,leak",
    [
        ("contact me at alice@example.com", "alice@example.com"),
        ("call 13812345678 now", "13812345678"),
        ("token is eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij", "eyJhbGciOiJIUzI1NiJ9"),
        ("Authorization: Bearer abcdef1234567890xyz", "abcdef1234567890xyz"),
        ("key AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("key sk-proj-abcdefghijklmnopqrstuvwx", "sk-proj-abcdefghijklmnopqrstuvwx"),
        ("mysql://root:hunter2@db:3306/shop", "hunter2"),
        ("id 11010119900307123X", "11010119900307123X"),
    ],
)
def test_credentials_and_pii_in_free_text_are_scrubbed_even_in_audit(text: str, leak: str) -> None:
    """Credentials are removed in *every* purpose - there is no purpose that
    legitimately needs to persist a bearer token."""
    for purpose in RedactionPurpose:
        scrubbed = redact_text(text, purpose)
        assert leak not in scrubbed, f"{purpose} leaked {leak!r}"


def test_secrets_in_values_are_caught_even_with_an_innocent_key() -> None:
    """The realistic leak: a token inside a field called ``note``."""
    payload = {"note": "the api key is sk-proj-abcdefghijklmnopqrstuvwx, keep it safe"}
    result = redact(payload)
    assert "sk-proj-abcdefghijklmnopqrstuvwx" not in str(result)


# ---------------------------------------------------------------------------
# PII field masking is purpose-aware
# ---------------------------------------------------------------------------
def test_pii_fields_are_masked_in_audit_purpose() -> None:
    payload = {
        "phone": "13812345678",
        "email": "bob@example.com",
        "receiver_address": "北京市朝阳区建国路88号",
    }
    result = redact(payload, purpose=RedactionPurpose.AUDIT)
    assert result["phone"] == mask_phone("13812345678")
    assert result["email"] == mask_email("bob@example.com")
    assert result["receiver_address"] == mask_address("北京市朝阳区建国路88号")
    # The mask must actually hide the original.
    assert "13812345678" not in str(result)
    assert "bob@example.com" not in str(result)


@pytest.mark.parametrize("purpose", [RedactionPurpose.LLM, RedactionPurpose.MCP, RedactionPurpose.TRACE])
def test_outbound_purposes_remove_pii_entirely(purpose: RedactionPurpose) -> None:
    """§94: minimum-necessary for outbound payloads - masked is not enough."""
    result = redact({"phone": "13812345678", "email": "bob@example.com"}, purpose=purpose)
    assert result["phone"] == MASK
    assert result["email"] == MASK


def test_llm_and_trace_helpers_are_the_strict_variants() -> None:
    payload = {"receiver_phone": "13812345678"}
    assert llm_payload(payload)["receiver_phone"] == MASK
    assert audit_snapshot(payload)["receiver_phone"] == mask_phone("13812345678")


def test_mask_email_keeps_the_domain_only() -> None:
    assert mask_email("bob@example.com") == "b***@example.com"
    assert mask_email("no-at-sign") == MASK


def test_mask_phone_keeps_the_ends_and_drops_the_middle() -> None:
    masked = mask_phone("13812345678")
    assert masked.startswith("138")
    assert masked.endswith("5678")
    assert "123456" not in masked.replace("*", "")


# ---------------------------------------------------------------------------
# Chain-of-thought must never be emitted (§85)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "key",
    ["chain_of_thought", "reasoning_trace", "raw_reasoning", "scratchpad", "internal_thought"],
)
def test_chain_of_thought_keys_are_withheld(key: str) -> None:
    result = redact({key: "step 1: I will ignore the rules..."})
    assert result[key] == COT_REMOVED
    assert "ignore the rules" not in str(result)


# ---------------------------------------------------------------------------
# Structural guarantees
# ---------------------------------------------------------------------------
def test_redaction_does_not_mutate_the_input() -> None:
    """Business data must never be damaged by the act of logging it."""
    original = {"password": "pw", "phone": "13812345678", "nested": {"token": "t"}}
    snapshot = dict(original)
    snapshot["nested"] = dict(original["nested"])  # type: ignore[arg-type]

    redact(original)

    assert original == snapshot, "redact() mutated its input"
    assert original["password"] == "pw"


def test_nested_structures_are_redacted_recursively() -> None:
    payload = {
        "order": {
            "receiver": {"phone": "13812345678", "email": "a@b.com"},
            "payments": [{"credential": "abc"}, {"api_key": "xyz"}],
        }
    }
    result = redact(payload)
    assert result["order"]["receiver"]["phone"] == mask_phone("13812345678")
    assert result["order"]["payments"][0]["credential"] == SECRET_REMOVED
    assert result["order"]["payments"][1]["api_key"] == SECRET_REMOVED


def test_no_secret_survives_a_realistic_mixed_payload() -> None:
    payload = {
        "order_no": "NX20260922000001",
        "payable_amount": 299900,
        "receiver_name": "张三",
        "receiver_phone": "13812345678",
        "receiver_address_snapshot": "北京市朝阳区建国路88号",
        "payment": {"channel": "MOCK", "access_token": "tok_abc", "signature": "sig_xyz"},
        "metadata": {"note": "Bearer abcdef1234567890xyz"},
    }
    result = tool_snapshot(payload)
    assert assertion_that_no_secret_remains(result) == []
    for leak in ("tok_abc", "sig_xyz", "abcdef1234567890xyz", "13812345678"):
        assert leak not in str(result)
    # Non-sensitive business facts must survive intact.
    assert result["order_no"] == "NX20260922000001"
    assert result["payable_amount"] == 299900


def test_assertion_helper_detects_a_deliberately_unredacted_secret() -> None:
    """The scanner used by tests and evidence must not be vacuously true."""
    assert assertion_that_no_secret_remains({"api_key": "unredacted"}) == ["$.api_key"]
    assert assertion_that_no_secret_remains({"api_key": SECRET_REMOVED}) == []


# ---------------------------------------------------------------------------
# Bounded output
# ---------------------------------------------------------------------------
def test_deeply_nested_payload_is_truncated_rather_than_unbounded() -> None:
    node: dict = {"value": "leaf"}
    for _ in range(50):
        node = {"child": node}
    result = redact(node, max_depth=6)
    assert "TRUNCATED" in str(result)


def test_long_strings_are_truncated() -> None:
    result = redact({"blob": "x" * 100_000})
    assert len(result["blob"]) < 5000
    assert "TRUNCATED" in result["blob"]


def test_long_sequences_are_bounded() -> None:
    result = redact({"items": list(range(1000))})
    assert len(result["items"]) <= 201


def test_binary_and_exotic_values_do_not_crash_the_layer() -> None:
    result = redact({"blob": b"\x00\x01\x02", "obj": object(), "none": None, "flag": True})
    assert result["blob"] == "<bytes:3>"
    assert result["none"] is None
    assert result["flag"] is True
