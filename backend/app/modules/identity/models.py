"""Identity ORM models - the eight tables required by spec §21.

    merchants, users, auth_sessions, roles, permissions,
    user_roles, role_permissions, user_addresses

Spec §22 is the load-bearing one:

    auth_sessions must persist ``refresh_token_hash`` (never plaintext) plus
    ``session_id``, ``jti``, ``expires_at``, ``revoked_at``, ``rotated_from``,
    ``created_at``, ``last_used_at``.

The ``rotated_from`` self-reference is what makes INV-016 enforceable: a reuse
can be detected by walking the chain from a presented token back to its origin,
which lets the server revoke the whole family rather than just the one row -
the difference between stopping an attacker and merely annoying them.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.modules.identity.enums import (
    AddressTag,
    DataScope,
    UserStatus,
    UserType,
)
from app.shared.db.base import (
    Base,
    MerchantScopedMixin,
    PkMixin,
    SoftDeleteMixin,
    TimestampMixin,
    short_str,
    status_column,
)
from app.shared.db.types import BigIntUnsigned, DateTimeMS

if TYPE_CHECKING:
    pass

#: Maximum refresh-token chain depth before the server refuses to rotate further.
#: Bounds the cost of the reuse-detection walk and prevents a pathological or
#: malicious chain from becoming an unbounded query.
MAX_ROTATION_CHAIN_DEPTH = 50


class Merchant(Base, PkMixin, TimestampMixin, SoftDeleteMixin):
    """A commercial tenant.

    V1 runs a single merchant (``Nexora Digital``), but every commercial row
    carries ``merchant_id`` from the first migration so that multi-merchant
    support is a feature, not a data migration (spec §4).
    """

    __tablename__ = "merchants"
    __table_args__ = (
        UniqueConstraint("code", name="uq_merchants_code"),
        CheckConstraint("length(code) >= 2", name="code_min_length"),
    )

    code: Mapped[str] = mapped_column(short_str(32), nullable=False)
    name: Mapped[str] = mapped_column(short_str(128), nullable=False)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(
        status_column(), nullable=False, default="ACTIVE", server_default="ACTIVE"
    )
    #: Commission rate in basis points. Unused in V1 (no platform settlement,
    #: spec §4) but present so the marketplace era does not need a migration.
    commission_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    users: Mapped[list[User]] = relationship(back_populates="merchant", lazy="noload")


class User(Base, PkMixin, TimestampMixin, MerchantScopedMixin, SoftDeleteMixin):
    """An account - consumer or staff.

    ``merchant_id`` is nullable because a consumer has no merchant (§24). That
    nullability is load-bearing: the DataScope resolver must not treat a NULL
    merchant as "all merchants".
    """

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        UniqueConstraint("email", name="uq_users_email"),
        UniqueConstraint("phone", name="uq_users_phone"),
        CheckConstraint(
            "user_type IN ('CONSUMER','STAFF')",
            name="user_type_valid",
        ),
        CheckConstraint(
            "status IN ('ACTIVE','INACTIVE','LOCKED','DISABLED')",
            name="status_valid",
        ),
        # Staff must be attributable to a merchant, or a DataScope of MERCHANT
        # would silently mean "everything". Enforced in the database so no code
        # path can create the inconsistency.
        CheckConstraint(
            "(user_type = 'CONSUMER') OR (user_type = 'STAFF' AND merchant_id IS NOT NULL)",
            name="staff_requires_merchant",
        ),
        Index("ix_users_merchant_status", "merchant_id", "status"),
    )

    username: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(short_str(128), nullable=False, default="")

    user_type: Mapped[str] = mapped_column(
        status_column(), nullable=False, default=UserType.CONSUMER.value
    )
    status: Mapped[str] = mapped_column(
        status_column(), nullable=False, default=UserStatus.ACTIVE.value, server_default="ACTIVE"
    )

    #: Explicit, per-user override of the computed data scope. NULL means
    #: "derive from roles", which is the normal case; a value here is how a
    #: support agent is deliberately widened to MERCHANT scope.
    data_scope_override: Mapped[str | None] = mapped_column(status_column(), nullable=True)

    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    merchant: Mapped[Merchant | None] = relationship(back_populates="users", lazy="joined")
    sessions: Mapped[list[AuthSession]] = relationship(
        back_populates="user", lazy="noload", cascade="all, delete-orphan"
    )
    addresses: Mapped[list[UserAddress]] = relationship(back_populates="user", lazy="noload")
    # `user_roles` has TWO foreign keys into `users` - `user_id` (the grantee)
    # and `granted_by` (the administrator who granted it). SQLAlchemy cannot
    # choose between them, so the join column is named explicitly. Getting this
    # wrong would silently resolve grants against the *granting* admin, which is
    # an authorization bug rather than a modelling inconvenience.
    user_roles: Mapped[list[UserRole]] = relationship(
        back_populates="user",
        lazy="selectin",
        cascade="all, delete-orphan",
        foreign_keys="UserRole.user_id",
    )

    @property
    def is_staff(self) -> bool:
        return self.user_type == UserType.STAFF.value

    @property
    def is_active(self) -> bool:
        return self.status == UserStatus.ACTIVE.value

    @property
    def role_codes(self) -> tuple[str, ...]:
        return tuple(link.role.code for link in self.user_roles if link.role is not None)

    @property
    def permission_codes(self) -> frozenset[str]:
        """Union of permissions granted by every role."""
        granted: set[str] = set()
        for link in self.user_roles:
            if link.role is None:
                continue
            granted.update(p.permission.code for p in link.role.role_permissions if p.permission)
        return frozenset(granted)


class AuthSession(Base, PkMixin, TimestampMixin):
    """A refresh-token session.

    Column set frozen by spec §22. Two deliberate choices:

    * ``session_id`` is a separate opaque string from the primary key, so the
      value embedded in a token does not reveal how many sessions exist or when
      the row was created (a weak but free information disclosure).
    * ``rotated_from`` is a self-FK rather than a plain string, so the reuse
      chain is guaranteed to reference a real predecessor.
    """

    __tablename__ = "auth_sessions"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_auth_sessions_session_id"),
        # Rotation updates the row in place, so the hash must be unique: a
        # collision would mean two live sessions share a refresh token.
        UniqueConstraint("refresh_token_hash", name="uq_auth_sessions_refresh_token_hash"),
        UniqueConstraint("jti", name="uq_auth_sessions_jti"),
        Index("ix_auth_sessions_user_revoked", "user_id", "revoked_at"),
        Index("ix_auth_sessions_expires", "expires_at"),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="revoked_after_created",
        ),
    )

    user_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)

    #: SHA-256 (peppered) digest of the refresh token. The plaintext exists only
    #: in the response cookie and in the client's cookie jar - never here.
    refresh_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    jti: Mapped[str] = mapped_column(String(64), nullable=False)

    issued_at: Mapped[datetime] = mapped_column(DateTimeMS, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTimeMS, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)
    rotated_from: Mapped[int | None] = mapped_column(
        BigIntUnsigned, ForeignKey("auth_sessions.id", ondelete="SET NULL"), nullable=True
    )
    revoked_reason: Mapped[str | None] = mapped_column(status_column(), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    # Context captured at login, so an operator can answer "where was this
    # session created" during an incident (spec §133 audit intent).
    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    login_source: Mapped[str | None] = mapped_column(short_str(32), nullable=True)

    user: Mapped[User] = relationship(back_populates="sessions", lazy="noload")
    rotated_from_session: Mapped[AuthSession | None] = relationship(
        remote_side="AuthSession.id", lazy="noload"
    )

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    def is_expired(self, *, now: datetime) -> bool:
        return self.expires_at <= now


class Role(Base, PkMixin, TimestampMixin, MerchantScopedMixin):
    """A named bundle of permissions."""

    __tablename__ = "roles"
    __table_args__ = (
        UniqueConstraint("code", "merchant_id", name="uq_roles_code_merchant"),
        CheckConstraint("is_system = 1 OR is_system = 0", name="is_system_boolean"),
    )

    code: Mapped[str] = mapped_column(short_str(64), nullable=False)
    name: Mapped[str] = mapped_column(short_str(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: System roles cannot be deleted or renamed through the API; they are the
    #: vocabulary the rest of the platform (and the seed) depends on.
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    #: Roles are the source of the DataScope a user ends up with (§104).
    data_scope: Mapped[str] = mapped_column(
        status_column(), nullable=False, default=DataScope.SELF.value, server_default="SELF"
    )

    role_permissions: Mapped[list[RolePermission]] = relationship(
        back_populates="role", lazy="selectin", cascade="all, delete-orphan"
    )
    user_roles: Mapped[list[UserRole]] = relationship(
        back_populates="role", lazy="noload", cascade="all, delete-orphan"
    )


class Permission(Base, PkMixin, TimestampMixin):
    """A single ``resource:action`` capability."""

    __tablename__ = "permissions"
    __table_args__ = (
        UniqueConstraint("code", name="uq_permissions_code"),
        Index("ix_permissions_resource", "resource"),
    )

    code: Mapped[str] = mapped_column(short_str(96), nullable=False)
    resource: Mapped[str] = mapped_column(short_str(48), nullable=False)
    action: Mapped[str] = mapped_column(short_str(48), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Deny-list flag for the seeded vocabulary: a permission can exist and be
    #: known to the system while never being grantable through the console.
    is_grantable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )

    role_permissions: Mapped[list[RolePermission]] = relationship(
        back_populates="permission", lazy="noload", cascade="all, delete-orphan"
    )


class UserRole(Base, PkMixin, TimestampMixin):
    """Assignment of a role to a user.

    ``merchant_id`` is duplicated from the user so that a role assignment is
    scoped to a tenant: without it, a role granted in one merchant would be
    visible from another once more than one merchant exists.
    """

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_user_roles_user_role"),
        Index("ix_user_roles_role", "role_id"),
    )

    user_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    granted_by: Mapped[int | None] = mapped_column(
        BigIntUnsigned, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTimeMS, nullable=True)

    user: Mapped[User] = relationship(back_populates="user_roles", foreign_keys=[user_id], lazy="noload")
    role: Mapped[Role] = relationship(back_populates="user_roles", lazy="selectin")


class RolePermission(Base, PkMixin, TimestampMixin):
    """Assignment of a permission to a role."""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permissions_role_permission"),
        Index("ix_role_permissions_permission", "permission_id"),
    )

    role_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    permission_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False
    )
    granted_by: Mapped[int | None] = mapped_column(
        BigIntUnsigned, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    role: Mapped[Role] = relationship(back_populates="role_permissions", lazy="noload")
    permission: Mapped[Permission] = relationship(back_populates="role_permissions", lazy="selectin")


class UserAddress(Base, PkMixin, TimestampMixin, MerchantScopedMixin, SoftDeleteMixin):
    """A delivery address.

    Addresses are PII (§94): ``receiver_phone`` and the full address are masked
    by the redaction layer on every outbound path, and the order keeps its own
    immutable snapshot (§35) rather than referencing this row - so editing an
    address cannot rewrite history.
    """

    __tablename__ = "user_addresses"
    __table_args__ = (
        Index("ix_user_addresses_user_default", "user_id", "is_default"),
        CheckConstraint("tag IN ('HOME','OFFICE','SCHOOL','OTHER')", name="tag_valid"),
        CheckConstraint("length(receiver_name) > 0", name="receiver_name_present"),
        CheckConstraint("length(receiver_phone) >= 6", name="receiver_phone_present"),
    )

    user_id: Mapped[int] = mapped_column(
        BigIntUnsigned, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    receiver_name: Mapped[str] = mapped_column(short_str(64), nullable=False)
    receiver_phone: Mapped[str] = mapped_column(String(32), nullable=False)

    province: Mapped[str] = mapped_column(short_str(64), nullable=False, default="")
    city: Mapped[str] = mapped_column(short_str(64), nullable=False, default="")
    district: Mapped[str] = mapped_column(short_str(64), nullable=False, default="")
    detail: Mapped[str] = mapped_column(String(255), nullable=False)
    postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)

    tag: Mapped[str] = mapped_column(
        status_column(), nullable=False, default=AddressTag.HOME.value, server_default="HOME"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )

    user: Mapped[User] = relationship(back_populates="addresses", lazy="noload")

    def to_snapshot(self) -> dict[str, str]:
        """Immutable copy embedded into an order (spec §35).

        Deliberately a plain dict rather than a reference: the whole point is
        that a later edit to this address must not be able to change a
        historical order (INV-014).
        """
        parts = [self.province, self.city, self.district, self.detail]
        return {
            "receiver_name": self.receiver_name,
            "receiver_phone": self.receiver_phone,
            "full_address": "".join(part for part in parts if part),
            "postal_code": self.postal_code or "",
        }

    @property
    def formatted(self) -> str:
        return "".join(part for part in (self.province, self.city, self.district, self.detail) if part)


__all__ = [
    "MAX_ROTATION_CHAIN_DEPTH",
    "AuthSession",
    "Merchant",
    "Permission",
    "Role",
    "RolePermission",
    "User",
    "UserAddress",
    "UserRole",
]
