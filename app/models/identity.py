"""Identity, session and audit models (DATABASE.md section 3.1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.permissions import Role
from app.db.base import BaseEntity
from app.db.types import GUID, JSONType, StrEnumType, UTCDateTime


class AuditAction(StrEnum):
    """The audited action catalogue (SECURITY.md section 5).

    Declared in full from Phase 3 so later phases record against a fixed
    vocabulary rather than inventing action names as they go.
    """

    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    USER_CREATED = "USER_CREATED"
    USER_UPDATED = "USER_UPDATED"
    USER_DEACTIVATED = "USER_DEACTIVATED"
    PERMISSION_CHANGED = "PERMISSION_CHANGED"

    ACCOUNT_CREATED = "ACCOUNT_CREATED"
    ACCOUNT_UPDATED = "ACCOUNT_UPDATED"

    REQUIREMENT_CREATED = "REQUIREMENT_CREATED"
    REQUIREMENT_UPDATED = "REQUIREMENT_UPDATED"
    REQUIREMENT_STATUS_CHANGED = "REQUIREMENT_STATUS_CHANGED"
    JD_PARSED = "JD_PARSED"

    RESOURCE_CREATED = "RESOURCE_CREATED"
    RESOURCE_UPDATED = "RESOURCE_UPDATED"
    CV_UPLOADED = "CV_UPLOADED"
    CV_PARSED = "CV_PARSED"

    DOCUMENT_UPLOADED = "DOCUMENT_UPLOADED"
    DOCUMENT_DOWNLOADED = "DOCUMENT_DOWNLOADED"
    DOCUMENT_UPDATED = "DOCUMENT_UPDATED"
    DOCUMENT_DELETED = "DOCUMENT_DELETED"

    MATCH_GENERATED = "MATCH_GENERATED"
    SCORE_COMPUTED = "SCORE_COMPUTED"
    SCORING_CONFIG_CHANGED = "SCORING_CONFIG_CHANGED"

    OPPORTUNITY_CREATED = "OPPORTUNITY_CREATED"
    OPPORTUNITY_STAGE_CHANGED = "OPPORTUNITY_STAGE_CHANGED"
    OPPORTUNITY_DECISION = "OPPORTUNITY_DECISION"

    CV_SUBMITTED = "CV_SUBMITTED"
    SUBMISSION_STATUS_CHANGED = "SUBMISSION_STATUS_CHANGED"
    INTERVIEW_CREATED = "INTERVIEW_CREATED"
    INTERVIEW_OUTCOME_RECORDED = "INTERVIEW_OUTCOME_RECORDED"
    SELECTION_RECORDED = "SELECTION_RECORDED"

    DEPLOYMENT_CREATED = "DEPLOYMENT_CREATED"
    DEPLOYMENT_UPDATED = "DEPLOYMENT_UPDATED"
    DEPLOYMENT_ENDED = "DEPLOYMENT_ENDED"

    BILLING_CREATED = "BILLING_CREATED"
    BILLING_UPDATED = "BILLING_UPDATED"
    BILLING_CONFIRMED = "BILLING_CONFIRMED"

    IMPORT_COMMITTED = "IMPORT_COMMITTED"
    EXPORT_GENERATED = "EXPORT_GENERATED"


class User(BaseEntity):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(StrEnumType(Role), nullable=False, index=True)
    job_title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)
    must_change_password: Mapped[bool] = mapped_column(default=False, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    failed_login_count: Mapped[int] = mapped_column(default=0, nullable=False)

    # lazy="raise": a lazy load inside async request handling raises the
    # opaque greenlet_spawn error, so accidental access fails loudly here
    # instead. Callers that need tokens load them explicitly.
    # passive_deletes: the FK carries ON DELETE CASCADE, so the collection
    # never has to be loaded just to delete a user.
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="raise",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return f"<User {self.email} role={self.role}>"


class RefreshToken(BaseEntity):
    """One row per issued refresh token.

    Rotation creates a new row and marks the old one replaced. Reuse of a
    replaced token means the cookie was stolen, so the whole family is revoked
    (SECURITY.md section 1).
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(back_populates="refresh_tokens", lazy="raise")

    __table_args__ = (Index("ix_refresh_tokens_user_active", "user_id", "revoked_at"),)


class AuditLog(BaseEntity):
    """Append-only. No API path updates or deletes a row."""

    __tablename__ = "audit_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[AuditAction] = mapped_column(
        StrEnumType(AuditAction), nullable=False, index=True
    )
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_audit_logs_entity", "entity_type", "entity_id", "created_at"),
        Index("ix_audit_logs_actor_time", "user_id", "created_at"),
    )


class LoginAttempt(BaseEntity):
    """Failed-login ledger backing account lockout and brute-force detection.

    Stored separately from `users.failed_login_count` because attempts against a
    non-existent email must be counted too — otherwise the lockout response time
    reveals which addresses are real.
    """

    __tablename__ = "login_attempts"

    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    succeeded: Mapped[bool] = mapped_column(default=False, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (Index("ix_login_attempts_email_time", "email", "created_at"),)


__all__ = ["AuditAction", "AuditLog", "LoginAttempt", "RefreshToken", "User"]
