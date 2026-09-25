"""Authenticated profile and permission hints.

Both handlers are reads with no transaction of their own: the account is loaded
by ``AuthService.user_profile`` and the permission hints come straight off the
already-resolved principal.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import envelope
from app.modules.identity.dependencies import CurrentPrincipal
from app.modules.identity.schemas import profile_data
from app.modules.identity.service import AuthService
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/users/me", summary="Current account")
def me(
    principal: CurrentPrincipal,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    user = AuthService(session, settings).user_profile(principal.user_id)
    return envelope(data=profile_data(user))


@router.get("/users/me/permissions", summary="Current permission hints")
def my_permissions(principal: CurrentPrincipal) -> dict:
    return envelope(data={"roles": list(principal.roles), "permissions": sorted(principal.permissions)})
