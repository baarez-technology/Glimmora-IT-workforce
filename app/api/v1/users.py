"""User administration endpoints (ADMIN, with read for MANAGEMENT)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.core.deps import ActiveUser, SessionDep, require
from app.core.pagination import Page, PageParams, page_params
from app.core.permissions import Permission, Role
from app.models.identity import User
from app.schemas.identity import (
    UserCreateRequest,
    UserResetPasswordRequest,
    UserResponse,
    UserUpdateRequest,
)
from app.services.user import UserService

router = APIRouter(prefix="/users", tags=["users"])


def _to_response(user: User) -> UserResponse:
    response = UserResponse.model_validate(user)
    response.is_locked = user.locked_until is not None and user.locked_until > user.created_at
    return response


@router.get("", response_model=Page[UserResponse], summary="List users")
async def list_users(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.USER_READ))],
    params: Annotated[PageParams, Depends(page_params)],
    role: Annotated[Role | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[UserResponse]:
    users, total = await UserService(session).list_users(params, role=role, is_active=is_active)
    return Page.build([_to_response(user) for user in users], total, params)


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user",
)
async def create_user(
    payload: UserCreateRequest,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.USER_CREATE))],
) -> UserResponse:
    user = await UserService(session).create_user(payload, actor=actor)
    return _to_response(user)


@router.get("/{user_id}", response_model=UserResponse, summary="Get a user")
async def get_user(
    user_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.USER_READ))],
) -> UserResponse:
    return _to_response(await UserService(session).get_user(user_id))


@router.patch("/{user_id}", response_model=UserResponse, summary="Update a user")
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.USER_UPDATE))],
) -> UserResponse:
    return _to_response(await UserService(session).update_user(user_id, payload, actor=actor))


@router.post("/{user_id}/deactivate", response_model=UserResponse, summary="Deactivate a user")
async def deactivate_user(
    user_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.USER_DEACTIVATE))],
) -> UserResponse:
    return _to_response(await UserService(session).deactivate_user(user_id, actor=actor))


@router.post(
    "/{user_id}/reset-password",
    response_model=UserResponse,
    summary="Reset another user's password",
)
async def reset_password(
    user_id: uuid.UUID,
    payload: UserResetPasswordRequest,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.USER_UPDATE))],
) -> UserResponse:
    return _to_response(await UserService(session).reset_password(user_id, payload, actor=actor))


@router.get("/me/profile", response_model=UserResponse, include_in_schema=False)
async def my_profile(user: ActiveUser) -> UserResponse:
    return _to_response(user)


__all__ = ["router"]
