"""Shared FastAPI dependencies: session, current user, permission guards.

This is enforcement layer 1 of the three described in SECURITY.md section 4.
Layer 2 lives in services (ownership and row-level rules); layer 3 is the
serializer redaction in `app.core.redaction`.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ForbiddenError, UnauthenticatedError
from app.core.logging import user_id_ctx
from app.core.permissions import Permission, Role, permissions_for
from app.core.security import decode_access_token
from app.db.session import get_session
from app.models.identity import User
from app.repositories.identity import UserRepository

# auto_error=False so a missing header produces our error envelope rather than
# FastAPI's default {"detail": ...} shape.
bearer_scheme = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def client_ip(request: Request) -> str | None:
    """Client address, honouring one proxy hop.

    Only the first entry of X-Forwarded-For is trusted, and only because the
    deployment terminates at a single Nginx hop we control.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def user_agent(request: Request) -> str | None:
    return (request.headers.get("user-agent") or "")[:255] or None


async def get_current_user(
    request: Request,
    session: SessionDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    if credentials is None or not credentials.credentials:
        raise UnauthenticatedError(log_detail="missing bearer token")

    claims = decode_access_token(credentials.credentials)

    user = await UserRepository(session).get(claims.user_id)
    if user is None:
        raise UnauthenticatedError(log_detail=f"token references unknown user {claims.user_id}")
    if not user.is_active:
        raise UnauthenticatedError(
            "This account has been deactivated.",
            log_detail=f"deactivated user {user.email} presented a valid token",
        )
    # A role change must take effect immediately, not when the access token
    # happens to expire, so the database role wins over the token claim.
    if user.role != claims.role:
        raise UnauthenticatedError(
            "Your access has changed. Please sign in again.",
            log_detail=f"role mismatch: token={claims.role} db={user.role}",
        )

    request.state.current_user = user
    user_id_ctx.set(str(user.id))
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_active_user(user: CurrentUser) -> User:
    """Current user who is not blocked behind a forced password change.

    Used by every business endpoint. `/auth/me`, `/auth/change-password` and
    `/auth/logout` deliberately use `CurrentUser` instead, so a user who must
    change their password can still complete that flow.
    """
    if user.must_change_password:
        raise ForbiddenError(
            "You must change your password before continuing.",
            log_detail="password change required",
        )
    return user


ActiveUser = Annotated[User, Depends(get_current_active_user)]


def require(
    *permissions: Permission,
    require_all: bool = True,
) -> Callable[..., Coroutine[Any, Any, User]]:
    """Dependency factory guarding an endpoint with one or more permissions.

    @router.post("/", dependencies=[Depends(require(Permission.USER_CREATE))])
    """

    async def guard(user: ActiveUser) -> User:
        granted = permissions_for(user.role)
        ok = (
            all(permission in granted for permission in permissions)
            if require_all
            else any(permission in granted for permission in permissions)
        )
        if not ok:
            missing = [p.value for p in permissions if p not in granted]
            raise ForbiddenError(
                log_detail=f"role={user.role.value} missing={missing}",
            )
        return user

    return guard


def require_role(*roles: Role) -> Callable[..., Coroutine[Any, Any, User]]:
    """Role guard for the few places a permission is the wrong abstraction."""

    async def guard(user: ActiveUser) -> User:
        if user.role not in roles:
            raise ForbiddenError(
                log_detail=f"role={user.role.value} not in {[r.value for r in roles]}"
            )
        return user

    return guard


def user_permissions(user: User) -> list[str]:
    return sorted(permission.value for permission in permissions_for(user.role))


__all__ = [
    "ActiveUser",
    "CurrentUser",
    "SessionDep",
    "client_ip",
    "get_current_active_user",
    "get_current_user",
    "require",
    "require_role",
    "user_agent",
    "user_permissions",
]
