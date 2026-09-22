"""Identity application services.

Spec references:
    §22  only the refresh-token hash is persisted.
    §23  rotation on every refresh; short-lived access tokens.
    §24  consumer ``merchant_id`` may be null; staff always carry one.
    §109 JWT tampering, expired tokens, refresh reuse, brute force, IDOR.
    §112 INV-016: after rotation the previous refresh token is dead.
    §116 the five auth behaviours that must be tested.

## The rotation design, and why it looks the way it does

There are two ways to implement "rotate on refresh", and only one of them can
satisfy §116's *"reuse old refresh"* test.

**(a) In-place update.** Overwrite ``refresh_token_hash`` on the existing row.
Cheap, but it *destroys the evidence*: once rotated, the old token's hash no
longer exists anywhere, so presenting it looks identical to presenting garbage.
Reuse is therefore undetectable, and an attacker who steals a token and races the
legitimate client simply wins silently.

**(b) Chain rotation.** Every rotation creates a **new** ``auth_sessions`` row and
revokes the old one with ``rotated_from`` pointing back at it. The old hash
remains in the table as a tripwire, so presenting a rotated token is
*recognisably* different from presenting a random string, and the server can
respond correctly by revoking the whole family.

This module implements **(b)**. Spec §22 mandates ``rotated_from``, and that
column is only meaningful under (b) - so the frozen design already implied this.
The cost is more rows; the benefit is that token theft is *detected* rather than
merely limited.

Note the deliberately unhelpful error returned to the client in both cases: the
caller always sees ``REFRESH_TOKEN_REUSED``/``TOKEN_INVALID`` without learning
whether the token ever existed, which keeps the endpoint from becoming an oracle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import (
    AccountDisabledError,
    AuthenticationError,
    InvalidCredentialsError,
    RefreshTokenReuseError,
    TokenInvalidError,
    ValidationError,
)
from app.core.logging import get_logger
from app.modules.identity.enums import (
    AddressTag,
    DataScope,
    SessionRevokeReason,
    UserStatus,
    UserType,
)
from app.modules.identity.models import AuthSession, User, UserAddress
from app.modules.identity.repository import (
    AddressRepository,
    AuthSessionRepository,
    RoleRepository,
    UserRepository,
    resolve_data_scope,
)
from app.modules.identity.security import (
    AccessTokenClaims,
    create_access_token,
    generate_refresh_token,
    guard_login_allowed,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.shared.db.base import utc_now

logger = get_logger(__name__)

#: Grace window during which a just-rotated token is treated as a benign
#: double-submit rather than an attack.
#:
#: Why this exists: a browser can legitimately fire two requests with the same
#: cookie when a tab is restored, and mobile networks retry. Treating every such
#: case as theft would revoke honest users constantly, which trains operators to
#: ignore the alert. Within the window we return a *fresh* pair without revoking;
#: outside it we treat it as theft. Either way the old token is never accepted
#: twice for the same business effect.
REUSE_GRACE_SECONDS = 20


@dataclass(slots=True)
class IssuedSession:
    """A freshly minted session and the secrets that go with it.

    The refresh token plaintext appears here exactly once, on its way into an
    HttpOnly cookie, and is never stored or logged.
    """

    session: AuthSession
    access_token: str
    access_claims: AccessTokenClaims
    refresh_token: str
    refresh_expires_at: datetime


@dataclass(slots=True)
class Principal:
    """A resolved, authenticated caller.

    This is the object the rest of the application authorises against. It is
    assembled server-side from the database, never from client input - which is
    what makes INV-010 (agent permission ⊆ user permission) checkable at all.
    """

    user_id: int
    user_type: str
    merchant_id: int | None
    roles: tuple[str, ...]
    permissions: frozenset[str]
    data_scope: DataScope
    session_id: str
    is_staff: bool

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def has_any_permission(self, *permissions: str) -> bool:
        return bool(self.permissions.intersection(permissions))

    def require_permission(self, permission: str) -> None:
        from app.core.errors import PermissionDeniedError

        if not self.has_permission(permission):
            raise PermissionDeniedError(f"missing required permission: {permission}")

    def is_owner_of(self, *, owner_user_id: int) -> bool:
        return self.user_id == owner_user_id

    def assert_can_access_user(self, *, owner_user_id: int, owner_merchant_id: int | None) -> None:
        """Row-level authorization for user-owned resources."""
        from app.core.errors import DataScopeViolationError

        if self.data_scope is DataScope.ALL:
            return
        if self.data_scope is DataScope.SELF:
            if self.user_id != owner_user_id:
                raise DataScopeViolationError("resource belongs to another user")
            return
        if self.data_scope is DataScope.MERCHANT:
            if (
                self.merchant_id is None
                or owner_merchant_id is None
                or self.merchant_id != owner_merchant_id
            ):
                raise DataScopeViolationError("resource belongs to another merchant")
            return
        raise DataScopeViolationError("no data scope grants access to this resource")

    def assert_can_access_merchant(self, *, merchant_id: int | None) -> None:
        from app.core.errors import DataScopeViolationError

        if self.data_scope is DataScope.ALL:
            return
        if self.data_scope is DataScope.MERCHANT:
            if self.merchant_id is None or merchant_id is None or self.merchant_id != merchant_id:
                raise DataScopeViolationError("resource belongs to another merchant")
            return
        if self.data_scope is DataScope.SELF:
            # A consumer may read their own merchant's public data.
            return
        raise DataScopeViolationError("no data scope grants access to this resource")


class AuthService:
    """Login, refresh rotation, logout and session inspection."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._users = UserRepository(session)
        self._sessions = AuthSessionRepository(session)
        self._roles = RoleRepository(session)

    # -- helpers ----------------------------------------------------------
    @property
    def _pepper(self) -> str:
        """Mix the application secret into refresh-token hashing.

        Without it, a leaked *database* is enough to verify a guessed refresh
        token offline. With it, the attacker also needs the application secret -
        so the two leaks must happen together to be useful.
        """
        return self._settings.JWT_SECRET_KEY.get_secret_value()

    def _build_principal(self, user: User, *, session_id: str) -> Principal:
        role_codes = user.role_codes
        role_scopes = {
            link.role.code: link.role.data_scope
            for link in user.user_roles
            if link.role is not None
        }
        scope = resolve_data_scope(
            roles=role_codes,
            role_scopes=role_scopes,
            # A consumer with no staff role still needs to see their own orders,
            # so the floor for an authenticated *consumer* is SELF. Staff with no
            # role deliberately get NONE: a misconfigured staff account must fail
            # closed, and SELF would additionally be meaningless for a principal
            # whose job is to look at other people's orders.
            override=user.data_scope_override
            or (DataScope.SELF.value if (not role_codes and not user.is_staff) else None),
        )
        return Principal(
            user_id=user.id,
            user_type=user.user_type,
            merchant_id=user.merchant_id,
            roles=role_codes,
            permissions=user.permission_codes,
            data_scope=scope,
            session_id=session_id,
            is_staff=user.user_type == UserType.STAFF.value,
        )

    def _new_session(
        self,
        *,
        user: User,
        refresh_token: str,
        jti: str,
        now: datetime,
        client_ip: str | None,
        user_agent: str | None,
        login_source: str,
        rotated_from: int | None = None,
    ) -> AuthSession:
        session_id = f"{user.id}-{jti}"
        return AuthSession(
            user_id=user.id,
            session_id=session_id,
            refresh_token_hash=hash_refresh_token(refresh_token, pepper=self._pepper),
            jti=jti,
            issued_at=now,
            expires_at=now + timedelta(seconds=self._settings.REFRESH_TOKEN_TTL_SECONDS),
            rotated_from=rotated_from,
            client_ip=client_ip,
            user_agent=(user_agent or "")[:512] or None,
            login_source=login_source,
            last_used_at=now,
        )

    def _issue(
        self,
        *,
        user: User,
        principal: Principal,
        now: datetime,
    ) -> tuple[AuthSession, str, AccessTokenClaims]:
        refresh_token = generate_refresh_token()
        jti = generate_refresh_token()[:32]
        auth_session = self._new_session(
            user=user,
            refresh_token=refresh_token,
            jti=jti,
            now=now,
            client_ip=None,
            user_agent=None,
            login_source="API",
        )
        self._sessions.add(auth_session)
        # The encoded token is not returned from _issue - callers receive it
        # through IssuedSession - so only the claims are needed here.
        _access_token, claims = create_access_token(
            settings=self._settings,
            user_id=user.id,
            session_id=auth_session.session_id,
            roles=principal.roles,
            permissions=principal.permissions,
            data_scope=principal.data_scope.value,
            merchant_id=principal.merchant_id,
            user_type=principal.user_type,
            now=now,
        )
        return auth_session, refresh_token, claims

    # -- login ------------------------------------------------------------
    def login(
        self,
        *,
        identifier: str,
        password: str,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        """Authenticate and open a new session.

        Ordering is deliberate: state and lockout checks happen *before* the
        Argon2 verification, so a locked account costs no memory and brute-force
        pressure cannot be converted into a resource-exhaustion attack.
        """
        now = utc_now()
        cleaned = (identifier or "").strip()
        if not cleaned or not password:
            raise InvalidCredentialsError("username or password is incorrect")

        user = self._users.find_by_identifier(cleaned)

        if user is None:
            # Burn comparable CPU so response time does not reveal whether the
            # account exists (§109 user enumeration).
            from app.modules.identity.security import dummy_verify

            dummy_verify()
            raise InvalidCredentialsError("username or password is incorrect")

        guard_login_allowed(
            status=user.status,
            failed_attempts=user.failed_login_attempts,
            settings=self._settings,
        )
        if user.locked_until is not None and user.locked_until > now:
            from app.core.errors import AccountLockedError

            raise AccountLockedError("account is temporarily locked")

        if not verify_password(password, user.password_hash):
            self._users.register_failed_login(user)
            logger.warning("login failed", user_id=user.id, client_ip=client_ip)
            raise InvalidCredentialsError("username or password is incorrect")

        if user.deleted_at is not None:
            raise AccountDisabledError("account is disabled")

        # Transparent rehash: an existing user keeps working while the stored
        # hash is upgraded to current parameters.
        from app.modules.identity.security import password_needs_rehash

        if password_needs_rehash(user.password_hash):
            self._users.update_password(user, hash_password(password))

        self._users.register_successful_login(user, when=now)
        principal = self._build_principal(user, session_id="pending")

        refresh_token = generate_refresh_token()
        jti = generate_refresh_token()[:32]
        auth_session = self._new_session(
            user=user,
            refresh_token=refresh_token,
            jti=jti,
            now=now,
            client_ip=client_ip,
            user_agent=user_agent,
            login_source="API",
        )
        self._sessions.add(auth_session)

        access_token, claims = create_access_token(
            settings=self._settings,
            user_id=user.id,
            session_id=auth_session.session_id,
            roles=principal.roles,
            permissions=principal.permissions,
            data_scope=principal.data_scope.value,
            merchant_id=principal.merchant_id,
            user_type=principal.user_type,
            now=now,
        )
        logger.info("login succeeded", user_id=user.id, session_id=auth_session.session_id)

        return IssuedSession(
            session=auth_session,
            access_token=access_token,
            access_claims=claims,
            refresh_token=refresh_token,
            refresh_expires_at=auth_session.expires_at,
        )

    # -- refresh (the rotation path) --------------------------------------
    def rotate(
        self,
        *,
        presented_refresh_token: str,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        """Exchange a refresh token for a new pair, enforcing single use.

        Outcomes, in the order they are decided:

        1. Hash matches no row            -> invalid (never existed) -> 401.
        2. Row is revoked                 -> **reuse detected** -> revoke the
                                             whole family -> 401.
        3. Row is expired                 -> expired -> 401.
        4. User is disabled               -> 403, session revoked.
        5. Otherwise                      -> rotate: new row, old row revoked
                                             with reason ROTATED.
        """
        if not presented_refresh_token:
            raise TokenInvalidError("refresh token is missing")

        now = utc_now()
        presented_hash = hash_refresh_token(presented_refresh_token, pepper=self._pepper)
        auth_session = self._sessions.get_by_refresh_hash(presented_hash)

        # -- case 1: unknown token ---------------------------------------
        if auth_session is None:
            logger.warning("refresh presented an unknown token", client_ip=client_ip)
            raise TokenInvalidError("refresh token is invalid")

        # -- case 2: revoked => reuse ------------------------------------
        if auth_session.revoked_at is not None:
            return self._handle_reuse(auth_session, now=now, client_ip=client_ip)

        # -- case 3: expired ---------------------------------------------
        if auth_session.expires_at <= now:
            self._sessions.revoke(auth_session, reason=SessionRevokeReason.EXPIRED.value, when=now)
            raise TokenInvalidError("refresh token has expired")

        # -- case 4: user state ------------------------------------------
        user = self._users.get_with_authorization(auth_session.user_id)
        if user is None or user.deleted_at is not None:
            self._sessions.revoke(auth_session, reason=SessionRevokeReason.ADMIN_REVOKED.value, when=now)
            raise AccountDisabledError("account is disabled")
        if user.status in {UserStatus.DISABLED.value, UserStatus.INACTIVE.value}:
            self._sessions.revoke(
                auth_session, reason=SessionRevokeReason.ACCOUNT_LOCKED.value, when=now
            )
            raise AccountDisabledError("account is not active")
        if user.status == UserStatus.LOCKED.value and (
            user.locked_until is None or user.locked_until > now
        ):
            self._sessions.revoke(
                auth_session, reason=SessionRevokeReason.ACCOUNT_LOCKED.value, when=now
            )
            raise AuthenticationError("account is temporarily locked")

        # -- case 5: rotate ----------------------------------------------
        self._sessions.revoke(
            auth_session, reason=SessionRevokeReason.ROTATED.value, when=now
        )
        # Record *before* minting the successor so the direction of the chain is
        # unambiguous: successor.rotated_from -> predecessor.
        self._session.flush()

        principal = self._build_principal(user, session_id=auth_session.session_id)
        refresh_token = generate_refresh_token()
        jti = generate_refresh_token()[:32]
        successor = self._new_session(
            user=user,
            refresh_token=refresh_token,
            jti=jti,
            now=now,
            client_ip=client_ip,
            user_agent=user_agent or auth_session.user_agent,
            login_source="REFRESH",
            rotated_from=auth_session.id,
        )
        self._sessions.add(successor)

        access_token, claims = create_access_token(
            settings=self._settings,
            user_id=user.id,
            session_id=successor.session_id,
            roles=principal.roles,
            permissions=principal.permissions,
            data_scope=principal.data_scope.value,
            merchant_id=principal.merchant_id,
            user_type=principal.user_type,
            now=now,
        )
        logger.info(
            "refresh rotated",
            user_id=user.id,
            previous_session_id=auth_session.session_id,
            new_session_id=successor.session_id,
        )
        return IssuedSession(
            session=successor,
            access_token=access_token,
            access_claims=claims,
            refresh_token=refresh_token,
            refresh_expires_at=successor.expires_at,
        )

    def _handle_reuse(
        self,
        auth_session: AuthSession,
        *,
        now: datetime,
        client_ip: str | None,
    ) -> IssuedSession:
        """Respond to a revoked token being presented again.

        Inside the grace window this is treated as a benign double-submit: the
        user agent retried or two tabs raced. We hand back a usable pair so the
        session does not break, but we do **not** revive the presented token.
        Outside it, this is theft: the whole family dies.
        """
        # Was it revoked by rotation, or for another reason? Only rotation is
        # ambiguous enough to deserve a grace window.
        #
        # Checking the presented row alone is NOT sufficient, and getting this
        # wrong is a real security bug (found by this module's own test suite):
        # a token rotated 5 seconds before the user pressed "log out" is itself
        # revoked-for-rotation, so a row-local check would happily tolerate it
        # and hand the caller a brand-new session *after* an explicit logout.
        # The family must therefore be inspected as a whole: the grace window
        # applies only while every revoked member of the family was revoked by
        # rotation. Any other reason - logout, admin revocation, password change,
        # or a previously detected reuse - is an explicit statement of intent
        # that nothing in this family may be resumed.
        rotated = auth_session.revoked_reason == SessionRevokeReason.ROTATED.value
        if rotated:
            rotated = self._family_is_rotation_only(auth_session)
        if rotated and auth_session.revoked_at is not None:
            age = (now - auth_session.revoked_at).total_seconds()
            if age <= REUSE_GRACE_SECONDS:
                logger.info(
                    "refresh double-submit tolerated",
                    session_id=auth_session.session_id,
                    age_seconds=round(age, 2),
                )
                return self._reissue_from_chain(auth_session, now=now, client_ip=client_ip)

        revoked_count = self._sessions.revoke_family(
            auth_session, reason=SessionRevokeReason.REUSE_DETECTED.value
        )
        logger.error(
            "refresh token reuse detected; session family revoked",
            session_id=auth_session.session_id,
            revoked_sessions=revoked_count,
            client_ip=client_ip,
        )
        raise RefreshTokenReuseError(
            "refresh token reuse detected; all sessions in this family have been revoked",
            context={"revoked_sessions": revoked_count},
        )

    def _family_is_rotation_only(self, auth_session: AuthSession) -> bool:
        """True when every revoked member of the family was revoked by rotation.

        This is what stops the double-submit grace window from becoming a
        post-logout resurrection path.
        """
        family_ids = self._sessions.family_session_ids(auth_session)
        if not family_ids:
            return False
        revoked_reasons = (
            self._session.query(AuthSession.revoked_reason)
            .filter(AuthSession.id.in_(family_ids), AuthSession.revoked_at.is_not(None))
            .all()
        )
        return all(
            reason == SessionRevokeReason.ROTATED.value
            for (reason,) in revoked_reasons
        )

    def _reissue_from_chain(
        self,
        auth_session: AuthSession,
        *,
        now: datetime,
        client_ip: str | None,
    ) -> IssuedSession:
        """Mint a successor of the *current tip* of the family, not of the
        presented (already-rotated) row.

        Chaining off the presented row would create a second branch and a
        fork in the family, which makes later reuse analysis ambiguous.
        """
        tip = self._family_tip(auth_session)
        if tip.id != auth_session.id:
            self._sessions.touch(tip, when=now)

        user = self._users.get_with_authorization(tip.user_id)
        if user is None or user.deleted_at is not None:
            raise AccountDisabledError("account is disabled")

        principal = self._build_principal(user, session_id=tip.session_id)
        refresh_token = generate_refresh_token()
        jti = generate_refresh_token()[:32]
        successor = self._new_session(
            user=user,
            refresh_token=refresh_token,
            jti=jti,
            now=now,
            client_ip=client_ip,
            user_agent=tip.user_agent,
            login_source="REFRESH",
            rotated_from=tip.id,
        )
        self._sessions.add(successor)
        access_token, claims = create_access_token(
            settings=self._settings,
            user_id=user.id,
            session_id=successor.session_id,
            roles=principal.roles,
            permissions=principal.permissions,
            data_scope=principal.data_scope.value,
            merchant_id=principal.merchant_id,
            user_type=principal.user_type,
            now=now,
        )
        return IssuedSession(
            session=successor,
            access_token=access_token,
            access_claims=claims,
            refresh_token=refresh_token,
            refresh_expires_at=successor.expires_at,
        )

    def _family_tip(self, auth_session: AuthSession) -> AuthSession:
        """Follow the chain forward to the newest live session."""
        current = auth_session
        for _ in range(50):
            children = (
                self._session.query(AuthSession)
                .filter(AuthSession.rotated_from == current.id)
                .order_by(AuthSession.id.desc())
                .all()
            )
            live = [child for child in children if child.revoked_at is None]
            if not live:
                break
            current = live[0]
        return current

    # -- logout -----------------------------------------------------------
    def logout(self, *, refresh_token: str) -> int:
        """Revoke the session behind a refresh token. Idempotent."""
        if not refresh_token:
            return 0
        presented_hash = hash_refresh_token(refresh_token, pepper=self._pepper)
        auth_session = self._sessions.get_by_refresh_hash(presented_hash)
        if auth_session is None:
            # Already gone. Returning success avoids leaking whether the token
            # was real, and makes retries safe.
            return 0
        was_live = auth_session.revoked_at is None
        self._sessions.revoke(auth_session, reason=SessionRevokeReason.LOGOUT.value)
        # A logout should end the whole rotation family, not just the newest
        # link - otherwise a token stolen before logout keeps working.
        # `revoke_family` only counts rows it transitions, so the presented row
        # (revoked one line above) is added back in; otherwise a single-session
        # logout would report 0 revoked sessions, which reads as a no-op.
        family_revoked = self._sessions.revoke_family(
            auth_session, reason=SessionRevokeReason.LOGOUT.value
        )
        revoked = family_revoked + (1 if was_live else 0)
        logger.info("logout", session_id=auth_session.session_id, revoked_sessions=revoked)
        return revoked

    def logout_all(self, *, user_id: int, except_session_pk: int | None = None) -> int:
        revoked = self._sessions.revoke_all_for_user(
            user_id,
            reason=SessionRevokeReason.LOGOUT_ALL.value,
            except_session_id=except_session_pk,
        )
        logger.info("logout all", user_id=user_id, revoked_sessions=revoked)
        return revoked

    # -- inspection -------------------------------------------------------
    def list_sessions(self, *, user_id: int) -> list[AuthSession]:
        return self._sessions.get_active_for_user(user_id)

    def revoke_session(self, *, user_id: int, session_pk: int) -> bool:
        """Revoke one session, refusing to touch another user's row.

        The ownership check lives here rather than in the router so that no
        future caller can forget it.
        """
        auth_session = self._sessions.get(session_pk)
        if auth_session is None or auth_session.user_id != user_id:
            return False
        self._sessions.revoke(auth_session, reason=SessionRevokeReason.ADMIN_REVOKED.value)
        return True

    def principal_for_user(self, user_id: int, *, session_id: str) -> Principal:
        user = self._users.get_with_authorization(user_id)
        if user is None:
            raise AuthenticationError("account no longer exists")
        return self._build_principal(user, session_id=session_id)

    def change_password(self, *, user_id: int, current_password: str, new_password: str) -> None:
        """Change a password and revoke every other session.

        Revoking siblings is the point: after a password change the old
        credential must not remain usable anywhere, and a user who changes their
        password *because* they suspect compromise expects exactly this.
        """
        user = self._users.get(user_id)
        if user is None:
            raise AuthenticationError("account no longer exists")
        if not verify_password(current_password, user.password_hash):
            raise InvalidCredentialsError("current password is incorrect")
        if new_password == current_password:
            raise ValidationError("new password must differ from the current password")

        self._users.update_password(user, hash_password(new_password))
        self._sessions.revoke_all_for_user(
            user_id, reason=SessionRevokeReason.PASSWORD_CHANGED.value
        )


class AddressService:
    """Address book use cases.

    Kept separate from ``AuthService`` because it is a different concern with a
    different threat profile (IDOR on a user-owned row rather than credential
    handling), even though both live in the identity context.
    """

    MAX_ADDRESSES_PER_USER = 20

    def __init__(self, session: Session) -> None:
        self._session = session
        self._addresses = AddressRepository(session)

    def list(self, *, user_id: int) -> list[UserAddress]:
        return self._addresses.list_for_user(user_id)

    def get(self, *, user_id: int, address_id: int) -> UserAddress:
        address = self._addresses.get_owned(address_id=address_id, user_id=user_id)
        if address is None:
            from app.core.errors import AddressNotFoundError

            # Deliberately the same error whether the row is absent or belongs to
            # somebody else: distinguishing them confirms the existence of
            # another user's address id (§109 IDOR).
            raise AddressNotFoundError("address not found")
        return address

    def create(
        self,
        *,
        user_id: int,
        merchant_id: int | None,
        receiver_name: str,
        receiver_phone: str,
        province: str,
        city: str,
        district: str,
        detail: str,
        postal_code: str | None = None,
        tag: str = AddressTag.HOME.value,
        is_default: bool = False,
    ) -> UserAddress:
        if self._addresses.count_for_user(user_id) >= self.MAX_ADDRESSES_PER_USER:
            raise ValidationError(f"at most {self.MAX_ADDRESSES_PER_USER} addresses are allowed")

        first_address = self._addresses.count_for_user(user_id) == 0
        make_default = is_default or first_address
        if make_default:
            self._addresses.clear_default(user_id)

        address = UserAddress(
            user_id=user_id,
            merchant_id=merchant_id,
            receiver_name=receiver_name.strip(),
            receiver_phone=receiver_phone.strip(),
            province=province.strip(),
            city=city.strip(),
            district=district.strip(),
            detail=detail.strip(),
            postal_code=(postal_code or "").strip() or None,
            tag=tag,
            is_default=make_default,
        )
        return self._addresses.add(address)

    def update(
        self,
        *,
        user_id: int,
        address_id: int,
        changes: dict[str, object],
    ) -> UserAddress:
        address = self.get(user_id=user_id, address_id=address_id)
        allowed = {
            "receiver_name",
            "receiver_phone",
            "province",
            "city",
            "district",
            "detail",
            "postal_code",
            "tag",
            "is_default",
        }
        # Mass-assignment guard (§110): only explicitly listed fields can be
        # written, so a caller cannot set user_id, merchant_id or created_at by
        # including them in the body.
        unknown = set(changes) - allowed
        if unknown:
            raise ValidationError(f"fields cannot be modified: {sorted(unknown)}")

        if changes.get("is_default") is True:
            self._addresses.clear_default(user_id, except_id=address.id)

        for key, value in changes.items():
            if key == "postal_code":
                setattr(address, key, (str(value) or "").strip() or None)
            elif isinstance(value, str):
                setattr(address, key, value.strip())
            else:
                setattr(address, key, value)
        self._session.flush()
        return address

    def delete(self, *, user_id: int, address_id: int) -> None:
        address = self.get(user_id=user_id, address_id=address_id)
        was_default = address.is_default
        self._addresses.soft_delete(address)
        if was_default:
            remaining = self._addresses.list_for_user(user_id)
            if remaining:
                # Promote another address so checkout always has a default and
                # never has to guess.
                remaining[0].is_default = True
                self._session.flush()

    def set_default(self, *, user_id: int, address_id: int) -> UserAddress:
        address = self.get(user_id=user_id, address_id=address_id)
        self._addresses.clear_default(user_id, except_id=address.id)
        address.is_default = True
        self._session.flush()
        return address


__all__ = [
    "REUSE_GRACE_SECONDS",
    "AddressService",
    "AuthService",
    "IssuedSession",
    "Principal",
]
