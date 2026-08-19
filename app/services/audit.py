"""Audit service.

Append-only trail of who changed what (SECURITY.md section 5). Passwords,
tokens and document bytes never reach it, and the diff is limited to an
allow-list of fields so a change record cannot become a second copy of the data.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger, request_id_ctx
from app.models.identity import AuditAction, AuditLog, User
from app.repositories.identity import AuditRepository

logger = get_logger("audit")

#: Never written to an audit diff, at any depth.
NEVER_AUDITED = frozenset(
    {
        "hashed_password",
        "password",
        "new_password",
        "current_password",
        "token",
        "token_hash",
        "access_token",
        "refresh_token",
        "secret",
        "api_key",
    }
)


def _serialise(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return str(value)
    return value


def build_diff(
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    *,
    fields: set[str],
) -> dict[str, dict[str, Any]] | None:
    """Before/after for the named fields, limited to values that actually changed."""
    before = before or {}
    after = after or {}
    changes: dict[str, dict[str, Any]] = {}

    for field in sorted(fields - NEVER_AUDITED):
        old = _serialise(before.get(field))
        new = _serialise(after.get(field))
        if old != new:
            changes[field] = {"from": old, "to": new}

    return changes or None


class AuditService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.repo = AuditRepository(session)

    async def record(
        self,
        action: AuditAction,
        *,
        summary: str,
        actor: User | None = None,
        actor_email: str | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        changes: dict[str, Any] | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuditLog:
        entry = AuditLog(
            user_id=actor.id if actor else None,
            actor_email=(actor.email if actor else actor_email),
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=summary,
            changes=changes,
            ip_address=ip_address,
            user_agent=(user_agent or "")[:255] or None,
            request_id=request_id_ctx.get(),
        )
        await self.repo.add(entry)
        logger.info(
            "audit",
            action=action.value,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else None,
        )
        return entry


__all__ = ["NEVER_AUDITED", "AuditService", "build_diff"]
