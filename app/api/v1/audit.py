"""Audit log viewer (ADMIN and MANAGEMENT)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.deps import SessionDep, require
from app.core.pagination import Page, PageParams, page_params
from app.core.permissions import Permission
from app.models.identity import AuditAction, User
from app.repositories.identity import AuditRepository
from app.schemas.identity import AuditLogResponse

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("", response_model=Page[AuditLogResponse], summary="Search the audit trail")
async def list_audit_logs(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.AUDIT_VIEW))],
    params: Annotated[PageParams, Depends(page_params)],
    user_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[AuditAction | None, Query()] = None,
    entity_type: Annotated[str | None, Query(max_length=64)] = None,
    entity_id: Annotated[uuid.UUID | None, Query()] = None,
    date_from: Annotated[datetime | None, Query()] = None,
    date_to: Annotated[datetime | None, Query()] = None,
) -> Page[AuditLogResponse]:
    logs, total = await AuditRepository(session).list_logs(
        params,
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        date_from=date_from,
        date_to=date_to,
    )
    return Page.build([AuditLogResponse.model_validate(log) for log in logs], total, params)


@router.get("/actions", response_model=list[str], summary="Audited action catalogue")
async def list_actions(
    _: Annotated[User, Depends(require(Permission.AUDIT_VIEW))],
) -> list[str]:
    return [action.value for action in AuditAction]


__all__ = ["router"]
