"""Notifications.

Introduced in Phase 8 for the zero-bench sweep, built to the full DATABASE.md
section 3 shape so Phase 12 adds categories and delivery channels rather than
migrating the table. Only `BENCH_REDEPLOYMENT` is produced today.

`dedupe_key` is the load-bearing column. The sweep runs daily and a 30-day
milestone stays reached for several runs, so without a unique key every
consultant approaching the bench would generate an alert every morning until
they rolled off — which is how people learn to ignore alerts.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.permissions import Role
from app.db.base import BaseEntity
from app.db.types import GUID, JSONType, StrEnumType, UTCDateTime


class NotificationCategory(StrEnum):
    SUBMISSION_SLA = "SUBMISSION_SLA"
    DOCUMENT_EXPIRY = "DOCUMENT_EXPIRY"
    BENCH_REDEPLOYMENT = "BENCH_REDEPLOYMENT"
    INTERVIEW_REMINDER = "INTERVIEW_REMINDER"
    FOLLOW_UP_OVERDUE = "FOLLOW_UP_OVERDUE"
    PROJECT_ENDING = "PROJECT_ENDING"
    SYSTEM = "SYSTEM"


class NotificationSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class Notification(BaseEntity):
    __tablename__ = "notifications"

    #: Addressed to one person, or to a whole role, or both. A bench alert goes
    #: to Resourcing as a group *and* to the account's named sales owner.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    role_target: Mapped[Role | None] = mapped_column(StrEnumType(Role), nullable=True, index=True)

    category: Mapped[NotificationCategory] = mapped_column(
        StrEnumType(NotificationCategory), nullable=False, index=True
    )
    severity: Mapped[NotificationSeverity] = mapped_column(
        StrEnumType(NotificationSeverity),
        nullable=False,
        default=NotificationSeverity.INFO,
        index=True,
    )

    title: Mapped[str] = mapped_column(String(240), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    #: Where clicking the alert should land. Stored rather than derived so a
    #: route change never turns historical alerts into dead links.
    action_url: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Structured detail for the UI — for a bench alert, the ranked suggestions.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    is_read: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    #: Unique per fact, not per run. See the module docstring.
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)

    __table_args__ = (
        Index("ix_notifications_inbox", "user_id", "is_read", "created_at"),
        Index("ix_notifications_role_inbox", "role_target", "is_read", "created_at"),
    )

    def mark_read(self, *, when: datetime) -> None:
        if not self.is_read:
            self.is_read = True
            self.read_at = when


__all__ = [
    "Notification",
    "NotificationCategory",
    "NotificationSeverity",
]
