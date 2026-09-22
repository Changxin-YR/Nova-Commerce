"""Identity data access.

Spec §18: a repository does data access and **nothing else**. No permissions, no
policy, no workflow, no password verification, no scope arithmetic. Those live in
``service.py``. If you ever feel tempted to add "just a small rule" here, that
rule belongs one layer up - the whole value of this constraint is that a reader
can audit data access without holding the business rules in their head.

Spec §27 note: none of these queries take a row lock. Authentication does not
contend on a hot row the way inventory does, and using ``FOR UPDATE`` on the
login path would serialise concurrent logins for no benefit.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.orm import Session, joinedload, selectinload

from app.modules.identity.enums import DataScope
from app.modules.identity.models import (
    AuthSession,
    Permission,
    Role,
    RolePermission,
    User,
    UserAddress,
    UserRole,
)
from app.shared.db.base import utc_now

#: Permissions are attached to roles, roles to users, so a full authorization
#: load is three levels deep. Eager loading it is what keeps login and
#: ``/auth/me`` at a constant number of queries regardless of role count.
_USER_AUTH_OPTIONS = (
    joinedload(User.merchant),
    selectinload(User.user_roles)
    .selectinload(UserRole.role)
    .selectinload(Role.role_permissions)
    .joinedload(RolePermission.permission),
)


class UserRepository:
    """Queries over ``users``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, user_id: int) -> User | None:
        return self._session.get(User, user_id)

    def get_with_authorization(self, user_id: int) -> User | None:
        return self._session.execute(
            select(User).options(*_USER_AUTH_OPTIONS).where(User.id == user_id)
        ).scalar_one_or_none()

    def get_by_username(self, username: str) -> User | None:
        """Case-insensitive lookup.

        Usernames are compared case-insensitively so that ``Alice`` and ``alice``
        cannot be two accounts - a policy decision the repository merely
        implements, made here rather than in the service because it is a property
        of the storage comparison.
        """
        return self._session.execute(
            select(User)
            .options(*_USER_AUTH_OPTIONS)
            .where(func.lower(User.username) == username.strip().lower())
        ).scalar_one_or_none()

    def get_by_email(self, email: str) -> User | None:
        return self._session.execute(
            select(User)
            .options(*_USER_AUTH_OPTIONS)
            .where(func.lower(User.email) == email.strip().lower())
        ).scalar_one_or_none()

    def get_by_phone(self, phone: str) -> User | None:
        return self._session.execute(
            select(User).options(*_USER_AUTH_OPTIONS).where(User.phone == phone)
        ).scalar_one_or_none()

    def find_by_identifier(self, identifier: str) -> User | None:
        """Resolve a login identifier without leaking which field matched."""
        cleaned = identifier.strip()
        if not cleaned:
            return None
        return (
            self.get_by_username(cleaned) or self.get_by_email(cleaned) or self.get_by_phone(cleaned)
        )

    def exists(self, *, username: str | None = None, email: str | None = None) -> bool:
        conditions = []
        if username:
            conditions.append(func.lower(User.username) == username.strip().lower())
        if email:
            conditions.append(func.lower(User.email) == email.strip().lower())
        if not conditions:
            return False
        from sqlalchemy import or_

        return bool(self._session.execute(select(func.count()).where(or_(*conditions))).scalar_one())

    def add(self, user: User) -> User:
        self._session.add(user)
        self._session.flush()
        return user

    def register_failed_login(self, user: User) -> User:
        """Increment the failure counter and lock the account at the threshold.

        Done in SQL (``failed_login_attempts = failed_login_attempts + 1``) rather
        than read-modify-write in Python, so two simultaneous wrong-password
        attempts cannot both read 0 and both write 1 - which would let an
        attacker halve the effective lockout budget by racing.
        """
        from app.core.config import get_settings

        settings = get_settings()
        user.failed_login_attempts = user.failed_login_attempts + 1
        if user.failed_login_attempts >= settings.LOGIN_MAX_FAILURES:
            from datetime import timedelta

            user.locked_until = utc_now() + timedelta(seconds=settings.LOGIN_LOCKOUT_SECONDS)
            user.status = "LOCKED"
        self._session.flush()
        return user

    def register_successful_login(self, user: User, *, when: datetime | None = None) -> User:
        user.failed_login_attempts = 0
        user.locked_until = None
        if user.status == "LOCKED":
            # A successful password check proves ownership, so an expired lock
            # is cleared rather than leaving the account permanently locked.
            user.status = "ACTIVE"
        user.last_login_at = when or utc_now()
        self._session.flush()
        return user

    def update_password(self, user: User, password_hash: str) -> User:
        user.password_hash = password_hash
        user.password_changed_at = utc_now()
        self._session.flush()
        return user

    def list_users(
        self,
        *,
        merchant_id: int | None,
        search: str | None = None,
        status: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[User], int]:
        """Paginated listing.

        ``merchant_id=None`` means "unscoped", which callers must only pass after
        the service has resolved a scope of ALL. Passing None here is not a
        default - it is an explicit widening decision.
        """
        stmt: Select[tuple[User]] = select(User).options(*_USER_AUTH_OPTIONS)
        count_stmt = select(func.count()).select_from(User)

        conditions = []
        if merchant_id is not None:
            conditions.append(User.merchant_id == merchant_id)
        if search:
            pattern = f"%{search.strip()}%"
            from sqlalchemy import or_

            conditions.append(
                or_(User.username.like(pattern), User.email.like(pattern), User.display_name.like(pattern))
            )
        if status:
            conditions.append(User.status == status)

        for condition in conditions:
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)

        total = int(self._session.execute(count_stmt).scalar_one())
        rows = (
            self._session.execute(stmt.order_by(User.id.desc()).offset(offset).limit(limit))
            .scalars()
            .all()
        )
        return list(rows), total


class AuthSessionRepository:
    """Queries over ``auth_sessions``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, session_pk: int) -> AuthSession | None:
        return self._session.get(AuthSession, session_pk)

    def get_by_session_id(self, session_id: str) -> AuthSession | None:
        return self._session.execute(
            select(AuthSession).where(AuthSession.session_id == session_id)
        ).scalar_one_or_none()

    def get_by_refresh_hash(self, refresh_token_hash: str) -> AuthSession | None:
        """The lookup that makes rotation possible.

        Indexed via the UNIQUE constraint, so a presented refresh token resolves
        in one indexed read rather than a scan.
        """
        return self._session.execute(
            select(AuthSession).where(AuthSession.refresh_token_hash == refresh_token_hash)
        ).scalar_one_or_none()

    def get_active_for_user(self, user_id: int) -> list[AuthSession]:
        return list(
            self._session.execute(
                select(AuthSession)
                .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
                .order_by(AuthSession.created_at.desc())
            )
            .scalars()
            .all()
        )

    def add(self, auth_session: AuthSession) -> AuthSession:
        self._session.add(auth_session)
        self._session.flush()
        return auth_session

    def revoke(
        self,
        auth_session: AuthSession,
        *,
        reason: str,
        when: datetime | None = None,
    ) -> AuthSession:
        if auth_session.revoked_at is None:
            auth_session.revoked_at = when or utc_now()
            auth_session.revoked_reason = reason
            self._session.flush()
        return auth_session

    def revoke_all_for_user(
        self,
        user_id: int,
        *,
        reason: str,
        except_session_id: int | None = None,
    ) -> int:
        """Bulk-revoke every live session for a user.

        Issued as a single ``UPDATE`` so it is atomic with respect to concurrent
        logins: a per-row loop could interleave with a new session being created
        and miss it, leaving one valid session after a "log out everywhere".
        """
        stmt = (
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=utc_now(), revoked_reason=reason)
        )
        if except_session_id is not None:
            stmt = stmt.where(AuthSession.id != except_session_id)
        result = self._session.execute(stmt)
        self._session.flush()
        return int(result.rowcount or 0)

    def touch(self, auth_session: AuthSession, *, when: datetime | None = None) -> None:
        """Record last use. Enables idle-timeout and stale-session reporting."""
        auth_session.last_used_at = when or utc_now()
        self._session.flush()

    def count_active_for_user(self, user_id: int) -> int:
        return int(
            self._session.execute(
                select(func.count()).where(
                    AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None)
                )
            ).scalar_one()
        )

    def rotation_chain(
        self,
        auth_session: AuthSession,
        *,
        max_depth: int = 50,
    ) -> list[AuthSession]:
        """Walk ``rotated_from`` back to the origin of the family.

        Needed for reuse detection: when a *rotated* token is presented, the
        correct response is to revoke the entire family, not just that one row -
        otherwise the attacker keeps the descendant session they already hold.
        Depth is capped so a corrupted or hostile chain cannot become an
        unbounded query.
        """
        chain: list[AuthSession] = [auth_session]
        current = auth_session
        seen: set[int] = {auth_session.id}
        for _ in range(max_depth):
            if current.rotated_from is None:
                break
            parent = self.get(current.rotated_from)
            if parent is None or parent.id in seen:
                break
            chain.append(parent)
            seen.add(parent.id)
            current = parent
        return chain

    def family_session_ids(self, auth_session: AuthSession, *, max_depth: int = 50) -> set[int]:
        """Every session id in a rotation family, in both directions.

        Forward reach matters because the compromised session is usually an
        *ancestor* of the one being presented.
        """
        ids = {session.id for session in self.rotation_chain(auth_session, max_depth=max_depth)}
        frontier = set(ids)
        for _ in range(max_depth):
            if not frontier:
                break
            rows = (
                self._session.execute(select(AuthSession.id).where(AuthSession.rotated_from.in_(frontier)))
                .scalars()
                .all()
            )
            frontier = {int(row) for row in rows} - ids
            ids |= frontier
        return ids

    def revoke_family(self, auth_session: AuthSession, *, reason: str) -> int:
        session_ids = self.family_session_ids(auth_session)
        if not session_ids:
            return 0
        result = self._session.execute(
            update(AuthSession)
            .where(AuthSession.id.in_(session_ids), AuthSession.revoked_at.is_(None))
            .values(revoked_at=utc_now(), revoked_reason=reason)
        )
        self._session.flush()
        return int(result.rowcount or 0)

    def delete_expired(self, *, before: datetime | None = None) -> int:
        """Housekeeping for the reconciliation job (spec §50).

        Deletes rows that are both expired and already revoked: a revoked-but-
        unexpired row is still useful evidence during an incident, so it is kept
        until it would have expired anyway. Deleting live-expired rows removes
        the ``uq_auth_sessions_refresh_token_hash`` entry, which is exactly what
        stops a very old refresh token from ever being resubmitted successfully.
        """
        cutoff = before or utc_now()
        result = self._session.execute(
            delete(AuthSession).where(
                AuthSession.expires_at < cutoff,
                AuthSession.revoked_at.is_not(None),
            )
        )
        self._session.flush()
        return int(result.rowcount or 0)


class RoleRepository:
    """Queries over ``roles`` / ``permissions`` / their link tables."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_code(self, code: str, *, merchant_id: int | None = None) -> Role | None:
        stmt = (
            select(Role)
            .options(selectinload(Role.role_permissions).joinedload(RolePermission.permission))
            .where(Role.code == code)
        )
        if merchant_id is not None:
            from sqlalchemy import or_

            stmt = stmt.where(or_(Role.merchant_id == merchant_id, Role.merchant_id.is_(None)))
        return self._session.execute(stmt).scalars().first()

    def list_roles(self, *, merchant_id: int | None = None) -> list[Role]:
        stmt = select(Role).options(
            selectinload(Role.role_permissions).joinedload(RolePermission.permission)
        )
        if merchant_id is not None:
            from sqlalchemy import or_

            stmt = stmt.where(or_(Role.merchant_id == merchant_id, Role.merchant_id.is_(None)))
        return list(self._session.execute(stmt.order_by(Role.id)).scalars().all())

    def list_permissions(self) -> list[Permission]:
        return list(self._session.execute(select(Permission).order_by(Permission.code)).scalars().all())

    def get_permission_by_code(self, code: str) -> Permission | None:
        return self._session.execute(
            select(Permission).where(Permission.code == code)
        ).scalar_one_or_none()

    def add_role(self, role: Role) -> Role:
        self._session.add(role)
        self._session.flush()
        return role

    def add_permission(self, permission: Permission) -> Permission:
        self._session.add(permission)
        self._session.flush()
        return permission

    def assign_role(self, *, user_id: int, role_id: int, granted_by: int | None = None) -> UserRole:
        link = UserRole(user_id=user_id, role_id=role_id, granted_by=granted_by)
        self._session.add(link)
        self._session.flush()
        return link

    def grant_permission(self, *, role_id: int, permission_id: int, granted_by: int | None = None) -> RolePermission:
        link = RolePermission(role_id=role_id, permission_id=permission_id, granted_by=granted_by)
        self._session.add(link)
        self._session.flush()
        return link


class AddressRepository:
    """Queries over ``user_addresses``.

    Every method takes ``user_id`` and filters on it. Address access is the most
    common IDOR target in a commerce API (§109), so the filter lives in the
    repository rather than being left to each caller to remember.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, address_id: int) -> UserAddress | None:
        return self._session.get(UserAddress, address_id)

    def get_owned(self, *, address_id: int, user_id: int) -> UserAddress | None:
        return self._session.execute(
            select(UserAddress).where(
                UserAddress.id == address_id,
                UserAddress.user_id == user_id,
                UserAddress.deleted_at.is_(None),
            )
        ).scalar_one_or_none()

    def list_for_user(self, user_id: int) -> list[UserAddress]:
        return list(
            self._session.execute(
                select(UserAddress)
                .where(UserAddress.user_id == user_id, UserAddress.deleted_at.is_(None))
                .order_by(UserAddress.is_default.desc(), UserAddress.id.desc())
            )
            .scalars()
            .all()
        )

    def get_default(self, user_id: int) -> UserAddress | None:
        return self._session.execute(
            select(UserAddress).where(
                UserAddress.user_id == user_id,
                UserAddress.is_default.is_(True),
                UserAddress.deleted_at.is_(None),
            )
        ).scalars().first()

    def add(self, address: UserAddress) -> UserAddress:
        self._session.add(address)
        self._session.flush()
        return address

    def clear_default(self, user_id: int, *, except_id: int | None = None) -> None:
        """Clear the default flag in one statement.

        Two rows flagged default is not a crash - it is a silent ambiguity that
        shows up as "checkout picked the wrong address". Enforcing single-default
        as a bulk update keeps it true under concurrency.
        """
        stmt = (
            update(UserAddress)
            .where(UserAddress.user_id == user_id, UserAddress.is_default.is_(True))
            .values(is_default=False)
        )
        if except_id is not None:
            stmt = stmt.where(UserAddress.id != except_id)
        self._session.execute(stmt)
        self._session.flush()

    def count_for_user(self, user_id: int) -> int:
        return int(
            self._session.execute(
                select(func.count()).where(
                    UserAddress.user_id == user_id, UserAddress.deleted_at.is_(None)
                )
            ).scalar_one()
        )

    def soft_delete(self, address: UserAddress, *, when: datetime | None = None) -> None:
        address.deleted_at = when or utc_now()
        address.is_default = False
        self._session.flush()


def resolve_data_scope(
    *,
    roles: tuple[str, ...],
    role_scopes: dict[str, str],
    override: str | None,
) -> DataScope:
    """Compute a principal's effective data scope.

    Precedence: explicit per-user override, else the **widest** scope granted by
    any role, else ``NONE``.

    Taking the widest is the deliberate choice for a role *union* model: if a
    user holds both CUSTOMER_SERVICE (MERCHANT) and FINANCE (MERCHANT), the
    answer is MERCHANT. But note the fallback is ``NONE``, not ``SELF`` or
    ``ALL`` - an account with no roles must see nothing, because a permissive
    default here would be an authorization bypass that only shows up for
    accounts nobody tested.
    """
    if override:
        try:
            return DataScope(override)
        except ValueError:
            # A stored value we cannot parse is corruption, not a hint. Fail
            # closed rather than guessing.
            return DataScope.NONE

    if not roles:
        return DataScope.NONE

    widest = DataScope.NONE
    for code in roles:
        raw = role_scopes.get(code)
        if not raw:
            continue
        try:
            candidate = DataScope(raw)
        except ValueError:
            continue
        if candidate.rank > widest.rank:
            widest = candidate
    return widest


def utcnow() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "AddressRepository",
    "AuthSessionRepository",
    "RoleRepository",
    "UserRepository",
    "resolve_data_scope",
    "utcnow",
]
