"""Auth, user, role and audit schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.permissions import Role
from app.models.identity import AuditAction

# --------------------------------------------------------------------- auth


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return value.strip().lower()


class TokenResponse(BaseModel):
    """The refresh token is NOT in this payload — it goes out as an httpOnly cookie."""

    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: CurrentUserResponse


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class CurrentUserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    job_title: str | None = None
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None = None
    permissions: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------- users


class UserCreateRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=160)
    role: Role
    password: str = Field(min_length=1, max_length=256)
    job_title: str | None = Field(default=None, max_length=120)
    must_change_password: bool = True

    @field_validator("email")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("full_name")
    @classmethod
    def _trim(cls, value: str) -> str:
        return value.strip()


class UserUpdateRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    role: Role | None = None
    job_title: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None


class UserResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=1, max_length=256)
    must_change_password: bool = True


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    role: Role
    job_title: str | None
    is_active: bool
    must_change_password: bool
    last_login_at: datetime | None
    is_locked: bool = False
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------- roles


class RoleResponse(BaseModel):
    role: Role
    label: str
    description: str
    permission_count: int
    permissions: list[str]


class PermissionMatrixRow(BaseModel):
    permission: str
    is_field_permission: bool
    roles: dict[str, bool]


class RoleCatalogueResponse(BaseModel):
    roles: list[RoleResponse]
    matrix: list[PermissionMatrixRow]


# --------------------------------------------------------------------- audit


class AuditLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID | None
    actor_email: str | None
    action: AuditAction
    entity_type: str | None
    entity_id: uuid.UUID | None
    summary: str
    changes: dict[str, Any] | None
    ip_address: str | None
    request_id: str | None
    created_at: datetime


__all__ = [
    "AuditLogResponse",
    "ChangePasswordRequest",
    "CurrentUserResponse",
    "LoginRequest",
    "PermissionMatrixRow",
    "RoleCatalogueResponse",
    "RoleResponse",
    "TokenResponse",
    "UserCreateRequest",
    "UserResetPasswordRequest",
    "UserResponse",
    "UserUpdateRequest",
]
