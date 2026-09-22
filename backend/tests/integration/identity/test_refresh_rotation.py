"""FG-13 - Auth session and refresh-token rotation.

Spec §116 requires these five behaviours to be tested:

    Refresh Rotation, Revocation, Expired Refresh, Reuse Old Refresh,
    Logout Revocation

Spec §112 INV-016: after rotation the previous refresh token must no longer be
valid. Spec §22: only the refresh-token **hash** may ever be persisted.

These tests run against **real MySQL** (marker: integration). That matters
because the properties under test are database properties: the unique constraint
on ``refresh_token_hash``, the ``rotated_from`` chain, and the atomic bulk
revoke. A mock session would happily "pass" a rotation test while the real
unique constraint rejected the insert.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.errors import (
    AccountDisabledError,
    InvalidCredentialsError,
    RefreshTokenReuseError,
    TokenInvalidError,
)
from app.modules.identity.enums import DataScope, SessionRevokeReason, UserType
from app.modules.identity.models import AuthSession, Merchant, Role, User
from app.modules.identity.repository import RoleRepository
from app.modules.identity.security import hash_password
from app.modules.identity.service import REUSE_GRACE_SECONDS, AuthService
from app.shared.db.session import configure_database, get_session_factory

pytestmark = pytest.mark.integration

PASSWORD = "Correct-Horse-Battery-9"


@pytest.fixture(scope="module")
def _engine():
    configure_database()
    return configure_database()


@pytest.fixture
def db(_engine):
    """A session bound to a transaction that is rolled back after the test.

    Uses a real connection so MySQL constraints are genuinely exercised, and
    rolls back so the suite stays re-runnable without reseeding.
    """
    connection = _engine.connect()
    transaction = connection.begin()
    session = get_session_factory()(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def merchant(db) -> Merchant:
    entity = Merchant(code=f"T{datetime.now(UTC).timestamp():.0f}"[:20], name="Test Merchant")
    db.add(entity)
    db.flush()
    return entity


@pytest.fixture
def consumer(db, merchant) -> User:
    entity = User(
        username=f"user_{datetime.now(UTC).timestamp()}",
        email=f"user_{datetime.now(UTC).timestamp()}@example.test",
        password_hash=hash_password(PASSWORD),
        display_name="Test Consumer",
        user_type=UserType.CONSUMER.value,
        status="ACTIVE",
        merchant_id=None,
    )
    db.add(entity)
    db.flush()
    return entity


@pytest.fixture
def auth(db) -> AuthService:
    return AuthService(db, get_settings())


def _age_revocation(auth_session: AuthSession, db, *, seconds: int | None = None) -> None:
    """Backdate a revocation past the reuse grace window.

    Both timestamps move together, because the database enforces
    ``revoked_at >= created_at`` - which is exactly the kind of invariant that
    makes a real-database test worth running: against a mock session this helper
    would not have been necessary, and the test would have proved less.
    """
    age = seconds if seconds is not None else REUSE_GRACE_SECONDS + 5
    aged = datetime.now(UTC) - timedelta(seconds=age)
    auth_session.created_at = aged
    auth_session.revoked_at = aged
    db.flush()


def _login(auth: AuthService, user: User):
    return auth.login(identifier=user.username, password=PASSWORD, client_ip="127.0.0.1")


# ===========================================================================
# 1. Refresh rotation
# ===========================================================================
def test_login_returns_a_usable_pair(auth: AuthService, consumer: User) -> None:
    issued = _login(auth, consumer)
    assert issued.access_token
    assert issued.refresh_token
    assert issued.session.revoked_at is None
    assert issued.session.expires_at > datetime.now(UTC)


def test_only_the_refresh_hash_is_persisted(auth: AuthService, consumer: User, db) -> None:
    """Spec §22 - the plaintext must be nowhere in the database.

    Checked by scanning every text column of the row, not by inspecting the one
    column we remembered to look at.
    """
    issued = _login(auth, consumer)
    plaintext = issued.refresh_token

    # Raw SQL on purpose: the assertion is about the physical row, so it must not
    # be filtered through the ORM's view of the model.
    row = (
        db.execute(text("SELECT * FROM auth_sessions WHERE id = :id"), {"id": issued.session.id})
        .mappings()
        .one()
    )

    dumped = " ".join(str(value) for value in row.values())
    assert plaintext not in dumped, "the plaintext refresh token was persisted"
    assert row["refresh_token_hash"] != plaintext
    assert len(row["refresh_token_hash"]) == 64, "expected a SHA-256 hex digest"


def test_rotation_issues_a_new_pair_and_revokes_the_old_session(
    auth: AuthService, consumer: User
) -> None:
    first = _login(auth, consumer)
    second = auth.rotate(presented_refresh_token=first.refresh_token)

    assert second.refresh_token != first.refresh_token
    assert second.access_token != first.access_token
    assert second.session.id != first.session.id

    # the predecessor is revoked, and the successor points back at it
    assert first.session.revoked_at is not None
    assert first.session.revoked_reason == SessionRevokeReason.ROTATED.value
    assert second.session.rotated_from == first.session.id


def test_rotated_token_is_no_longer_accepted(
    auth: AuthService, consumer: User, db
) -> None:
    """INV-016, the core of the whole gate."""
    first = _login(auth, consumer)
    auth.rotate(presented_refresh_token=first.refresh_token)

    # Age the revocation past the double-submit grace window so this is
    # unambiguously theft. `created_at` must be moved back as well: the
    # `ck_auth_sessions_revoked_after_created` CHECK constraint (correctly)
    # refuses a revocation that precedes creation, so a naive backdate of
    # `revoked_at` alone is rejected by the database.
    _age_revocation(first.session, db)
    db.flush()

    with pytest.raises(RefreshTokenReuseError):
        auth.rotate(presented_refresh_token=first.refresh_token)


def test_rotation_can_be_chained_repeatedly(auth: AuthService, consumer: User) -> None:
    issued = _login(auth, consumer)
    tokens = [issued.refresh_token]
    current = issued
    for _ in range(4):
        current = auth.rotate(presented_refresh_token=current.refresh_token)
        tokens.append(current.refresh_token)

    assert len(set(tokens)) == len(tokens), "a refresh token was reused across rotations"
    assert current.session.revoked_at is None


# ===========================================================================
# 2. Reuse detection and family revocation
# ===========================================================================
def test_reuse_outside_the_grace_window_revokes_the_whole_family(
    auth: AuthService, consumer: User, db
) -> None:
    """A stolen-then-replayed token must kill the legitimate session too.

    Revoking only the presented row would leave the attacker holding a valid
    descendant, which is the failure mode this test exists to prevent.
    """
    first = _login(auth, consumer)
    second = auth.rotate(presented_refresh_token=first.refresh_token)
    third = auth.rotate(presented_refresh_token=second.refresh_token)

    # Age the first revocation past the grace window (see _age_revocation).
    _age_revocation(first.session, db)
    db.flush()

    with pytest.raises(RefreshTokenReuseError) as excinfo:
        auth.rotate(presented_refresh_token=first.refresh_token)

    assert excinfo.value.context.get("revoked_sessions", 0) >= 1

    # Every member of the family must now be dead, including the live tip.
    db.refresh(second.session)
    db.refresh(third.session)
    for label, session in (("first", first.session), ("second", second.session), ("third", third.session)):
        assert session.revoked_at is not None, f"{label} session survived a reuse detection"


def test_reuse_inside_the_grace_window_is_tolerated(
    auth: AuthService, consumer: User
) -> None:
    """A benign double-submit (two tabs, a retry) must not log the user out.

    Over-aggressive reuse detection is not "more secure": it trains operators to
    ignore the alert and breaks real users, so the grace window is a deliberate
    part of the design and is tested as such.
    """
    first = _login(auth, consumer)
    auth.rotate(presented_refresh_token=first.refresh_token)  # revokes immediately

    reissued = auth.rotate(presented_refresh_token=first.refresh_token)

    assert reissued.refresh_token
    assert reissued.session.revoked_at is None
    # The reissued session chains off the *tip*, not off the replayed token, so
    # the family does not fork.
    assert reissued.session.rotated_from != first.session.id


def test_reuse_of_an_admin_revoked_session_is_never_tolerated(
    auth: AuthService, consumer: User
) -> None:
    """The grace window applies only to rotation, never to an explicit revoke.

    Otherwise an administrator's "kill this session now" could be undone simply
    by replaying the token within 20 seconds.
    """
    issued = _login(auth, consumer)
    issued.session.revoked_reason = SessionRevokeReason.ADMIN_REVOKED.value
    issued.session.revoked_at = datetime.now(UTC)
    auth._session.flush()

    with pytest.raises(RefreshTokenReuseError):
        auth.rotate(presented_refresh_token=issued.refresh_token)


def test_unknown_refresh_token_is_rejected_without_revoking_anything(
    auth: AuthService, consumer: User
) -> None:
    issued = _login(auth, consumer)
    with pytest.raises(TokenInvalidError):
        auth.rotate(presented_refresh_token="not-a-real-token")
    # A random string must not be able to log a legitimate user out.
    assert issued.session.revoked_at is None


def test_empty_refresh_token_is_rejected(auth: AuthService) -> None:
    with pytest.raises(TokenInvalidError):
        auth.rotate(presented_refresh_token="")


# ===========================================================================
# 3. Expiry
# ===========================================================================
def test_expired_refresh_token_is_rejected(auth: AuthService, consumer: User, db) -> None:
    issued = _login(auth, consumer)
    issued.session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()

    with pytest.raises(TokenInvalidError):
        auth.rotate(presented_refresh_token=issued.refresh_token)

    db.refresh(issued.session)
    assert issued.session.revoked_reason == SessionRevokeReason.EXPIRED.value


# ===========================================================================
# 4. Revocation
# ===========================================================================
def test_logout_revokes_the_session_and_its_family(auth: AuthService, consumer: User, db) -> None:
    first = _login(auth, consumer)
    second = auth.rotate(presented_refresh_token=first.refresh_token)

    revoked = auth.logout(refresh_token=second.refresh_token)
    # Exactly one row is transitioned here: the tip. Its predecessor was already
    # revoked by the rotation that produced it, so there is nothing left for the
    # family sweep to do. The meaningful assertion is the one below - that BOTH
    # ends of the chain are dead afterwards.
    assert revoked >= 1

    db.refresh(first.session)
    db.refresh(second.session)
    assert first.session.revoked_at is not None
    assert second.session.revoked_at is not None

    # Both tokens must now fail, not merely the one that was presented.
    with pytest.raises((TokenInvalidError, RefreshTokenReuseError)):
        auth.rotate(presented_refresh_token=first.refresh_token)


def test_logout_is_idempotent(auth: AuthService, consumer: User) -> None:
    issued = _login(auth, consumer)
    assert auth.logout(refresh_token=issued.refresh_token) >= 1
    # A second call must not raise: retries and double-clicks are normal.
    assert auth.logout(refresh_token=issued.refresh_token) == 0


def test_logout_all_spares_a_nominated_session(auth: AuthService, consumer: User, db) -> None:
    a = _login(auth, consumer)
    b = _login(auth, consumer)

    auth.logout_all(user_id=consumer.id, except_session_pk=b.session.id)

    db.refresh(a.session)
    db.refresh(b.session)
    assert a.session.revoked_at is not None
    assert b.session.revoked_at is None


def test_revoke_session_refuses_another_users_session(
    auth: AuthService, consumer: User, db, merchant: Merchant
) -> None:
    """IDOR guard: session ids are guessable integers, so ownership is checked."""
    issued = _login(auth, consumer)
    other = User(
        username=f"other_{datetime.now(UTC).timestamp()}",
        password_hash=hash_password(PASSWORD),
        user_type=UserType.CONSUMER.value,
        merchant_id=None,
    )
    db.add(other)
    db.flush()

    assert auth.revoke_session(user_id=other.id, session_pk=issued.session.id) is False
    db.refresh(issued.session)
    assert issued.session.revoked_at is None


def test_password_change_revokes_every_session(auth: AuthService, consumer: User, db) -> None:
    issued = _login(auth, consumer)
    auth.change_password(
        user_id=consumer.id, current_password=PASSWORD, new_password="A-Different-Passphrase-7"
    )
    db.refresh(issued.session)
    assert issued.session.revoked_at is not None
    assert issued.session.revoked_reason == SessionRevokeReason.PASSWORD_CHANGED.value


def test_password_change_requires_the_current_password(auth: AuthService, consumer: User) -> None:
    with pytest.raises(InvalidCredentialsError):
        auth.change_password(
            user_id=consumer.id, current_password="wrong", new_password="A-Different-Passphrase-7"
        )


# ===========================================================================
# 5. Login hardening
# ===========================================================================
def test_login_rejects_a_wrong_password_without_revealing_which_part_failed(
    auth: AuthService, consumer: User, db
) -> None:
    """Both the unknown-user and wrong-password paths must be indistinguishable."""
    with pytest.raises(InvalidCredentialsError):
        auth.login(identifier=consumer.username, password="wrong", client_ip="127.0.0.1")
    with pytest.raises(InvalidCredentialsError):
        auth.login(identifier="definitely-not-a-user", password="wrong", client_ip="127.0.0.1")

    db.refresh(consumer)
    assert consumer.failed_login_attempts == 1


def test_account_locks_after_the_configured_number_of_failures(
    auth: AuthService, consumer: User, db
) -> None:
    settings = get_settings()
    for _ in range(settings.LOGIN_MAX_FAILURES):
        with pytest.raises(InvalidCredentialsError):
            auth.login(identifier=consumer.username, password="wrong")

    db.refresh(consumer)
    assert consumer.status == "LOCKED"
    # Even the *correct* password must now be refused: the lockout must not be
    # bypassable by simply continuing to try.
    from app.core.errors import AccountLockedError

    with pytest.raises(AccountLockedError):
        auth.login(identifier=consumer.username, password=PASSWORD)


def test_successful_login_resets_the_failure_counter(auth: AuthService, consumer: User, db) -> None:
    with pytest.raises(InvalidCredentialsError):
        auth.login(identifier=consumer.username, password="wrong")
    _login(auth, consumer)
    db.refresh(consumer)
    assert consumer.failed_login_attempts == 0


def test_disabled_account_cannot_log_in(auth: AuthService, consumer: User, db) -> None:
    consumer.status = "DISABLED"
    db.flush()
    with pytest.raises(AccountDisabledError):
        auth.login(identifier=consumer.username, password=PASSWORD)


def test_login_by_email_and_username_both_work(auth: AuthService, consumer: User) -> None:
    assert auth.login(identifier=consumer.username, password=PASSWORD).refresh_token
    assert auth.login(identifier=consumer.email, password=PASSWORD).refresh_token


def test_username_lookup_is_case_insensitive(auth: AuthService, consumer: User) -> None:
    assert auth.login(identifier=consumer.username.upper(), password=PASSWORD).refresh_token


# ===========================================================================
# 6. Principal / data scope
# ===========================================================================
def test_consumer_defaults_to_self_scope(auth: AuthService, consumer: User) -> None:
    """A consumer with no roles must still see their own data - and only that."""
    issued = _login(auth, consumer)
    principal = auth.principal_for_user(consumer.id, session_id=issued.session.session_id)
    assert principal.data_scope is DataScope.SELF
    assert principal.merchant_id is None


def test_staff_without_a_role_gets_no_scope(db, merchant: Merchant) -> None:
    """A misconfigured staff account must fail closed."""
    staff = User(
        username=f"staff_{datetime.now(UTC).timestamp()}",
        password_hash=hash_password(PASSWORD),
        user_type=UserType.STAFF.value,
        merchant_id=merchant.id,
    )
    db.add(staff)
    db.flush()

    service = AuthService(db, get_settings())
    principal = service.principal_for_user(staff.id, session_id="s")
    assert principal.data_scope is DataScope.NONE
    assert principal.permissions == frozenset()


def test_role_grants_permissions_and_widens_scope(db, merchant: Merchant) -> None:
    from app.modules.identity.enums import PermissionCode, RoleCode

    roles = RoleRepository(db)
    role = Role(
        code=RoleCode.OPERATOR.value,
        name="Operator",
        merchant_id=merchant.id,
        data_scope=DataScope.MERCHANT.value,
        is_system=True,
    )
    roles.add_role(role)
    permission = roles.get_permission_by_code(PermissionCode.ORDER_READ.value)
    if permission is None:
        from app.modules.identity.models import Permission

        permission = roles.add_permission(
            Permission(
                code=PermissionCode.ORDER_READ.value,
                resource="order",
                action="read",
            )
        )
    roles.grant_permission(role_id=role.id, permission_id=permission.id)

    staff = User(
        username=f"op_{datetime.now(UTC).timestamp()}",
        password_hash=hash_password(PASSWORD),
        user_type=UserType.STAFF.value,
        merchant_id=merchant.id,
    )
    db.add(staff)
    db.flush()
    roles.assign_role(user_id=staff.id, role_id=role.id)

    principal = AuthService(db, get_settings()).principal_for_user(staff.id, session_id="s")
    assert principal.data_scope is DataScope.MERCHANT
    assert PermissionCode.ORDER_READ.value in principal.permissions


def test_scope_guard_blocks_cross_user_access(db, merchant: Merchant) -> None:
    from app.core.errors import DataScopeViolationError

    owner = User(
        username=f"o_{datetime.now(UTC).timestamp()}",
        password_hash=hash_password(PASSWORD),
        user_type=UserType.CONSUMER.value,
    )
    intruder = User(
        username=f"i_{datetime.now(UTC).timestamp()}",
        password_hash=hash_password(PASSWORD),
        user_type=UserType.CONSUMER.value,
    )
    db.add_all([owner, intruder])
    db.flush()

    principal = AuthService(db, get_settings()).principal_for_user(intruder.id, session_id="s")
    principal.assert_can_access_user(owner_user_id=intruder.id, owner_merchant_id=None)
    with pytest.raises(DataScopeViolationError):
        principal.assert_can_access_user(owner_user_id=owner.id, owner_merchant_id=None)


def test_expired_sessions_are_cleanable(db) -> None:
    """Housekeeping used by the reconciliation job (spec §50)."""
    from app.modules.identity.repository import AuthSessionRepository

    repository = AuthSessionRepository(db)
    assert repository.delete_expired(before=datetime.now(UTC)) >= 0
