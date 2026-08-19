"""Data access for users, sessions, audit and login attempts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.orm import selectinload

from app.core.pagination import PageParams, apply_sort, paginate
from app.core.permissions import Role
from app.models.identity import AuditAction, AuditLog, LoginAttempt, RefreshToken, User
from app.repositories.base import BaseRepository

USER_SORT_FIELDS = {"email", "full_name", "role", "is_active", "created_at", "last_login_at"}
AUDIT_SORT_FIELDS = {"created_at", "action", "entity_type"}


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(func.lower(User.email) == email.strip().lower())
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def email_taken(self, email: str, *, exclude_id: uuid.UUID | None = None) -> bool:
        stmt = select(User.id).where(func.lower(User.email) == email.strip().lower())
        if exclude_id is not None:
            stmt = stmt.where(User.id != exclude_id)
        return (await self.session.execute(stmt)).first() is not None

    async def count_active_admins(self, *, exclude_id: uuid.UUID | None = None) -> int:
        stmt = select(func.count()).where(User.role == Role.ADMIN, User.is_active.is_(True))
        if exclude_id is not None:
            stmt = stmt.where(User.id != exclude_id)
        return int((await self.session.execute(stmt)).scalar_one())

    async def list_users(
        self,
        params: PageParams,
        *,
        role: Role | None = None,
        is_active: bool | None = None,
    ) -> tuple[list[User], int]:
        stmt: Select[Any] = select(User)
        if role is not None:
            stmt = stmt.where(User.role == role)
        if is_active is not None:
            stmt = stmt.where(User.is_active.is_(is_active))
        if params.q:
            needle = f"%{params.q.strip().lower()}%"
            stmt = stmt.where(
                or_(func.lower(User.email).like(needle), func.lower(User.full_name).like(needle))
            )
        stmt = apply_sort(stmt, User, params, USER_SORT_FIELDS)
        if params.sort_spec() is None:
            stmt = stmt.order_by(User.full_name.asc())
        return await paginate(self.session, stmt, params)


class RefreshTokenRepository(BaseRepository[RefreshToken]):
    model = RefreshToken

    async def get_by_hash(self, token_hash: str) -> RefreshToken | None:
        """Load the token with its user — the refresh path always needs both."""
        stmt = (
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .options(selectinload(RefreshToken.user))
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def revoke_family(self, family_id: uuid.UUID) -> int:
        """Revoke every token in a family — the response to a replayed token."""
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        result = await self.session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        stmt = (
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        result = await self.session.execute(stmt)
        return int(getattr(result, "rowcount", 0) or 0)

    async def purge_expired(self, *, older_than_days: int = 30) -> int:
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        stmt = select(RefreshToken).where(RefreshToken.expires_at < cutoff)
        rows = list((await self.session.execute(stmt)).scalars().all())
        for row in rows:
            await self.session.delete(row)
        return len(rows)


class AuditRepository(BaseRepository[AuditLog]):
    """Append-only: this repository intentionally exposes no update or delete."""

    model = AuditLog

    async def list_logs(
        self,
        params: PageParams,
        *,
        user_id: uuid.UUID | None = None,
        action: AuditAction | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> tuple[list[AuditLog], int]:
        stmt: Select[Any] = select(AuditLog)
        if user_id is not None:
            stmt = stmt.where(AuditLog.user_id == user_id)
        if action is not None:
            stmt = stmt.where(AuditLog.action == action)
        if entity_type is not None:
            stmt = stmt.where(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            stmt = stmt.where(AuditLog.entity_id == entity_id)
        if date_from is not None:
            stmt = stmt.where(AuditLog.created_at >= date_from)
        if date_to is not None:
            stmt = stmt.where(AuditLog.created_at <= date_to)
        if params.q:
            needle = f"%{params.q.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(AuditLog.summary).like(needle),
                    func.lower(AuditLog.actor_email).like(needle),
                )
            )
        stmt = apply_sort(stmt, AuditLog, params, AUDIT_SORT_FIELDS)
        if params.sort_spec() is None:
            stmt = stmt.order_by(AuditLog.created_at.desc())
        return await paginate(self.session, stmt, params)


class LoginAttemptRepository(BaseRepository[LoginAttempt]):
    model = LoginAttempt

    async def count_recent_failures(self, email: str, *, minutes: int) -> int:
        cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
        stmt = select(func.count()).where(
            func.lower(LoginAttempt.email) == email.strip().lower(),
            LoginAttempt.succeeded.is_(False),
            LoginAttempt.created_at >= cutoff,
        )
        return int((await self.session.execute(stmt)).scalar_one())


__all__ = [
    "AUDIT_SORT_FIELDS",
    "USER_SORT_FIELDS",
    "AuditRepository",
    "LoginAttemptRepository",
    "RefreshTokenRepository",
    "UserRepository",
]
