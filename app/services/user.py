"""User administration service."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.core.pagination import PageParams
from app.core.permissions import Role
from app.core.security import hash_password, validate_password_strength
from app.models.identity import AuditAction, User
from app.repositories.identity import RefreshTokenRepository, UserRepository
from app.schemas.identity import UserCreateRequest, UserResetPasswordRequest, UserUpdateRequest
from app.services.audit import AuditService, build_diff

#: Fields whose before/after values are worth keeping in the audit trail.
AUDITED_USER_FIELDS = {"email", "full_name", "role", "job_title", "is_active"}


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.tokens = RefreshTokenRepository(session)
        self.audit = AuditService(session)

    async def list_users(
        self,
        params: PageParams,
        *,
        role: Role | None = None,
        is_active: bool | None = None,
    ) -> tuple[list[User], int]:
        return await self.users.list_users(params, role=role, is_active=is_active)

    async def get_user(self, user_id: uuid.UUID) -> User:
        user = await self.users.get(user_id)
        if user is None:
            raise NotFoundError("user", user_id)
        return user

    async def create_user(self, payload: UserCreateRequest, *, actor: User) -> User:
        if await self.users.email_taken(payload.email):
            raise ConflictError(
                "A user with that email address already exists.",
                details=[{"field": "email", "message": "Already in use"}],
            )

        validate_password_strength(payload.password, email=payload.email)

        user = User(
            email=payload.email,
            full_name=payload.full_name,
            hashed_password=hash_password(payload.password),
            role=payload.role,
            job_title=payload.job_title,
            is_active=True,
            must_change_password=payload.must_change_password,
        )
        await self.users.add(user)

        await self.audit.record(
            AuditAction.USER_CREATED,
            summary=f"Created user {user.email} with role {user.role.value}",
            actor=actor,
            entity_type="user",
            entity_id=user.id,
            changes={"role": {"from": None, "to": user.role.value}},
        )
        return user

    async def update_user(
        self, user_id: uuid.UUID, payload: UserUpdateRequest, *, actor: User
    ) -> User:
        user = await self.get_user(user_id)
        before = user.to_dict()

        updates = payload.model_dump(exclude_unset=True)
        role_changed = "role" in updates and updates["role"] != user.role
        deactivating = updates.get("is_active") is False and user.is_active

        # Locking every administrator out of the platform is unrecoverable
        # without database access, so the last active admin is protected.
        if role_changed or deactivating:
            await self._assert_not_last_admin(
                user, new_role=updates.get("role"), deactivating=deactivating
            )

        if deactivating and user.id == actor.id:
            raise ValidationError(
                "You cannot deactivate your own account.",
                details=[{"field": "is_active", "message": "Ask another administrator"}],
            )

        for field, value in updates.items():
            setattr(user, field, value)

        if deactivating:
            await self.tokens.revoke_all_for_user(user.id)

        changes = build_diff(before, user.to_dict(), fields=AUDITED_USER_FIELDS)

        await self.audit.record(
            AuditAction.PERMISSION_CHANGED if role_changed else AuditAction.USER_UPDATED,
            summary=(
                f"Changed role of {user.email} to {user.role.value}"
                if role_changed
                else f"Updated user {user.email}"
            ),
            actor=actor,
            entity_type="user",
            entity_id=user.id,
            changes=changes,
        )
        return user

    async def deactivate_user(self, user_id: uuid.UUID, *, actor: User) -> User:
        user = await self.get_user(user_id)

        if user.id == actor.id:
            raise ValidationError(
                "You cannot deactivate your own account.",
                details=[{"field": "id", "message": "Ask another administrator"}],
            )
        if not user.is_active:
            return user

        await self._assert_not_last_admin(user, deactivating=True)

        user.is_active = False
        revoked = await self.tokens.revoke_all_for_user(user.id)

        await self.audit.record(
            AuditAction.USER_DEACTIVATED,
            summary=f"Deactivated {user.email} ({revoked} sessions revoked)",
            actor=actor,
            entity_type="user",
            entity_id=user.id,
            changes={"is_active": {"from": True, "to": False}},
        )
        return user

    async def reset_password(
        self, user_id: uuid.UUID, payload: UserResetPasswordRequest, *, actor: User
    ) -> User:
        user = await self.get_user(user_id)
        validate_password_strength(payload.new_password, email=user.email)

        user.hashed_password = hash_password(payload.new_password)
        user.must_change_password = payload.must_change_password
        user.failed_login_count = 0
        user.locked_until = None
        await self.tokens.revoke_all_for_user(user.id)

        await self.audit.record(
            AuditAction.PASSWORD_CHANGED,
            summary=f"{actor.email} reset the password for {user.email}",
            actor=actor,
            entity_type="user",
            entity_id=user.id,
        )
        return user

    async def _assert_not_last_admin(
        self,
        user: User,
        *,
        new_role: Role | None = None,
        deactivating: bool = False,
    ) -> None:
        losing_admin = user.role == Role.ADMIN and (
            deactivating or (new_role is not None and new_role != Role.ADMIN)
        )
        if not losing_admin:
            return

        remaining = await self.users.count_active_admins(exclude_id=user.id)
        if remaining == 0:
            raise ForbiddenError(
                "This is the only active administrator. Promote another user first.",
                log_detail=f"blocked removal of last admin {user.email}",
            )


__all__ = ["AUDITED_USER_FIELDS", "UserService"]
