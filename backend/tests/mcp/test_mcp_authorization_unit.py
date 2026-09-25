"""Authorization tests that need no infrastructure: the verifier and the policy table.

The property under test is that **a token is the only source of authority**, and
that the four-way intersection of spec section 90 is enforced independently of
transport. These tests therefore present tokens to the production
:class:`~app.mcp.auth.NovaTokenVerifier` rather than asserting on a stub, and assert
on the *reason* for a refusal as well as the fact of one - a verifier that refused
everything would satisfy "wrong issuer is rejected" while being useless, so each
negative case is paired with the positive control it differs from by exactly one
property.
"""

from __future__ import annotations

import asyncio
import base64
import secrets

import pytest

from app.mcp.auth import (
    InvalidTokenClaimsError,
    NovaTokenVerifier,
    build_principal,
    extract_scopes,
)
from app.mcp.policy import (
    FORBIDDEN_TOOL_NAMES,
    TOOL_SPECS,
    TOOL_SPECS_BY_NAME,
    authorize_tool,
    authorized_tool_names,
)
from app.modules.identity.enums import DataScope, PermissionCode, UserType
from tests.mcp.support import (
    TEST_ISSUER,
    TEST_RESOURCE,
    all_tool_scopes,
    mint_token,
    principal_for,
    settings_for_tests,
    tamper,
)

pytestmark = pytest.mark.unit


def _verify(token: str, settings=None):
    """Run the async verifier synchronously. One place, so every case is identical."""
    resolved = settings or settings_for_tests()
    return asyncio.run(NovaTokenVerifier(resolved).verify_token(token))


# ---------------------------------------------------------------------------
# Signature, issuer, audience, expiry
# ---------------------------------------------------------------------------
def test_valid_token_is_accepted_and_carries_a_principal():
    settings = settings_for_tests()
    verified = _verify(mint_token(settings=settings), settings)
    assert verified is not None
    assert verified.subject == "7001"
    assert verified.resource == TEST_RESOURCE
    assert "nova.read" in verified.scopes
    # The principal the verifier resolved, not one rebuilt from the raw payload: this
    # is the object every tool body receives, so asserting on anything else would
    # test a second code path that does not exist in production.
    principal = verified.claims["nova_principal"]
    assert principal.user_id == 7001
    assert principal.merchant_id == 31
    assert principal.data_scope is DataScope.MERCHANT
    assert principal.is_staff is True


def test_wrong_issuer_is_rejected():
    settings = settings_for_tests()
    token = mint_token(settings=settings, issuer="http://evil.example.com/realms/nova")
    assert _verify(token, settings) is None


def test_tampered_signature_is_rejected():
    settings = settings_for_tests()
    assert _verify(tamper(mint_token(settings=settings)), settings) is None


def test_token_signed_with_another_key_is_rejected():
    settings = settings_for_tests()
    # A second, independently generated key of the same shape: the point is that the
    # signature verifies against *a* key, just not the configured one.
    other_key = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii")
    forged = mint_token(settings=settings, key=other_key)
    assert _verify(forged, settings) is None


def test_expired_token_is_rejected():
    settings = settings_for_tests()
    assert _verify(mint_token(settings=settings, expires_in=-60), settings) is None


def test_not_yet_valid_token_is_rejected():
    settings = settings_for_tests()
    # nbf one hour in the future: PyJWT's ImmatureSignatureError, which is a
    # different failure from "expired" and must not be accepted either.
    assert _verify(mint_token(settings=settings, not_before_offset=3600), settings) is None


def test_wrong_audience_is_rejected():
    settings = settings_for_tests()
    token = mint_token(settings=settings, audience="http://127.0.0.1:8020/other-service")
    assert _verify(token, settings) is None


def test_audience_list_containing_another_service_only_is_rejected():
    """An audience *list* is accepted by PyJWT on any match; the verifier must require ours.

    This is the case a naive ``audience=`` argument misses: with a list audience the
    library returns the token as valid because one entry matched, even though the
    entry that matched is not this server.
    """
    settings = settings_for_tests()
    import jwt

    now = int(__import__("time").time())
    payload = {
        "sub": "7001",
        "iss": settings.issuer_url,
        "aud": ["http://127.0.0.1:8020/other-service"],
        "iat": now,
        "exp": now + 300,
        "data_scope": DataScope.MERCHANT.value,
        "permissions": ["order:read"],
        "user_type": UserType.STAFF.value,
        "merchant_id": 31,
        "scope": "nova.read",
    }
    token = jwt.encode(payload, settings.hmac_secret.get_secret_value(), algorithm="HS256")
    assert _verify(token, settings) is None


def test_algorithm_substitution_is_rejected():
    """``alg: none`` must not be accepted, whatever the key material is."""
    settings = settings_for_tests()
    import jwt

    now = int(__import__("time").time())
    unsecured = jwt.encode(
        {
            "sub": "7001",
            "iss": settings.issuer_url,
            "aud": settings.resource_server_url,
            "iat": now,
            "exp": now + 300,
            "data_scope": DataScope.ALL.value,
            "scope": "nova.read",
        },
        key="",
        algorithm="none",
    )
    assert _verify(unsecured, settings) is None


def test_missing_base_scope_is_rejected_at_verification():
    settings = settings_for_tests(required_scopes=["nova.read"])
    assert _verify(mint_token(settings=settings, scopes=("catalog:read",)), settings) is None


def test_verifier_without_key_material_refuses_everything():
    """No key is not "skip verification" - it is "no token can be valid"."""
    settings = settings_for_tests(hmac_secret=None)
    assert settings.has_key_material is False
    assert _verify(mint_token(settings=settings), settings) is None


def test_empty_and_garbage_tokens_return_none_rather_than_raising():
    settings = settings_for_tests()
    for candidate in ("", "   ", "not-a-jwt", "a.b.c", "ey.ey.ey"):
        assert _verify(candidate, settings) is None


def test_unknown_data_scope_claim_is_refused_not_defaulted():
    """A malformed scope must fail closed; defaulting to ALL would be a grant."""
    with pytest.raises(InvalidTokenClaimsError):
        build_principal({"sub": "1", "data_scope": "EVERYTHING"})
    with pytest.raises(InvalidTokenClaimsError):
        build_principal({"sub": "1"})


def test_scope_claim_spellings_are_both_understood():
    assert extract_scopes({"scope": "nova.read nova.write"}) == {"nova.read", "nova.write"}
    assert extract_scopes({"scp": ["nova.read"]}) == {"nova.read"}
    assert extract_scopes({"scope": "nova.read, nova.write"}) == {"nova.read", "nova.write"}


def test_typescript_staff_without_merchant_is_still_staff_but_unscoped():
    principal = build_principal(
        {
            "sub": "5",
            "user_type": UserType.STAFF.value,
            "merchant_id": None,
            "data_scope": DataScope.NONE.value,
            "permissions": [],
            "roles": [],
        }
    )
    assert principal.is_staff is True
    assert principal.merchant_id is None
    assert principal.data_scope is DataScope.NONE


# ---------------------------------------------------------------------------
# The four-way intersection
# ---------------------------------------------------------------------------
def test_full_surface_is_visible_to_a_fully_privileged_staff_token():
    principal = principal_for(data_scope=DataScope.ALL, permissions=frozenset({p.value for p in PermissionCode}))
    names = authorized_tool_names(
        principal=principal, token_scopes=frozenset(all_tool_scopes()), write_scope="nova.write"
    )
    assert set(names) == {spec.name for spec in TOOL_SPECS}


def test_missing_rbac_permission_hides_exactly_that_tool():
    principal = principal_for(
        permissions=frozenset({PermissionCode.ORDER_READ.value}),
        data_scope=DataScope.MERCHANT,
    )
    names = authorized_tool_names(
        principal=principal, token_scopes=frozenset(all_tool_scopes()), write_scope="nova.write"
    )
    assert set(names) == {"nova.order.get", "nova.order.search"}


def test_missing_token_scope_hides_tools_even_when_rbac_allows_them():
    """OAuth consent is narrower than the role, and the narrower one wins."""
    principal = principal_for(
        permissions=frozenset({PermissionCode.ORDER_READ.value, PermissionCode.PRODUCT_READ.value}),
        data_scope=DataScope.MERCHANT,
    )
    names = authorized_tool_names(
        principal=principal, token_scopes=frozenset({"orders:read"}), write_scope="nova.write"
    )
    assert names == ("nova.order.get", "nova.order.search")
    assert "nova.product.search" not in names


def test_consumer_scope_sees_only_the_self_scoped_tools():
    principal = principal_for(
        user_type=UserType.CONSUMER.value,
        merchant_id=None,
        permissions=frozenset({
            PermissionCode.ORDER_READ.value,
            PermissionCode.FULFILLMENT_READ.value,
        }),
        data_scope=DataScope.SELF,
    )
    names = authorized_tool_names(
        principal=principal, token_scopes=frozenset(all_tool_scopes()), write_scope="nova.write"
    )
    assert names == ("nova.order.get", "nova.order.search", "nova.fulfillment.get")


def test_data_scope_none_authorizes_nothing():
    principal = principal_for(data_scope=DataScope.NONE)
    names = authorized_tool_names(
        principal=principal, token_scopes=frozenset(all_tool_scopes()), write_scope="nova.write"
    )
    assert names == ()


def test_write_tool_needs_the_write_scope_even_with_the_permission():
    principal = principal_for(permissions=frozenset({PermissionCode.PROMOTION_WRITE.value}))
    denial = authorize_tool(
        tool_name="nova.promotion.propose",
        principal=principal,
        token_scopes=frozenset({"nova.read", "promotion:propose"}),
        write_scope="nova.write",
    )
    assert denial is not None
    assert denial.code == "MCP_TOKEN_INSUFFICIENT_SCOPE"


def test_write_tool_denied_without_promotion_write_permission():
    principal = principal_for(permissions=frozenset({PermissionCode.PROMOTION_READ.value}))
    denial = authorize_tool(
        tool_name="nova.promotion.propose",
        principal=principal,
        token_scopes=frozenset({"nova.read", "nova.write", "promotion:propose"}),
        write_scope="nova.write",
    )
    assert denial is not None
    assert denial.code == "INSUFFICIENT_PERMISSION"


def test_unknown_and_forbidden_names_are_not_exposed():
    principal = principal_for(data_scope=DataScope.ALL, permissions=frozenset({p.value for p in PermissionCode}))
    for name in ("execute_sql", *FORBIDDEN_TOOL_NAMES):
        denial = authorize_tool(
            tool_name=name,
            principal=principal,
            token_scopes=frozenset(all_tool_scopes()),
            write_scope="nova.write",
        )
        assert denial is not None, name
        assert denial.code == "MCP_TOOL_NOT_EXPOSED", name


def test_cross_tenant_scope_is_refused_in_both_directions():
    """SELF cannot reach merchant data, and a tenant token cannot become an ALL token.

    The positive control is in the same test on purpose: "SELF is refused analytics"
    proves nothing on its own, because a policy that refuses *everything* would also
    satisfy it. The merchant-scoped principal reaching the same tool is what shows the
    refusal is about the scope and not about the tool.
    """
    consumer_scope = principal_for(merchant_id=None, data_scope=DataScope.SELF)
    denial = authorize_tool(
        tool_name="nova.analytics.sales",
        principal=consumer_scope,
        token_scopes=frozenset(all_tool_scopes()),
        write_scope="nova.write",
    )
    assert denial is not None
    assert denial.code == "DATA_SCOPE_VIOLATION"

    merchant = principal_for(merchant_id=31, data_scope=DataScope.MERCHANT)
    assert (
        authorize_tool(
            tool_name="nova.analytics.sales",
            principal=merchant,
            token_scopes=frozenset(all_tool_scopes()),
            write_scope="nova.write",
        )
        is None
    )
    # A broader DataScope satisfies the same requirement by rank, which is what keeps
    # the console's ALL operator from needing a second copy of every tool.
    platform = principal_for(merchant_id=None, data_scope=DataScope.ALL)
    assert (
        authorize_tool(
            tool_name="nova.analytics.sales",
            principal=platform,
            token_scopes=frozenset(all_tool_scopes()),
            write_scope="nova.write",
        )
        is None
    )


def test_every_spec_declares_scope_permission_risk_and_a_forbidding_input_model():
    for spec in TOOL_SPECS:
        assert spec.scope, spec.name
        assert spec.risk_level in {"READ", "LOW", "MEDIUM", "HIGH", "CRITICAL"}, spec.name
        assert spec.input_model.model_config.get("extra") == "forbid", spec.name
        assert spec.min_data_scope in set(DataScope), spec.name
        assert spec.name.startswith("nova."), spec.name


def test_the_frozen_sixteen_names_are_exactly_these():
    """The surface is a contract: a rename is a breaking change, so it is asserted."""
    assert [spec.name for spec in TOOL_SPECS] == [
        "nova.product.search",
        "nova.product.get",
        "nova.sku.get",
        "nova.order.get",
        "nova.order.search",
        "nova.fulfillment.get",
        "nova.after_sale.get",
        "nova.inventory.get",
        "nova.analytics.sales",
        "nova.analytics.inventory",
        "nova.analytics.product_performance",
        "nova.analytics.refunds",
        "nova.analytics.anomalies",
        "nova.knowledge.search",
        "nova.promotion.preview",
        "nova.promotion.propose",
    ]
    assert len(TOOL_SPECS_BY_NAME) == 16


def test_input_models_reject_identity_fields():
    """Identity is not an argument. ``user_id``/``merchant_id``/``permissions`` are refused."""
    from pydantic import ValidationError

    spec = TOOL_SPECS_BY_NAME["nova.order.get"]
    with pytest.raises(ValidationError):
        spec.input_model.model_validate({"order_no": "NV1", "user_id": 999})
    with pytest.raises(ValidationError):
        spec.input_model.model_validate({"order_no": "NV1", "merchant_id": 999})
    with pytest.raises(ValidationError):
        spec.input_model.model_validate({"order_no": "NV1", "permissions": ["order:read"]})
    with pytest.raises(ValidationError):
        spec.input_model.model_validate({"order_no": "NV1", "is_admin": True})


def test_the_verifier_refuses_a_token_whose_resource_is_another_server():
    """RFC 8707: a token issued for the REST API must not be replayable here.

    The signature is genuine, the issuer is right and the token is unexpired - the
    only wrong thing is the audience. That single difference is the whole test.
    """
    settings = settings_for_tests()
    for wrong in ("http://127.0.0.1:8020/api", "http://127.0.0.1:8020/", "nova-commerce-api"):
        assert _verify(mint_token(settings=settings, audience=wrong), settings) is None, wrong


def test_test_settings_are_wired_explicitly():
    settings = settings_for_tests()
    assert settings.issuer_url == TEST_ISSUER
    assert settings.resource_server_url == TEST_RESOURCE
    assert settings.required_scopes == ["nova.read"]
    assert settings.write_scope == "nova.write"
    assert settings.has_key_material is True
