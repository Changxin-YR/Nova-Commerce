"""Password hashing, token minting and refresh-token hashing.

Spec references:
    §22  only the refresh-token **hash** may be persisted; never the plaintext.
    §23  short-lived access token; refresh rotation; HttpOnly/Secure/SameSite.
    §109 JWT tampering, expired tokens, refresh reuse, brute force.
    §112 INV-016: after rotation the previous refresh token must be dead.

Threat model this module is written against:

* **Stolen database.** An attacker with a read-only dump must not be able to
  either log in (password hashes) or mint sessions (refresh-token hashes).
* **Stolen access token.** Must expire quickly and must be distinguishable from
  a refresh token, so a refresh token can never be replayed as a bearer token.
* **Token confusion.** ``typ`` is asserted in both directions.
* **Offline cracking of a leaked hash.** Refresh tokens are high-entropy random
  values, so a fast hash is sufficient *and* appropriate; passwords are
  low-entropy, so they get Argon2id. Using Argon2 for refresh tokens would burn
  CPU on the hot refresh path for no security gain.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

from app.core.config import Settings
from app.core.errors import (
    AccountDisabledError,
    AccountLockedError,
    InvalidCredentialsError,
    TokenExpiredError,
    TokenInvalidError,
)
from app.core.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
#: Argon2id parameters tuned to be ~50-100ms on commodity hardware: fast enough
#: that login is not a bottleneck, slow enough that offline cracking is
#: expensive. Memory cost is the parameter that matters most against GPUs.
_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,  # 64 MiB
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

MIN_PASSWORD_LENGTH: Final[int] = 10
MAX_PASSWORD_LENGTH: Final[int] = 128

#: A short deny-list of the passwords that actually show up in credential
#: stuffing. A full breach corpus belongs in a service, not in the source tree.
_COMMON_PASSWORDS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "password1",
        "password123",
        "12345678",
        "123456789",
        "qwertyuiop",
        "letmein123",
        "admin12345",
        "welcome123",
        "iloveyou123",
        "nova1234",
        "nova",
    }
)


def validate_password_strength(password: str) -> None:
    """Reject passwords that are trivially guessable.

    Length is the dominant factor, so the rule is length-first rather than a
    character-class checklist - composition rules push users toward ``Passw0rd!``,
    which is both weaker and harder to remember.
    """
    from app.core.errors import ValidationError

    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        # Bounded because Argon2 cost grows with input length; an unbounded
        # password is a cheap denial-of-service vector.
        raise ValidationError(f"password must be at most {MAX_PASSWORD_LENGTH} characters")
    if password.lower() in _COMMON_PASSWORDS:
        raise ValidationError("password is too common")
    if len(set(password)) < 4:
        raise ValidationError("password must contain at least 4 distinct characters")


def hash_password(password: str) -> str:
    """Hash with Argon2id. The returned string embeds salt and parameters."""
    validate_password_strength(password)
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Constant-time-ish verification.

    Returns ``False`` rather than raising, so callers cannot accidentally leak
    *which* part failed through an exception type. A malformed stored hash is
    treated as a failed login and logged, because it means data corruption.
    """
    if not password or not password_hash:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    except (VerificationError, InvalidHashError):
        logger.error("stored password hash could not be verified")
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True when the stored hash predates the current cost parameters."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except (VerificationError, InvalidHashError):
        return True


def dummy_verify() -> None:
    """Burn comparable time on a nonexistent account.

    Without this, ``login`` returns noticeably faster for an unknown username
    than for a wrong password, which is a user-enumeration oracle (§109).
    """
    _hasher.hash("dummy-timing-equaliser-value")


# ---------------------------------------------------------------------------
# Refresh tokens
# ---------------------------------------------------------------------------
def generate_refresh_token() -> str:
    """A 256-bit URL-safe random token.

    Opaque rather than a JWT: the server already stores session state, so a
    self-describing token would add nothing and would tempt callers into
    trusting it without a database lookup - which is exactly what rotation needs
    to prevent.
    """
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str, *, pepper: str = "") -> str:
    """SHA-256 hash of the refresh token (spec §22).

    The plaintext is never persisted. ``pepper`` (the app secret) is mixed in so
    that a leaked *database* alone is not sufficient to verify a guessed token -
    an attacker would also need the application secret.
    """
    digest = hashlib.sha256()
    digest.update(pepper.encode("utf-8"))
    digest.update(b"\x00")  # domain separator: prevents concatenation ambiguity
    digest.update(token.encode("utf-8"))
    return digest.hexdigest()


def refresh_token_matches(token: str, expected_hash: str, *, pepper: str = "") -> bool:
    """Constant-time comparison, so a timing side channel cannot confirm a guess."""
    return hmac.compare_digest(hash_refresh_token(token, pepper=pepper), expected_hash)


# ---------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------
AccessTokenType = Literal["access"]
_REQUIRED_CLAIMS = ("exp", "iat", "nbf", "iss", "aud", "sub", "jti", "typ")


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    """Verified access-token payload."""

    subject: int
    jti: str
    issued_at: datetime
    expires_at: datetime
    session_id: str
    roles: tuple[str, ...]
    permissions: frozenset[str]
    data_scope: str
    merchant_id: int | None
    user_type: str

    @property
    def claims(self) -> dict[str, Any]:
        return {
            "sub": str(self.subject),
            "jti": self.jti,
            "session_id": self.session_id,
            "roles": list(self.roles),
            "permissions": sorted(self.permissions),
            "data_scope": self.data_scope,
            "merchant_id": self.merchant_id,
            "user_type": self.user_type,
        }


def create_access_token(
    *,
    settings: Settings,
    user_id: int,
    session_id: str,
    roles: tuple[str, ...],
    permissions: frozenset[str],
    data_scope: str,
    merchant_id: int | None,
    user_type: str,
    now: datetime | None = None,
) -> tuple[str, AccessTokenClaims]:
    """Mint a short-lived access token.

    Permissions are embedded so the common authorization path needs no database
    round-trip. The trade-off is staleness: a permission revoked mid-token
    remains usable until expiry. That is acceptable *because* the TTL is short
    (§23); anything longer would make immediate revocation impossible and is why
    refresh rotation exists.
    """
    issued = now or datetime.now(UTC)
    expires = issued + timedelta(seconds=settings.ACCESS_TOKEN_TTL_SECONDS)
    jti = secrets.token_urlsafe(16)

    payload: dict[str, Any] = {
        "sub": str(user_id),
        "jti": jti,
        "sid": session_id,
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "iat": int(issued.timestamp()),
        "nbf": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        # `typ` is asserted on decode, so a refresh token can never be replayed
        # as a bearer token even if it happened to share a signing key.
        "typ": "access",
        "roles": list(roles),
        "permissions": sorted(permissions),
        "data_scope": data_scope,
        "merchant_id": merchant_id,
        "user_type": user_type,
    }

    encoded = jwt.encode(
        payload,
        settings.JWT_SECRET_KEY.get_secret_value(),
        algorithm=settings.JWT_ALGORITHM,
    )
    return encoded, AccessTokenClaims(
        subject=user_id,
        jti=jti,
        issued_at=issued,
        expires_at=expires,
        session_id=session_id,
        roles=roles,
        permissions=permissions,
        data_scope=data_scope,
        merchant_id=merchant_id,
        user_type=user_type,
    )


def decode_access_token(token: str, *, settings: Settings) -> AccessTokenClaims:
    """Verify and decode an access token.

    Every failure maps to a specific, non-leaking error: the client learns
    "expired" (so it knows to refresh) but never learns *why* a forged token
    failed, which would otherwise help an attacker iterate.
    """
    if not token:
        raise TokenInvalidError("no token supplied")

    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET_KEY.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],  # pin the algorithm: defeats alg=none
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
            options={
                "require": list(_REQUIRED_CLAIMS),
                "verify_signature": True,
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
            },
            leeway=5,  # tolerate modest clock skew between replicas
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("access token has expired") from exc
    except jwt.ImmatureSignatureError as exc:
        raise TokenInvalidError("access token is not yet valid") from exc
    except jwt.InvalidAudienceError as exc:
        raise TokenInvalidError("access token audience mismatch") from exc
    except jwt.InvalidIssuerError as exc:
        raise TokenInvalidError("access token issuer mismatch") from exc
    except jwt.InvalidAlgorithmError as exc:
        # A forged token asking for `none` lands here.
        raise TokenInvalidError("unsupported token algorithm") from exc
    except jwt.PyJWTError as exc:
        raise TokenInvalidError("access token could not be verified") from exc

    if payload.get("typ") != "access":
        raise TokenInvalidError("token is not an access token")

    try:
        subject = int(payload["sub"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenInvalidError("access token subject is malformed") from exc

    try:
        issued = datetime.fromtimestamp(int(payload["iat"]), tz=UTC)
        expires = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise TokenInvalidError("access token timestamps are malformed") from exc

    roles = tuple(str(role) for role in (payload.get("roles") or ()))
    permissions = frozenset(str(permission) for permission in (payload.get("permissions") or ()))

    raw_scope = payload.get("data_scope")
    from app.modules.identity.enums import DataScope

    try:
        data_scope = DataScope(str(raw_scope)).value
    except ValueError:
        # An unparseable scope must not fall back to a permissive default.
        raise TokenInvalidError("access token data scope is malformed") from None

    merchant_id = payload.get("merchant_id")
    return AccessTokenClaims(
        subject=subject,
        jti=str(payload["jti"]),
        issued_at=issued,
        expires_at=expires,
        session_id=str(payload.get("sid", "")),
        roles=roles,
        permissions=permissions,
        data_scope=data_scope,
        merchant_id=int(merchant_id) if merchant_id is not None else None,
        user_type=str(payload.get("user_type", "CONSUMER")),
    )


def guard_login_allowed(*, status: str, failed_attempts: int, settings: Settings) -> None:
    """Apply account-state and brute-force rules before verifying a password.

    Called *before* the password check on purpose: if the account is locked we
    should not spend 64 MiB of Argon2 memory on it, and the lockout must not be
    bypassable by simply continuing to guess.
    """
    if status == "DISABLED":
        raise AccountDisabledError("account is disabled")
    if status == "INACTIVE":
        raise AccountDisabledError("account is not active")
    if failed_attempts >= settings.LOGIN_MAX_FAILURES or status == "LOCKED":
        raise AccountLockedError(
            f"too many failed attempts; try again in {settings.LOGIN_LOCKOUT_SECONDS} seconds"
        )


def constant_time_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def verify_credentials_or_fail(*, password: str, password_hash: str) -> None:
    """Verify or raise a single, uniform failure.

    Raising the *same* error for "unknown user" and "wrong password" is what
    keeps ``login`` from becoming a user-enumeration oracle.
    """
    if not verify_password(password, password_hash):
        raise InvalidCredentialsError("username or password is incorrect")


__all__ = [
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "AccessTokenClaims",
    "constant_time_compare",
    "create_access_token",
    "decode_access_token",
    "dummy_verify",
    "generate_refresh_token",
    "guard_login_allowed",
    "hash_password",
    "hash_refresh_token",
    "password_needs_rehash",
    "refresh_token_matches",
    "validate_password_strength",
    "verify_credentials_or_fail",
    "verify_password",
]
