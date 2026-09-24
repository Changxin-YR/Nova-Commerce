"""Authenticated profile and permission hints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.errors import AuthenticationError, envelope
from app.core.redaction import mask_email, mask_phone
from app.modules.identity.dependencies import CurrentPrincipal
from app.modules.identity.models import User
from app.modules.identity.repository import UserRepository
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]


def profile_data(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": mask_email(user.email) if user.email else None,
        "phone": mask_phone(user.phone) if user.phone else None,
        "roles": list(user.role_codes),
        "merchant_id": user.merchant_id,
    }


@router.get("/users/me", summary="Current account")
def me(principal: CurrentPrincipal, session: SessionDep) -> dict:
    user = UserRepository(session).get_with_authorization(principal.user_id)
    if user is None:
        raise AuthenticationError("account no longer exists")
    return envelope(data=profile_data(user))


@router.get("/users/me/permissions", summary="Current permission hints")
def my_permissions(principal: CurrentPrincipal) -> dict:
    return envelope(data={"roles": list(principal.roles), "permissions": sorted(principal.permissions)})
