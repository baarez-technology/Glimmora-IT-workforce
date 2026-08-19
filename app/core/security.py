"""Password hashing, JWT access tokens and opaque refresh tokens.

SECURITY.md section 1. Argon2id for passwords, short-lived JWTs for access, and
rotating opaque refresh tokens stored only as a SHA-256 hash — a database dump
must not hand anyone a usable session.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import settings
from app.core.errors import UnauthenticatedError, ValidationError
from app.core.permissions import Role

# Defaults are argon2-cffi's RFC 9106 low-memory profile: a good balance for an
# internal tool on modest hardware.
_hasher = PasswordHasher()

# Rejected outright regardless of length — an internal platform is still phished.
_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "passw0rd123",
        "administrator",
        "qwertyuiop123",
        "letmein12345",
        "welcome12345",
        "glimmora1234",
        "changeme1234",
        "123456789012",
    }
)


# --------------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def password_needs_rehash(password_hash: str) -> bool:
    """True when the stored hash predates the current cost parameters."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def validate_password_strength(password: str, *, email: str | None = None) -> None:
    """Raise ValidationError with every problem at once, not one at a time."""
    problems: list[str] = []

    if len(password) < settings.PASSWORD_MIN_LENGTH:
        problems.append(f"Must be at least {settings.PASSWORD_MIN_LENGTH} characters")
    if password.lower() in _COMMON_PASSWORDS:
        problems.append("This password is too common")
    if password.isdigit() or password.isalpha():
        problems.append("Mix letters with numbers or symbols")
    if email and email.split("@")[0].lower() in password.lower():
        problems.append("Must not contain your email address")

    if problems:
        raise ValidationError(
            "That password does not meet the policy.",
            details=[{"field": "password", "message": problem} for problem in problems],
        )


# ------------------------------------------------------------------ access token


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: uuid.UUID
    role: Role
    jti: str
    expires_at: datetime


def create_access_token(*, user_id: uuid.UUID, role: Role) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.ACCESS_TOKEN_MINUTES)
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "role": role.value,
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "typ": "access",
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token, expires_at


def decode_access_token(token: str) -> TokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "sub", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthenticatedError(
            "Your session has expired. Please sign in again.",
            log_detail="access token expired",
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise UnauthenticatedError(log_detail=f"invalid access token: {exc}") from exc

    if payload.get("typ") != "access":
        raise UnauthenticatedError(log_detail="token is not an access token")

    try:
        role = Role(payload["role"])
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as exc:
        raise UnauthenticatedError(log_detail=f"malformed token claims: {exc}") from exc

    return TokenClaims(
        user_id=user_id,
        role=role,
        jti=str(payload["jti"]),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=UTC),
    )


# ----------------------------------------------------------------- refresh token


def generate_refresh_token() -> tuple[str, str, datetime]:
    """Return (raw_token, token_hash, expires_at).

    The raw token leaves in an httpOnly cookie and is never persisted; only the
    hash is stored, so the database cannot be replayed as a session.
    """
    raw = secrets.token_urlsafe(48)
    expires_at = datetime.now(UTC) + timedelta(days=settings.REFRESH_TOKEN_DAYS)
    return raw, hash_refresh_token(raw), expires_at


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return secrets.compare_digest(left, right)


__all__ = [
    "TokenClaims",
    "constant_time_equals",
    "create_access_token",
    "decode_access_token",
    "generate_refresh_token",
    "hash_password",
    "hash_refresh_token",
    "password_needs_rehash",
    "validate_password_strength",
    "verify_password",
]
