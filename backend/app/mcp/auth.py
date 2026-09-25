"""Real bearer-token verification for the MCP resource server (spec sections 87, 109).

This module is the entire trust boundary of the MCP surface. Everything it
returns is treated downstream as fact, so the only question that matters here is:
*was this signature verified against a key we chose, for the issuer and audience
we expect, while still inside its validity window?*

## Why PyJWT directly, and not the application's own decoder

``app.modules.identity.security.decode_access_token`` verifies the *first-party*
access token: HS256, the application's own issuer, the API audience. The MCP
resource server must additionally accept tokens minted by an external
authorization server (spec section 87 puts Keycloak in front of this surface),
which means RS*/ES* signatures, a **different** audience (the RFC 8707 resource
indicator, not the API audience), and possibly a JWKS endpoint instead of a
shared secret. Reusing the API decoder would therefore either reject every
legitimate MCP token or require loosening the API's own audience check - and
loosening it is how a token issued for the REST API becomes replayable against
the MCP surface.

So the verification is re-done here with its own key material, its own expected
issuer and its own expected audience. The rules are the same ones the application
already applies, deliberately: pinned algorithm list (``alg: none`` and
algorithm-substitution are dead on arrival), explicit issuer, explicit audience,
expiry with a small leeway.

## Why every failure returns ``None``

``TokenVerifier.verify_token`` returning ``None`` is the SDK's "this token is not
valid" signal, and the bearer middleware turns it into a 401. Raising instead
would escape into the ASGI stack as a 500 - telling an attacker their token was
*parsed* (so it is worth iterating on) and telling an operator nothing, because a
500 is indistinguishable from a bug. It would also break the SDK's contract:
``verify_token`` is typed ``-> AccessToken | None``, so any exception is a
protocol violation, not an error report.

The consequence is that this module never logs a token body, only the reason.
Spec section 132 forbids credentials in logs, and a token *is* a credential.
"""

from __future__ import annotations

from typing import Any, Final

import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken

from app.core.errors import PermissionDeniedError
from app.core.logging import get_logger
from app.mcp.config import MCPSettings
from app.modules.identity.enums import DataScope, UserType
from app.modules.identity.service import Principal

logger = get_logger(__name__)

#: Key that carries the server-resolved principal inside ``AccessToken.claims``.
#: A single constant so the writer and every reader cannot disagree about it.
PRINCIPAL_CLAIM: Final[str] = "nova_principal"

#: Claim names carrying the grant, in the order we accept them. ``scope`` is the
#: RFC 6749/8693 name; ``scp`` is what Azure AD and a few others emit.
_SCOPE_CLAIMS: Final[tuple[str, ...]] = ("scope", "scp")

#: Claim names carrying the audience/resource indicator. ``aud`` is standard JWT;
#: ``resource`` is the RFC 8707 name an authorization server may use instead.
_RESOURCE_CLAIMS: Final[tuple[str, ...]] = ("resource", "aud")

#: PyJWT client cache, keyed by JWKS URL. Module-level because a JWKS fetch per
#: request would put the authorization server on the critical path of every tool
#: call, and because PyJWT's client already does its own key-set caching.
_JWKS_CLIENTS: dict[str, PyJWKClient] = {}


class InvalidTokenClaimsError(ValueError):
    """The signature verified but the payload cannot describe a principal.

    Separate from a signature failure on purpose: this is a *configuration or
    issuer* fault (an authorization server emitting claims we do not understand),
    not an attacker, and the log line an operator needs is different.
    """


def _as_scope_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset(part for part in value.replace(",", " ").split() if part)
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(str(item) for item in value if str(item).strip())
    return frozenset()


def extract_scopes(claims: dict[str, Any]) -> frozenset[str]:
    """Scopes from whichever claim name this issuer uses."""
    scopes: set[str] = set()
    for name in _SCOPE_CLAIMS:
        scopes.update(_as_scope_set(claims.get(name)))
    return frozenset(scopes)


def _token_resource(claims: dict[str, Any]) -> str | None:
    for name in _RESOURCE_CLAIMS:
        raw = claims.get(name)
        if raw is None:
            continue
        if isinstance(raw, str) and raw:
            return raw
        if isinstance(raw, (list, tuple)) and raw:
            return str(raw[0])
    return None


def _decode_data_scope(raw: Any) -> DataScope:
    """Map the claim to a :class:`DataScope`, never defaulting to a broad one.

    An unknown or missing value is refused rather than rounded up. The failure mode
    this prevents is specific and severe: a token missing ``data_scope`` that
    defaulted to ``ALL`` would turn a malformed token into a platform-wide grant.
    """
    if raw is None:
        msg = "token carries no data_scope claim"
        raise InvalidTokenClaimsError(msg)
    try:
        return DataScope(str(raw))
    except ValueError as exc:
        msg = f"token data_scope {raw!r} is not a known scope"
        raise InvalidTokenClaimsError(msg) from exc


def build_principal(claims: dict[str, Any]) -> Principal:
    """Assemble a :class:`Principal` from **verified** claims only.

    Every field is read from the token payload and nowhere else. In particular the
    tool input models have no identity fields at all (``extra="forbid"``), so there
    is no code path in which a caller's arguments could reach this function.

    ``session_id`` is the token's ``sid`` when present. It is kept because the
    principal is used for audit attribution elsewhere in the platform, and an
    MCP-originated action that cannot name the session it came from is an action
    nobody can trace back to a login.
    """
    subject = claims.get("sub")
    if subject is None or str(subject).strip() == "":
        msg = "token carries no subject claim"
        raise InvalidTokenClaimsError(msg)
    try:
        user_id = int(str(subject))
    except ValueError as exc:
        msg = f"token subject {subject!r} is not a numeric user id"
        raise InvalidTokenClaimsError(msg) from exc

    raw_merchant = claims.get("merchant_id")
    merchant_id: int | None
    if raw_merchant is None:
        merchant_id = None
    else:
        try:
            merchant_id = int(raw_merchant)
        except (TypeError, ValueError) as exc:
            msg = "token merchant_id claim is malformed"
            raise InvalidTokenClaimsError(msg) from exc

    raw_type = str(claims.get("user_type", UserType.CONSUMER.value))
    try:
        user_type = UserType(raw_type)
    except ValueError as exc:
        msg = f"token user_type {raw_type!r} is not a known user type"
        raise InvalidTokenClaimsError(msg) from exc

    roles_raw = claims.get("roles") or ()
    permissions_raw = claims.get("permissions") or ()
    if not isinstance(roles_raw, (list, tuple, set, frozenset)):
        msg = "token roles claim is malformed"
        raise InvalidTokenClaimsError(msg)
    if not isinstance(permissions_raw, (list, tuple, set, frozenset)):
        msg = "token permissions claim is malformed"
        raise InvalidTokenClaimsError(msg)

    is_staff = user_type is UserType.STAFF
    return Principal(
        user_id=user_id,
        user_type=user_type.value,
        merchant_id=merchant_id,
        roles=tuple(str(role) for role in roles_raw),
        permissions=frozenset(str(permission) for permission in permissions_raw),
        data_scope=_decode_data_scope(claims.get("data_scope")),
        session_id=str(claims.get("sid", "")),
        # A STAFF token with no merchant is still staff, but every merchant-scoped
        # service will refuse it for lack of an owner rather than treating it as
        # consumer traffic - which is the fail-closed direction.
        is_staff=is_staff,
    )


class NovaTokenVerifier:
    """``TokenVerifier`` implementation backed by PyJWT.

    One instance per server, holding the configured key material. It is
    intentionally the only place in the MCP package that touches a signing key.
    """

    def __init__(self, settings: MCPSettings) -> None:
        self._settings = settings
        self._algorithms: tuple[str, ...] = (settings.algorithm,)
        self._leeway = settings.leeway_seconds
        self._resource_server_url = settings.resource_server_url.rstrip("/")

    # -- key material ------------------------------------------------------
    def _hmac_key(self) -> str | None:
        secret = self._settings.hmac_secret
        return secret.get_secret_value() if secret is not None else None

    def _pem_key(self) -> str | None:
        pem = self._settings.public_key_pem
        return pem.get_secret_value() if pem is not None else None

    def _signing_key(self, token: str) -> Any:
        """Resolve the verification key for ``token``, or raise.

        Order matters: a JWKS URL takes precedence because a deployment configured
        with both is running the OIDC path and the shared secret is a leftover. In
        that case choosing the secret would verify HS256 tokens against a key the
        authorization server does not use, which fails closed - but choosing JWKS
        is the configuration the operator actually asked for.
        """
        jwks_url = self._settings.jwks_url
        if jwks_url:
            client = _JWKS_CLIENTS.get(jwks_url)
            if client is None:
                client = PyJWKClient(jwks_url, cache_keys=True)
                _JWKS_CLIENTS[jwks_url] = client
            return client.get_signing_key_from_jwt(token).key
        pem = self._pem_key()
        if pem:
            return pem
        secret = self._hmac_key()
        if secret:
            return secret
        msg = "no MCP signing key is configured (MCP_HMAC_SECRET, MCP_PUBLIC_KEY_PEM or MCP_JWKS_URL)"
        raise jwt.InvalidKeyError(msg)

    # -- TokenVerifier protocol -------------------------------------------
    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify signature, issuer, audience, expiry and scopes. ``None`` on failure."""
        if not token or not token.strip():
            logger.warning("mcp bearer token rejected", reason="empty token")
            return None

        if not self._settings.has_key_material:
            # Refusing here rather than raising is what makes an unconfigured
            # deployment fail closed: no key means every token is invalid.
            logger.error("mcp bearer token rejected", reason="no verification key configured")
            return None

        try:
            signing_key = self._signing_key(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key,
                algorithms=list(self._algorithms),
                # Both are checked by PyJWT against our configured values, so a
                # token from another environment cannot be replayed here even
                # though the signature is genuine for *its* issuer.
                issuer=self._settings.issuer_url,
                audience=self._resource_server_url,
                leeway=self._leeway,
                options={
                    "require": ["exp", "iss", "aud", "sub"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.ExpiredSignatureError:
            logger.warning("mcp bearer token rejected", reason="expired")
            return None
        except jwt.ImmatureSignatureError:
            logger.warning("mcp bearer token rejected", reason="not yet valid")
            return None
        except jwt.InvalidAudienceError:
            logger.warning("mcp bearer token rejected", reason="wrong audience")
            return None
        except jwt.InvalidIssuerError:
            logger.warning("mcp bearer token rejected", reason="wrong issuer")
            return None
        except jwt.InvalidAlgorithmError:
            logger.warning("mcp bearer token rejected", reason="algorithm not permitted")
            return None
        except jwt.PyJWTError as exc:
            logger.warning("mcp bearer token rejected", reason="signature or claim verification failed", error=type(exc).__name__)
            return None

        resource = _token_resource(claims)
        if resource is None:
            logger.warning("mcp bearer token rejected", reason="no audience/resource claim")
            return None
        # Belt and braces next to AuthSettings.validate_token_resource: that flag
        # makes the *middleware* compare AccessToken.resource, and this makes the
        # verifier refuse a token that carries an audience list where the correct
        # entry is missing. With a list audience PyJWT accepts a match on any
        # entry, so the check below is what requires the match to be *ours*.
        if resource.rstrip("/") != self._resource_server_url:
            logger.warning("mcp bearer token rejected", reason="resource indicator is not this server")
            return None

        scopes = extract_scopes(claims)
        missing = sorted(set(self._settings.required_scopes) - scopes)
        if missing:
            logger.warning("mcp bearer token rejected", reason="missing base scope", missing=missing)
            return None

        try:
            principal = build_principal(claims)
        except InvalidTokenClaimsError as exc:
            logger.warning("mcp bearer token rejected", reason="claims unusable", error=str(exc))
            return None

        expires_at = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=principal.session_id or str(principal.user_id),
            scopes=sorted(scopes),
            expires_at=int(expires_at) if expires_at is not None else None,
            resource=resource,
            subject=str(claims.get("sub")),
            claims={
                # The four authorization dimensions, resolved once at the trust
                # boundary so no tool has to re-derive them from raw claims.
                PRINCIPAL_CLAIM: principal,
                "iss": claims.get("iss"),
                "roles": list(principal.roles),
                "permissions": sorted(principal.permissions),
                "data_scope": principal.data_scope.value,
                "merchant_id": principal.merchant_id,
                "token_expires_at": int(expires_at) if expires_at is not None else None,
            },
        )

    # -- helpers used by tests and the emitter ----------------------------
    def decode_for_test(self, token: str) -> dict[str, Any]:
        """Synchronous verification returning claims, for assertions in tests.

        Deliberately a thin wrapper over the same PyJWT call: a test that verified
        tokens with its own decoder could pass while the server's verifier was
        wrong, which is exactly the class of "green suite, broken component" the
        frozen rules forbid.
        """
        return jwt.decode(
            token,
            self._signing_key(token),
            algorithms=list(self._algorithms),
            issuer=self._settings.issuer_url,
            audience=self._resource_server_url,
            leeway=self._leeway,
            options={"require": ["exp", "iss", "aud", "sub"], "verify_signature": True},
        )


def principal_from_verified_token(verified: AccessToken) -> Principal:
    """Pull the resolved principal out of a verified token.

    Raises rather than returning ``None``: reaching this function means the token
    already passed verification, and if the principal is missing from its claims
    then this code and the verifier disagree - a programming error that must be
    loud, not a request that should be quietly refused.
    """
    claims = verified.claims or {}
    principal = claims.get(PRINCIPAL_CLAIM)
    if not isinstance(principal, Principal):
        msg = "verified token does not carry a resolved principal"
        raise PermissionDeniedError(msg)
    return principal


def assert_scoped_to_merchant(principal: Principal, *, merchant_id: int | None) -> None:
    """Raise unless ``principal`` may act inside ``merchant_id``.

    Used by the tool layer where a *second* scoping decision exists beyond the
    policy intersection - for instance once a product has been resolved and its
    owning merchant is known from the row rather than from the token. Reusing the
    platform's :class:`DataScope` rule keeps one definition of "may read this
    merchant" instead of a second, subtly different one in the MCP layer.
    """
    principal.assert_can_access_merchant(merchant_id=merchant_id)


__all__ = [
    "PRINCIPAL_CLAIM",
    "InvalidTokenClaimsError",
    "NovaTokenVerifier",
    "assert_scoped_to_merchant",
    "build_principal",
    "extract_scopes",
    "principal_from_verified_token",
]
