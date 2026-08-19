"""Requirement, JD parsing, review and SLA-deadline endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from sqlalchemy import select

from app.ai.extraction.text import SUPPORTED_EXTENSIONS, extract_text
from app.core.config import settings
from app.core.deps import SessionDep, require
from app.core.pagination import Page, PageParams, page_params
from app.core.permissions import Permission
from app.core.rate_limit import rate_limit
from app.db.types import utcnow
from app.models.accounts import Account, Project
from app.models.demand import (
    ContractType,
    DeadlineState,
    PrioritySource,
    Requirement,
    RequirementSource,
    RequirementStatus,
    ReviewStatus,
)
from app.models.identity import User
from app.repositories.demand import SkillRepository
from app.schemas.demand import (
    AcceptParseRequest,
    DeadlineInfo,
    ParseResultResponse,
    ParseTextRequest,
    RequirementCreate,
    RequirementDeadlineBoard,
    RequirementResponse,
    RequirementSkillResponse,
    RequirementStatusChange,
    RequirementStatusHistoryResponse,
    RequirementUpdate,
)
from app.services.requirements import RequirementService
from app.services.sla import deadline_status, describe

router = APIRouter(prefix="/requirements", tags=["requirements"])
skills_router = APIRouter(prefix="/skills", tags=["skills"])


# --------------------------------------------------------------- serializers


async def _name_map(session: Any, model: Any, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = {value for value in ids if value}
    if not ids:
        return {}
    label = model.full_name if model is User else model.name
    rows = await session.execute(select(model.id, label).where(model.id.in_(ids)))
    return {row[0]: row[1] for row in rows}


def _deadline_info(requirement: Requirement, *, now: datetime) -> DeadlineInfo | None:
    if requirement.response_deadline_at is None:
        return None
    status_value = deadline_status(requirement.response_deadline_at, now=now)
    return DeadlineInfo(
        state=status_value.state,
        deadline=status_value.deadline,
        hours_remaining=status_value.hours_remaining,
        is_overdue=status_value.is_overdue,
        label=describe(status_value),
    )


async def _serialize(session: Any, requirements: list[Requirement]) -> list[RequirementResponse]:
    """Batched name resolution, so a page never triggers N+1."""
    if not requirements:
        return []

    now = utcnow()
    account_ids: set[uuid.UUID] = set()
    for requirement in requirements:
        for account_id in (
            requirement.account_id,
            requirement.end_customer_id,
            requirement.route_account_id,
        ):
            if account_id is not None:
                account_ids.add(account_id)
    account_names = await _name_map(session, Account, account_ids)
    project_names = await _name_map(
        session, Project, {r.project_id for r in requirements if r.project_id}
    )
    owner_names = await _name_map(session, User, {r.owner_id for r in requirements if r.owner_id})

    items: list[RequirementResponse] = []
    for requirement in requirements:
        # Validated from columns: `skills` on the ORM holds link rows, not the
        # skill records the response declares.
        response = RequirementResponse.model_validate(requirement.to_dict())
        response.account_name = (
            account_names.get(requirement.account_id) if requirement.account_id else None
        )
        response.end_customer_name = (
            account_names.get(requirement.end_customer_id) if requirement.end_customer_id else None
        )
        response.route_account_name = (
            account_names.get(requirement.route_account_id)
            if requirement.route_account_id
            else None
        )
        response.project_name = (
            project_names.get(requirement.project_id) if requirement.project_id else None
        )
        response.owner_name = (
            owner_names.get(requirement.owner_id) if requirement.owner_id else None
        )
        response.deadline = _deadline_info(requirement, now=now)
        response.needs_review = requirement.is_awaiting_review
        response.skills = [
            RequirementSkillResponse(
                id=link.id,
                skill_id=link.skill_id,
                name=link.skill.name,
                category=link.skill.category,
                importance=link.importance,
                min_years=link.min_years,
            )
            for link in requirement.skills
        ]
        items.append(response)
    return items


async def _serialize_one(session: Any, requirement: Requirement) -> RequirementResponse:
    return (await _serialize(session, [requirement]))[0]


# -------------------------------------------------------------- requirements


@router.get("", response_model=Page[RequirementResponse], summary="List requirements")
async def list_requirements(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.REQUIREMENT_READ))],
    params: Annotated[PageParams, Depends(page_params)],
    requirement_status: Annotated[RequirementStatus | None, Query(alias="status")] = None,
    priority_source: Annotated[PrioritySource | None, Query()] = None,
    contract_type: Annotated[ContractType | None, Query()] = None,
    account_id: Annotated[uuid.UUID | None, Query()] = None,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    owner_id: Annotated[uuid.UUID | None, Query()] = None,
    review_status: Annotated[ReviewStatus | None, Query()] = None,
    country: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    open_only: Annotated[bool, Query()] = False,
    has_deadline: Annotated[bool | None, Query()] = None,
    skill_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[RequirementResponse]:
    requirements, total = await RequirementService(session).list_requirements(
        params,
        status=requirement_status,
        priority_source=priority_source,
        contract_type=contract_type,
        account_id=account_id,
        project_id=project_id,
        owner_id=owner_id,
        review_status=review_status,
        country=country,
        open_only=open_only,
        has_deadline=has_deadline,
        skill_id=skill_id,
    )
    return Page.build(await _serialize(session, requirements), total, params)


@router.get(
    "/deadlines",
    response_model=RequirementDeadlineBoard,
    summary="Submission SLA board",
)
async def deadline_board(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.REQUIREMENT_READ))],
) -> RequirementDeadlineBoard:
    """Open requirements grouped by how much of their submission window is left.

    VMS windows are commonly 24-48 hours, so this board is the screen that
    decides whether a seat is winnable at all (SOW section 5 NEW).
    """
    service = RequirementService(session)
    requirements = await service.requirements.open_with_deadlines()
    serialized = await _serialize(session, requirements)

    buckets: dict[DeadlineState, list[RequirementResponse]] = {
        DeadlineState.URGENT: [],
        DeadlineState.DUE_SOON: [],
        DeadlineState.SAFE: [],
        DeadlineState.EXPIRED: [],
    }
    for item in serialized:
        if item.deadline is not None and item.deadline.state in buckets:
            buckets[item.deadline.state].append(item)

    return RequirementDeadlineBoard(
        urgent=buckets[DeadlineState.URGENT],
        due_soon=buckets[DeadlineState.DUE_SOON],
        safe=buckets[DeadlineState.SAFE],
        expired=buckets[DeadlineState.EXPIRED],
        counts={state.value.lower(): len(items) for state, items in buckets.items()},
    )


@router.post(
    "",
    response_model=RequirementResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a requirement manually",
)
async def create_requirement(
    payload: RequirementCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.REQUIREMENT_CREATE))],
) -> RequirementResponse:
    requirement = await RequirementService(session).create_requirement(payload, actor=actor)
    return await _serialize_one(session, requirement)


@router.get("/{requirement_id}", response_model=RequirementResponse, summary="Get a requirement")
async def get_requirement(
    requirement_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.REQUIREMENT_READ))],
) -> RequirementResponse:
    requirement = await RequirementService(session).get_requirement(requirement_id)
    return await _serialize_one(session, requirement)


@router.patch("/{requirement_id}", response_model=RequirementResponse, summary="Update")
async def update_requirement(
    requirement_id: uuid.UUID,
    payload: RequirementUpdate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.REQUIREMENT_UPDATE))],
) -> RequirementResponse:
    requirement = await RequirementService(session).update_requirement(
        requirement_id, payload, actor=actor
    )
    return await _serialize_one(session, requirement)


@router.delete(
    "/{requirement_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Archive a requirement"
)
async def archive_requirement(
    requirement_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.REQUIREMENT_DELETE))],
) -> None:
    await RequirementService(session).archive_requirement(requirement_id, actor=actor)


@router.post("/{requirement_id}/status", response_model=RequirementResponse, summary="Change stage")
async def change_status(
    requirement_id: uuid.UUID,
    payload: RequirementStatusChange,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.REQUIREMENT_UPDATE))],
) -> RequirementResponse:
    requirement = await RequirementService(session).change_status(
        requirement_id, payload.status, actor=actor, reason=payload.reason
    )
    return await _serialize_one(session, requirement)


@router.get(
    "/{requirement_id}/history",
    response_model=list[RequirementStatusHistoryResponse],
    summary="Stage history",
)
async def requirement_history(
    requirement_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.REQUIREMENT_READ))],
) -> list[RequirementStatusHistoryResponse]:
    entries = await RequirementService(session).history(requirement_id)
    user_names = await _name_map(session, User, {e.user_id for e in entries if e.user_id})

    responses = []
    for entry in entries:
        response = RequirementStatusHistoryResponse.model_validate(entry)
        response.user_name = user_names.get(entry.user_id) if entry.user_id else None
        responses.append(response)
    return responses


# ------------------------------------------------------------------ parsing


@router.post(
    "/parse-text",
    response_model=RequirementResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Parse a pasted job description",
    dependencies=[Depends(rate_limit("parsing", limit_setting="RATE_LIMIT_PARSING_PER_MINUTE"))],
)
async def parse_text(
    payload: ParseTextRequest,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.JD_PARSE))],
) -> RequirementResponse:
    requirement = await RequirementService(session).parse_text(payload, actor=actor)
    return await _serialize_one(session, requirement)


@router.post(
    "/parse-document",
    response_model=RequirementResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Parse an uploaded job description",
    dependencies=[Depends(rate_limit("parsing", limit_setting="RATE_LIMIT_PARSING_PER_MINUTE"))],
)
async def parse_document(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.JD_PARSE))],
    file: Annotated[UploadFile, File(description=f"One of: {', '.join(SUPPORTED_EXTENSIONS)}")],
    account_id: Annotated[uuid.UUID | None, Form()] = None,
    project_id: Annotated[uuid.UUID | None, Form()] = None,
    priority_source: Annotated[PrioritySource | None, Form()] = None,
) -> RequirementResponse:
    content = await file.read()
    text = extract_text(file.filename or "upload", content, max_bytes=settings.MAX_UPLOAD_BYTES)

    requirement = await RequirementService(session).parse_text(
        ParseTextRequest(
            text=text,
            source=RequirementSource.DOCUMENT_UPLOAD,
            source_detail=(file.filename or "")[:255] or None,
            priority_source=priority_source,
            account_id=account_id,
            project_id=project_id,
        ),
        actor=actor,
    )
    return await _serialize_one(session, requirement)


@router.get(
    "/{requirement_id}/parse-result",
    response_model=ParseResultResponse,
    summary="Extracted fields, confidence and evidence",
)
async def parse_result(
    requirement_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.REQUIREMENT_READ))],
) -> ParseResultResponse:
    service = RequirementService(session)
    requirement = await service.get_requirement(requirement_id)

    payload = requirement.parsed_payload or {}
    fields = service.parse_fields_for_review(requirement)

    # Skills the parser named that are not on the master yet, so a reviewer can
    # see what will be created rather than discovering it afterwards.
    skill_repo = SkillRepository(session)
    proposed: list[str] = []
    for key in ("mandatory_skills", "preferred_skills"):
        entry = (payload.get("fields") or {}).get(key) or {}
        proposed.extend(str(name) for name in (entry.get("value") or []))
    unresolved = [name for name in proposed if await skill_repo.get_by_name(name) is None]

    return ParseResultResponse(
        requirement_id=requirement.id,
        source_text=requirement.description_raw or "",
        provider=str(payload.get("provider", "unknown")),
        model_id=str(payload.get("model_id", "unknown")),
        used_fallback=bool(payload.get("used_fallback", False)),
        overall_confidence=float(payload.get("overall_confidence") or 0.0),
        fields=fields,
        unresolved_skills=sorted(set(unresolved)),
        warnings=list(payload.get("warnings") or []),
        confirmation_required=[f.field for f in fields if f.requires_confirmation],
    )


@router.post(
    "/{requirement_id}/accept-parse",
    response_model=RequirementResponse,
    summary="Accept the reviewed fields",
)
async def accept_parse(
    requirement_id: uuid.UUID,
    payload: AcceptParseRequest,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.REQUIREMENT_UPDATE))],
) -> RequirementResponse:
    requirement = await RequirementService(session).accept_parse(
        requirement_id, payload, actor=actor
    )
    return await _serialize_one(session, requirement)


@router.post(
    "/{requirement_id}/reject-parse",
    response_model=RequirementResponse,
    summary="Reject a parsed requirement",
)
async def reject_parse(
    requirement_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.REQUIREMENT_UPDATE))],
    reason: Annotated[str | None, Query(max_length=255)] = None,
) -> RequirementResponse:
    requirement = await RequirementService(session).reject_parse(
        requirement_id, actor=actor, reason=reason
    )
    return await _serialize_one(session, requirement)


# ------------------------------------------------------------------- skills


@skills_router.get("", response_model=list[dict], summary="Search the skill master")
async def search_skills(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.REQUIREMENT_READ))],
    q: Annotated[str | None, Query(max_length=120)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict]:
    skills = await SkillRepository(session).search(q, limit=limit)
    return [
        {
            "id": str(skill.id),
            "name": skill.name,
            "category": skill.category,
            "needs_review": skill.needs_review,
        }
        for skill in skills
    ]


__all__ = ["router", "skills_router"]
