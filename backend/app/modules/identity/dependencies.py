"""FastAPI dependencies for authentication and authorization.

Spec references:
    §17  controllers are thin: parse, authenticate, call, map. This module owns the
         "authenticate" step so no controller repeats it.
    §104 route/menu/button permission is UX only; the check that matters is here.
    §109 JWT tampering, expired tokens, IDOR.
    §112 INV-010 agent effective permission <= current user permission.

Design note: the authorization check is a **dependency**, not a decorator applied
inside a service. That places it in the request pipeline, before any business code
runs, so a forgotten `if` inside a service cannot become an open endpoint. The
service layer still enforces row-level scope (`Principal.assert_can_access_*`),
because "may this caller use this endpoint" and "may this caller see this row" are
different questions and both must be answered.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.context import RequestContext, get_context, set_context
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.modules.identity.enums import DataScope, PermissionCode
from app.modules.identity.security import decode_access_token
from app.modules.identity.service import AuthService, Principal
from app.shared.db.session import get_session

#: ``auto_error=False`` so a missing header produces our own envelope-shaped 401
#: rather than FastAPI's default body. The wire format is frozen by §95 and an
#: inconsistency here would break the frontend's error mapper.
_bearer = HTTPBearer(auto_error=False, description="Short-lived access token (§23).")


def get_app_settings() -> Settings:
    return get_settings()


def get_current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> Principal:
    """Resolve and authorise the caller.

    Two things happen here that are easy to get wrong:

    1. **The token's embedded permissions are re-resolved from the database.** The
       access token carries roles/permissions so the common path needs no query -
       but the principal is rebuilt from the DB so that a permission revoked a
       moment ago takes effect on the next request rather than at token expiry.
       The short TTL (§23) bounds the window either way; this closes it further for
       everything except the token's own claims, which `decode_access_token` has
       already verified cryptographically.
    2. **The trusted context is enriched** with the verified identity, so anything
       downstream (audit, agent runtime, tool gateway) sees who is really calling.
       The values come from the database, never from the request body - which is
       what makes §70's "the model cannot forge this" true rather than aspirational.
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("an access token is required")

    claims = decode_access_token(credentials.credentials, settings=settings)

    auth_service = AuthService(session, settings)
    principal = auth_service.principal_for_user(claims.subject, session_id=claims.session_id)

    # A session revoked between issue and use must not keep working.
    from app.modules.identity.repository import AuthSessionRepository

    auth_session = AuthSessionRepository(session).get_by_session_id(claims.session_id)
    if auth_session is None or auth_session.revoked_at is not None:
        from app.core.errors import SessionRevokedError

        raise SessionRevokedError("this session is no longer valid")
    if auth_session.user_id != principal.user_id:
        # The token's subject and its session disagree - a forged or mismatched
        # pair. Treated as an authentication failure, not a permission one.
        raise AuthenticationError("token and session do not match")

    ctx = get_context()
    set_context(
        RequestContext(
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            source="HTTP",
            actor_type="STAFF" if principal.is_staff else "USER",
            actor_id=principal.user_id,
            user_id=principal.user_id,
            merchant_id=principal.merchant_id,
            roles=principal.roles,
            permissions=principal.permissions,
            data_scope=principal.data_scope.value,
            started_at=ctx.started_at,
        )
    )
    _ = request  # the Request is injected for future use (client ip, headers)
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def require_permission(*permissions: str) -> Callable[[Principal], Principal]:
    """Dependency factory: the caller must hold **at least one** of ``permissions``.

    "At least one" rather than "all", because the parameter is a list of acceptable
    routes to the same capability (e.g. `order:admin` or `order:read`). Passing
    several permissions and requiring all of them would make the call site read
    like an AND when the intent is an OR, which is a subtle way to lock out a
    legitimate role.
    """

    def _dependency(principal: CurrentPrincipal) -> Principal:
        if not principal.permissions:
            raise PermissionDeniedError("this account has no permissions")
        if not principal.has_any_permission(*permissions):
            raise PermissionDeniedError(
                "missing a required permission",
                context={"required_any_of": list(permissions)},
            )
        return principal

    return _dependency


def require_staff(principal: CurrentPrincipal) -> Principal:
    """Console endpoints are for staff; a consumer token must not reach them.

    Note this is *not* the same as a permission check. A consumer could in
    principle be granted a permission by mistake; requiring the account type as
    well means one misconfigured role cannot expose the back office.
    """
    if not principal.is_staff:
        raise PermissionDeniedError("this endpoint is for merchant staff")
    if principal.merchant_id is None:
        # INV-010/INV-012 both depend on a staff principal being attributable to a
        # merchant. A staff account without one cannot be scoped, so it is refused
        # rather than treated as unscoped.
        raise PermissionDeniedError("this staff account is not attached to a merchant")
    return principal


StaffPrincipal = Annotated[Principal, Depends(require_staff)]


def require_merchant_scope(principal: StaffPrincipal) -> Principal:
    """Require a scope broad enough to be useful for back-office reads.

    ``SELF`` is refused: a staff member whose scope resolves to their own records
    cannot meaningfully operate a merchant console, and silently returning an empty
    list would look like "there is no data" rather than "you are misconfigured".
    """
    if principal.data_scope in {DataScope.NONE, DataScope.SELF}:
        raise PermissionDeniedError(
            "this account's data scope is too narrow for the console",
            context={"data_scope": principal.data_scope.value},
        )
    return principal


ConsolePrincipal = Annotated[Principal, Depends(require_merchant_scope)]


__all__ = [
    "ConsolePrincipal",
    "CurrentPrincipal",
    "PermissionCode",
    "StaffPrincipal",
    "get_app_settings",
    "get_current_principal",
    "require_merchant_scope",
    "require_permission",
    "require_staff",
]
