"""Matching endpoints.

Every response carries the full component breakdown. The UI is required to
render it: a match shown as a bare percentage is a match nobody can defend to a
client (MATCHING.md section 5).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import SessionDep, require
from app.core.permissions import Permission, permissions_for
from app.engines.matching.config import DEFAULT_MATCH_WEIGHTS
from app.models.identity import User
from app.models.matching import Match, MatchBand, ScoringConfigKind
from app.models.talent import Resource
from app.services.matching import MatchingService, ScoringConfigService
from app.services.requirements import RequirementService

router = APIRouter(prefix="/matching", tags=["matching"])
scoring_router = APIRouter(prefix="/scoring", tags=["scoring"])


# ------------------------------------------------------------------ schemas


class MatchComponent(BaseModel):
    key: str
    label: str
    score: float | None
    weight: float
    contribution: float
    evidence: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class MatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    requirement_id: uuid.UUID
    resource_id: uuid.UUID
    resource_name: str | None = None
    resource_headline: str | None = None
    resource_type: str | None = None
    availability_status: str | None = None

    overall_score: float
    band: MatchBand
    confidence: float

    components: list[MatchComponent] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    restricted_components: list[str] = Field(
        default_factory=list,
        description="Components withheld from this role, named so the UI can say so.",
    )
    narrative: str | None = None

    weights_version: int | None
    engine_version: str
    computed_at: datetime


class MatchRunResponse(BaseModel):
    requirement_id: uuid.UUID
    requirement_title: str
    computed_at: datetime | None
    weights_version: int | None
    total: int
    matches: list[MatchResponse]


class ScoringConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: ScoringConfigKind
    name: str
    version: int
    is_active: bool
    payload: dict[str, Any]
    notes: str | None
    created_at: datetime


class ScoringConfigCreate(BaseModel):
    kind: ScoringConfigKind = ScoringConfigKind.MATCH_WEIGHTS
    name: str = Field(min_length=2, max_length=120)
    payload: dict[str, Any]
    notes: str | None = None
    activate: bool = False


# -------------------------------------------------------------- serializing


#: Components that express margin. Roles without :data:`Permission.FIELD_MARGIN`
#: are shown that the component exists and was withheld, never a silently
#: shorter breakdown that looks complete (SECURITY.md section 3).
_MARGIN_COMPONENTS = frozenset({"cost", "commercial"})

#: Columns whose Python shape differs from the response shape; set by hand below.
_STRUCTURED = {"components", "gaps", "reasons", "warnings", "missing_information"}


def _components(match: Match) -> list[MatchComponent]:
    stored = match.components or {}
    ordered = [key for key in DEFAULT_MATCH_WEIGHTS if key in stored]
    return [MatchComponent(key=key, **stored[key]) for key in ordered]


async def _serialize(
    session: AsyncSession, matches: list[Match], *, actor: User
) -> list[MatchResponse]:
    if not matches:
        return []

    can_see_margin = Permission.FIELD_MARGIN in permissions_for(actor.role)

    rows = await session.execute(
        select(
            Resource.id,
            Resource.full_name,
            Resource.headline,
            Resource.resource_type,
            Resource.availability_status,
        ).where(Resource.id.in_([match.resource_id for match in matches]))
    )
    details = {row[0]: row for row in rows}

    items: list[MatchResponse] = []
    for match in matches:
        response = MatchResponse.model_validate(match.to_dict(exclude=_STRUCTURED))
        row = details.get(match.resource_id)
        if row is not None:
            response.resource_name = row[1]
            response.resource_headline = row[2]
            response.resource_type = str(row[3])
            response.availability_status = str(row[4])

        response.gaps = [str(item) for item in match.gaps or []]
        response.reasons = [str(item) for item in match.reasons or []]
        response.warnings = [str(item) for item in match.warnings or []]
        response.missing_information = [str(item) for item in match.missing_information or []]

        components = _components(match)
        if can_see_margin:
            response.components = components
        else:
            response.components = [c for c in components if c.key not in _MARGIN_COMPONENTS]
            response.restricted_components = [
                c.label for c in components if c.key in _MARGIN_COMPONENTS
            ]

        items.append(response)
    return items


# ----------------------------------------------------------------- matching


@router.post(
    "/requirements/{requirement_id}/run",
    response_model=MatchRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Compute and persist matches for a requirement",
)
async def run_matching(
    requirement_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.MATCHING_RUN))],
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
) -> MatchRunResponse:
    requirement = await RequirementService(session).get_requirement(requirement_id)
    service = MatchingService(session)

    await service.run_for_requirement(requirement, actor=actor, limit=limit)
    await session.flush()

    stored = await service.stored_matches(requirement_id)
    return MatchRunResponse(
        requirement_id=requirement_id,
        requirement_title=requirement.title,
        computed_at=await service.last_computed(requirement_id),
        weights_version=stored[0].weights_version if stored else None,
        total=len(stored),
        matches=await _serialize(session, stored, actor=actor),
    )


@router.get(
    "/requirements/{requirement_id}",
    response_model=MatchRunResponse,
    summary="Ranked matches with full explanations",
)
async def get_matches(
    requirement_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.MATCHING_READ))],
    band: Annotated[MatchBand | None, Query()] = None,
    min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
) -> MatchRunResponse:
    requirement = await RequirementService(session).get_requirement(requirement_id)
    service = MatchingService(session)

    stored = await service.stored_matches(requirement_id)
    if band is not None:
        stored = [match for match in stored if match.band is band]
    if min_score is not None:
        stored = [match for match in stored if float(match.overall_score) >= min_score]

    return MatchRunResponse(
        requirement_id=requirement_id,
        requirement_title=requirement.title,
        computed_at=await service.last_computed(requirement_id),
        weights_version=stored[0].weights_version if stored else None,
        total=len(stored),
        matches=await _serialize(session, stored, actor=actor),
    )


@router.get(
    "/requirements/{requirement_id}/resources/{resource_id}",
    response_model=MatchResponse,
    summary="One match, fully explained",
)
async def get_match(
    requirement_id: uuid.UUID,
    resource_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.MATCHING_READ))],
) -> MatchResponse:
    match = await MatchingService(session).stored_match(requirement_id, resource_id)
    return (await _serialize(session, [match], actor=actor))[0]


# ------------------------------------------------------- scoring configuration


@scoring_router.get(
    "/configurations",
    response_model=list[ScoringConfigResponse],
    summary="Scoring rule versions",
)
async def list_configurations(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.SCORING_CONFIG_READ))],
    kind: Annotated[ScoringConfigKind | None, Query()] = None,
) -> list[ScoringConfigResponse]:
    configs = await ScoringConfigService(session).list_configs(kind)
    return [ScoringConfigResponse.model_validate(config) for config in configs]


@scoring_router.post(
    "/configurations",
    response_model=ScoringConfigResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new scoring rule version",
)
async def create_configuration(
    payload: ScoringConfigCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.SCORING_CONFIG_EDIT))],
) -> ScoringConfigResponse:
    config = await ScoringConfigService(session).create(
        payload.kind,
        name=payload.name,
        payload=payload.payload,
        notes=payload.notes,
        activate=payload.activate,
        actor=actor,
    )
    return ScoringConfigResponse.model_validate(config)


@scoring_router.post(
    "/configurations/{config_id}/activate",
    response_model=ScoringConfigResponse,
    summary="Activate a scoring rule version",
)
async def activate_configuration(
    config_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.SCORING_CONFIG_EDIT))],
) -> ScoringConfigResponse:
    config = await ScoringConfigService(session).activate(config_id, actor=actor)
    return ScoringConfigResponse.model_validate(config)


__all__ = ["router", "scoring_router"]
