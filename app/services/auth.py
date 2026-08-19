"""Authentication service: login, refresh rotation, logout, password change."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import UnauthenticatedError, ValidationError
from app.core.logging import get_logger, log_business_event
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    password_needs_rehash,
    validate_password_strength,
    verify_password,
)
from app.db.session import SessionFactory
from app.models.identity import AuditAction, LoginAttempt, RefreshToken, User
from app.repositories.identity import (
    LoginAttemptRepository,
    RefreshTokenRepository,
    UserRepository,
)
from app.services.audit import AuditService

logger = get_logger("auth")

# Verified against this when the email is unknown, so a missing account and a
# wrong password take the same time and reveal nothing (SECURITY.md section 1).
_DUMMY_HASH = hash_password("glimmora-timing-equaliser-not-a-real-password")

_GENERIC_LOGIN_FAILURE = "Those sign-in details are not correct."


@dataclass(frozen=True, slots=True)
class IssuedSession:
    user: User
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepository(session)
        self.tokens = RefreshTokenRepository(session)
        self.attempts = LoginAttemptRepository(session)
        self.audit = AuditService(session)

    # ------------------------------------------------------------- login
    async def login(
        self,
        *,
        email: str,
        password: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        email = email.strip().lower()
        user = await self.users.get_by_email(email)

        await self._assert_not_locked_out(email, user)

        # Always run a verification so the response time does not distinguish
        # "no such user" from "wrong password".
        password_ok = (
            verify_password(password, user.hashed_password)
            if user
            else verify_password(password, _DUMMY_HASH)
        )

        if user is None or not password_ok:
            await self._record_failure(email, user, ip_address, user_agent)
            raise UnauthenticatedError(
                _GENERIC_LOGIN_FAILURE,
                log_detail=f"failed login for {email} (user_exists={user is not None})",
            )

        if not user.is_active:
            await self._record_failure(email, user, ip_address, user_agent)
            raise UnauthenticatedError(
                "This account has been deactivated. Contact an administrator.",
                log_detail=f"login attempt on deactivated account {email}",
            )

        # Transparent upgrade if the hashing cost parameters have moved on.
        if password_needs_rehash(user.hashed_password):
            user.hashed_password = hash_password(password)

        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = datetime.now(UTC)

        await self.attempts.add(
            self.attempts.model(
                email=email,
                ip_address=ip_address,
                succeeded=True,
                user_agent=(user_agent or "")[:255] or None,
            )
        )
        await self.audit.record(
            AuditAction.LOGIN,
            summary=f"{user.email} signed in",
            actor=user,
            entity_type="user",
            entity_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        log_business_event("user_login", user_id=str(user.id), role=user.role.value)

        return await self._issue_session(user, ip_address=ip_address, user_agent=user_agent)

    # ----------------------------------------------------------- refresh
    async def refresh(
        self,
        *,
        raw_token: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        stored = await self.tokens.get_by_hash(hash_refresh_token(raw_token))

        if stored is None:
            raise UnauthenticatedError(
                "Your session has expired. Please sign in again.",
                log_detail="refresh token not recognised",
            )

        if stored.revoked_at is not None:
            # A revoked token being presented means the cookie leaked: the
            # legitimate client already rotated past it. Kill the whole family.
            revoked = await self.tokens.revoke_family(stored.family_id)
            logger.warning(
                "refresh_token_reuse_detected",
                user_id=str(stored.user_id),
                family_id=str(stored.family_id),
                revoked_count=revoked,
            )
            await self.audit.record(
                AuditAction.LOGOUT,
                summary="Refresh token reuse detected — all sessions revoked",
                actor=stored.user,
                entity_type="user",
                entity_id=stored.user_id,
                ip_address=ip_address,
            )
            raise UnauthenticatedError(
                "Your session is no longer valid. Please sign in again.",
                log_detail="refresh token reuse",
            )

        if stored.expires_at <= datetime.now(UTC):
            raise UnauthenticatedError(
                "Your session has expired. Please sign in again.",
                log_detail="refresh token expired",
            )

        user = stored.user
        if user is None or not user.is_active:
            await self.tokens.revoke_family(stored.family_id)
            raise UnauthenticatedError(
                "This account is no longer active.",
                log_detail="refresh for inactive user",
            )

        return await self._issue_session(
            user,
            family_id=stored.family_id,
            rotates=stored,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    # ------------------------------------------------------------ logout
    async def logout(self, *, raw_token: str | None, user: User | None = None) -> None:
        if raw_token:
            stored = await self.tokens.get_by_hash(hash_refresh_token(raw_token))
            if stored is not None:
                await self.tokens.revoke_family(stored.family_id)
                user = user or stored.user
        if user is not None:
            await self.audit.record(
                AuditAction.LOGOUT,
                summary=f"{user.email} signed out",
                actor=user,
                entity_type="user",
                entity_id=user.id,
            )

    # --------------------------------------------------- change password
    async def change_password(
        self,
        *,
        user: User,
        current_password: str,
        new_password: str,
        ip_address: str | None = None,
    ) -> None:
        if not verify_password(current_password, user.hashed_password):
            raise ValidationError(
                "Your current password is not correct.",
                details=[{"field": "current_password", "message": "Incorrect password"}],
            )
        if verify_password(new_password, user.hashed_password):
            raise ValidationError(
                "Choose a password you have not used before.",
                details=[
                    {"field": "new_password", "message": "Must differ from the current password"}
                ],
            )

        validate_password_strength(new_password, email=user.email)

        user.hashed_password = hash_password(new_password)
        user.must_change_password = False

        # A password change invalidates every existing session, everywhere.
        await self.tokens.revoke_all_for_user(user.id)

        await self.audit.record(
            AuditAction.PASSWORD_CHANGED,
            summary=f"{user.email} changed their password",
            actor=user,
            entity_type="user",
            entity_id=user.id,
            ip_address=ip_address,
        )

    # ----------------------------------------------------------- helpers
    async def _issue_session(
        self,
        user: User,
        *,
        family_id: uuid.UUID | None = None,
        rotates: RefreshToken | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> IssuedSession:
        access_token, access_expires = create_access_token(user_id=user.id, role=user.role)
        raw_refresh, refresh_hash, refresh_expires = generate_refresh_token()

        issued = RefreshToken(
            user_id=user.id,
            token_hash=refresh_hash,
            family_id=family_id or uuid.uuid4(),
            expires_at=refresh_expires,
            user_agent=(user_agent or "")[:255] or None,
            ip_address=ip_address,
        )
        await self.tokens.add(issued)

        if rotates is not None:
            rotates.revoked_at = datetime.now(UTC)
            rotates.replaced_by_id = issued.id

        return IssuedSession(
            user=user,
            access_token=access_token,
            access_expires_at=access_expires,
            refresh_token=raw_refresh,
            refresh_expires_at=refresh_expires,
        )

    async def _assert_not_locked_out(self, email: str, user: User | None) -> None:
        now = datetime.now(UTC)

        if user is not None and user.locked_until and user.locked_until > now:
            minutes = max(1, int((user.locked_until - now).total_seconds() // 60) + 1)
            raise UnauthenticatedError(
                f"Too many failed attempts. Try again in {minutes} minutes.",
                log_detail=f"account locked until {user.locked_until.isoformat()}",
            )

        # Counted per email even when no such user exists, so probing an unknown
        # address is throttled the same way.
        failures = await self.attempts.count_recent_failures(
            email, minutes=settings.LOGIN_LOCKOUT_MINUTES
        )
        if failures >= settings.LOGIN_MAX_ATTEMPTS:
            raise UnauthenticatedError(
                f"Too many failed attempts. Try again in {settings.LOGIN_LOCKOUT_MINUTES} minutes.",
                log_detail=f"email throttled after {failures} failures",
            )

    async def _record_failure(
        self,
        email: str,
        user: User | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        """Persist the failure in its own transaction.

        A failed login raises, and the request session is rolled back by the
        session dependency — which would discard the very rows that make lockout
        work. The security ledger therefore commits independently of the request
        that produced it.
        """
        async with SessionFactory() as ledger:
            ledger.add(
                LoginAttempt(
                    email=email,
                    ip_address=ip_address,
                    succeeded=False,
                    user_agent=(user_agent or "")[:255] or None,
                )
            )

            locked = False
            if user is not None:
                fresh = await UserRepository(ledger).get(user.id)
                if fresh is not None:
                    fresh.failed_login_count += 1
                    if fresh.failed_login_count >= settings.LOGIN_MAX_ATTEMPTS:
                        fresh.locked_until = datetime.now(UTC) + timedelta(
                            minutes=settings.LOGIN_LOCKOUT_MINUTES
                        )
                        locked = True

            await AuditService(ledger).record(
                AuditAction.LOGIN_FAILED,
                summary=f"Failed sign-in attempt for {email}",
                actor=None,
                actor_email=email,
                entity_type="user",
                entity_id=user.id if user else None,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            await ledger.commit()

        if locked and user is not None:
            logger.warning("account_locked", user_id=str(user.id))


__all__ = ["AuthService", "IssuedSession"]
