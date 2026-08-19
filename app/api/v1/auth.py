"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.config import settings
from app.core.deps import CurrentUser, SessionDep, client_ip, user_agent, user_permissions
from app.core.errors import UnauthenticatedError
from app.core.rate_limit import rate_limit
from app.schemas.identity import (
    ChangePasswordRequest,
    CurrentUserResponse,
    LoginRequest,
    TokenResponse,
)
from app.services.auth import AuthService, IssuedSession

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "glimmora_refresh"
# Scoped to the auth routes so the cookie is not attached to every API call.
REFRESH_COOKIE_PATH = f"{settings.API_V1_PREFIX}/auth"


def _set_refresh_cookie(response: Response, session: IssuedSession) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=session.refresh_token,
        httponly=True,
        secure=settings.is_production,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
        max_age=settings.REFRESH_TOKEN_DAYS * 24 * 3600,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)


def _token_response(session: IssuedSession) -> TokenResponse:
    user = session.user
    return TokenResponse(
        access_token=session.access_token,
        expires_at=session.access_expires_at,
        user=CurrentUserResponse(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            job_title=user.job_title,
            is_active=user.is_active,
            must_change_password=user.must_change_password,
            last_login_at=user.last_login_at,
            permissions=user_permissions(user),
        ),
    )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Sign in",
    dependencies=[Depends(rate_limit("login", limit_setting="RATE_LIMIT_LOGIN_PER_MINUTE"))],
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
) -> TokenResponse:
    issued = await AuthService(session).login(
        email=payload.email,
        password=payload.password,
        ip_address=client_ip(request),
        user_agent=user_agent(request),
    )
    _set_refresh_cookie(response, issued)
    return _token_response(issued)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate the session",
    dependencies=[Depends(rate_limit("refresh", limit=60))],
)
async def refresh(
    request: Request,
    response: Response,
    session: SessionDep,
) -> TokenResponse:
    raw_token = request.cookies.get(REFRESH_COOKIE)
    if not raw_token:
        raise UnauthenticatedError(
            "Your session has expired. Please sign in again.",
            log_detail="refresh called without a cookie",
        )

    try:
        issued = await AuthService(session).refresh(
            raw_token=raw_token,
            ip_address=client_ip(request),
            user_agent=user_agent(request),
        )
    except UnauthenticatedError:
        # A dead cookie should not keep being replayed by the browser.
        _clear_refresh_cookie(response)
        raise

    _set_refresh_cookie(response, issued)
    return _token_response(issued)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Sign out")
async def logout(request: Request, response: Response, session: SessionDep) -> None:
    await AuthService(session).logout(
        raw_token=request.cookies.get(REFRESH_COOKIE),
        user=getattr(request.state, "current_user", None),
    )
    _clear_refresh_cookie(response)


@router.get("/me", response_model=CurrentUserResponse, summary="Current user and permissions")
async def me(user: CurrentUser) -> CurrentUserResponse:
    return CurrentUserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        role=user.role,
        job_title=user.job_title,
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        last_login_at=user.last_login_at,
        permissions=user_permissions(user),
    )


@router.post(
    "/change-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change your own password",
)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    user: CurrentUser,
) -> None:
    await AuthService(session).change_password(
        user=user,
        current_password=payload.current_password,
        new_password=payload.new_password,
        ip_address=client_ip(request),
    )
    # Every session was revoked, including this one, so the cookie must go too.
    _clear_refresh_cookie(response)


__all__ = ["REFRESH_COOKIE", "REFRESH_COOKIE_PATH", "router"]
