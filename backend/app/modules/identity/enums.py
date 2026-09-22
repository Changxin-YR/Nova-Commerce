"""Identity domain vocabularies.

Spec references:
    §19 statuses are ``VARCHAR`` + Python Enum
    §21 the eight required identity tables
    §22 the ``auth_sessions`` column set
    §24 seed roles: CUSTOMER, CUSTOMER_SERVICE, OPERATOR, FINANCE, ADMIN
"""

from __future__ import annotations

from enum import StrEnum


class UserType(StrEnum):
    """Who the account belongs to.

    Consumers may have a ``NULL`` merchant_id (§24); staff always carry one,
    which is what makes the merchant guard in the repository meaningful.
    """

    CONSUMER = "CONSUMER"
    STAFF = "STAFF"


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    LOCKED = "LOCKED"
    DISABLED = "DISABLED"


class DataScope(StrEnum):
    """Row-level visibility (spec §104, §109 IDOR, §112 INV-010/INV-012).

    This is a *security* primitive, not a UI hint. Every repository query on
    scoped data is filtered through it, and the agent/MCP gateways intersect
    against it rather than replacing it.

    ``NONE`` exists so that "no scope at all" is representable: an anonymous or
    misconfigured principal must resolve to *nothing visible* rather than to the
    most permissive default. Defaulting to ``ALL`` is the classic way a scope
    system silently grants everything.
    """

    NONE = "NONE"
    SELF = "SELF"
    MERCHANT = "MERCHANT"
    ALL = "ALL"

    @property
    def rank(self) -> int:
        return _SCOPE_RANK[self]

    def covers(self, other: DataScope) -> bool:
        """True when this scope is at least as broad as ``other``."""
        return self.rank >= other.rank


_SCOPE_RANK: dict[DataScope, int] = {
    DataScope.NONE: 0,
    DataScope.SELF: 1,
    DataScope.MERCHANT: 2,
    DataScope.ALL: 3,
}


class SessionRevokeReason(StrEnum):
    """Why an ``auth_sessions`` row stopped being valid.

    Recorded rather than inferred, because "the user logged out" and "we detected
    refresh-token reuse" have very different incident meanings and the audit
    trail must distinguish them (§116).
    """

    LOGOUT = "LOGOUT"
    LOGOUT_ALL = "LOGOUT_ALL"
    ROTATED = "ROTATED"
    REUSE_DETECTED = "REUSE_DETECTED"
    EXPIRED = "EXPIRED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"  # noqa: S105 - a reason code, not a credential
    ADMIN_REVOKED = "ADMIN_REVOKED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"


class RoleCode(StrEnum):
    """Seeded roles (spec §24, §126)."""

    CUSTOMER = "CUSTOMER"
    CUSTOMER_SERVICE = "CUSTOMER_SERVICE"
    OPERATOR = "OPERATOR"
    FINANCE = "FINANCE"
    ADMIN = "ADMIN"


class PermissionCode(StrEnum):
    """Permission vocabulary.

    Naming is ``<resource>:<action>``. The list is intentionally explicit rather
    than wildcard-based: a wildcard grant is impossible to audit, and §90's MCP
    scope intersection needs a concrete set to intersect against.
    """

    # catalog
    PRODUCT_READ = "product:read"
    PRODUCT_WRITE = "product:write"
    PRODUCT_PUBLISH = "product:publish"
    CATEGORY_WRITE = "category:write"
    BRAND_WRITE = "brand:write"
    # inventory
    INVENTORY_READ = "inventory:read"
    INVENTORY_WRITE = "inventory:write"
    INVENTORY_ADJUST = "inventory:adjust"
    # orders
    ORDER_READ = "order:read"
    ORDER_READ_OWN = "order:read_own"
    ORDER_CANCEL = "order:cancel"
    ORDER_CONFIRM_RECEIPT = "order:confirm_receipt"
    ORDER_ADMIN = "order:admin"
    # payment
    PAYMENT_READ = "payment:read"
    PAYMENT_REFUND = "payment:refund"
    # fulfillment
    FULFILLMENT_READ = "fulfillment:read"
    FULFILLMENT_SHIP = "fulfillment:ship"
    # after-sales
    AFTER_SALE_READ = "after_sale:read"
    AFTER_SALE_REVIEW = "after_sale:review"
    AFTER_SALE_REQUEST = "after_sale:request"
    REFUND_EXECUTE = "refund:execute"
    # marketing
    PROMOTION_READ = "promotion:read"
    PROMOTION_WRITE = "promotion:write"
    COUPON_READ = "coupon:read"
    COUPON_WRITE = "coupon:write"
    # analytics
    ANALYTICS_READ = "analytics:read"
    ANALYTICS_EXPORT = "analytics:export"
    # knowledge
    KNOWLEDGE_READ = "knowledge:read"
    KNOWLEDGE_WRITE = "knowledge:write"
    KNOWLEDGE_INGEST = "knowledge:ingest"
    # agent / AI
    AGENT_CHAT = "agent:chat"
    AGENT_RUN_READ = "agent:run:read"
    # governance
    PENDING_ACTION_READ = "pending_action:read"
    PENDING_ACTION_APPROVE = "pending_action:approve"
    TOOL_REGISTRY_READ = "tool_registry:read"
    TOOL_REGISTRY_WRITE = "tool_registry:write"
    # system
    USER_READ = "user:read"
    USER_WRITE = "user:write"
    ROLE_ASSIGN = "role:assign"
    AUDIT_READ = "audit:read"
    SYSTEM_CONFIG = "system:config"
    DATA_SCOPE_WRITE = "data_scope:write"


class AddressTag(StrEnum):
    HOME = "HOME"
    OFFICE = "OFFICE"
    SCHOOL = "SCHOOL"
    OTHER = "OTHER"


__all__ = [
    "AddressTag",
    "DataScope",
    "PermissionCode",
    "RoleCode",
    "SessionRevokeReason",
    "UserStatus",
    "UserType",
]
