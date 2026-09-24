"""Cookie-backed authentication routes for the existing session service."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import AuthenticationError, PermissionDeniedError, TokenInvalidError, envelope
from app.modules.identity.api.users import profile_data
from app.modules.identity.repository import UserRepository
from app.modules.identity.service import AuthService, IssuedSession
from app.shared.db.session import get_session

router = APIRouter()
SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class LoginBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


def _cookie(response: Response, settings: Settings, refresh_token: str) -> None:
    response.set_cookie(
        key=settings.REFRESH_COOKIE_NAME,
        value=refresh_token,
        max_age=settings.REFRESH_TOKEN_TTL_SECONDS,
        httponly=True,
        secure=settings.REFRESH_COOKIE_SECURE,
        samesite=settings.REFRESH_COOKIE_SAMESITE,
        path=settings.REFRESH_COOKIE_PATH,
    )


def _check_cookie_origin(request: Request, settings: Settings) -> None:
    origin = request.headers.get("origin")
    if origin is None:
        return
    own_origin = str(request.base_url).rstrip("/")
    if origin != own_origin and origin not in settings.CORS_ALLOW_ORIGINS:
        raise PermissionDeniedError("refresh cookie origin is not allowed")


def _token_data(issued: IssuedSession, session: Session, settings: Settings) -> dict:
    user = UserRepository(session).get_with_authorization(issued.session.user_id)
    if user is None:
        raise AuthenticationError("account no longer exists")
    return {
        "access_token": issued.access_token,
        "token_type": "Bearer",
        "expires_in": settings.ACCESS_TOKEN_TTL_SECONDS,
        "user": profile_data(user),
    }


@router.post("/login", summary="Open a session")
def login(body: LoginBody, request: Request, response: Response, session: SessionDep, settings: SettingsDep) -> dict:
    issued = AuthService(session, settings).login(
        identifier=body.username,
        password=body.password,
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    data = _token_data(issued, session, settings)
    session.commit()
    _cookie(response, settings, issued.refresh_token)
    return envelope(data=data)


@router.post("/refresh", summary="Rotate the refresh cookie")
def refresh(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
) -> dict:
    _check_cookie_origin(request, settings)
    token = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    if not token:
        raise TokenInvalidError("refresh cookie is missing")
    issued = AuthService(session, settings).rotate(
        presented_refresh_token=token,
        client_ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    data = _token_data(issued, session, settings)
    session.commit()
    _cookie(response, settings, issued.refresh_token)
    return envelope(data=data)


@router.post("/logout", summary="End the current refresh-token family")
def logout(request: Request, response: Response, session: SessionDep, settings: SettingsDep) -> dict:
    _check_cookie_origin(request, settings)
    token = request.cookies.get(settings.REFRESH_COOKIE_NAME, "")
    revoked = AuthService(session, settings).logout(refresh_token=token)
    session.commit()
    response.delete_cookie(
        settings.REFRESH_COOKIE_NAME,
        path=settings.REFRESH_COOKIE_PATH,
        secure=settings.REFRESH_COOKIE_SECURE,
        httponly=True,
        samesite=settings.REFRESH_COOKIE_SAMESITE,
    )
    return envelope(data={"revoked_sessions": revoked})
