"""Reverse matching and the bench radar.

Same explainability contract as forward matching: no suggestion is ever a bare
number. Each one carries the component breakdown, the named route, and — new in
this direction — why the route scores the way it does.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.matching import MatchComponent, _components
from app.core.deps import SessionDep, require
from app.core.permissions import Permission, permissions_for
from app.db.types import utcnow
from app.models.demand import Requirement
from app.models.identity import User
from app.models.matching import Match, MatchBand
from app.models.talent import Resource
from app.services.documents import blocks_deployment
from app.services.reverse_matching import BenchSweepService, ReverseMatchingService

router = APIRouter(prefix="/reverse-matching", tags=["reverse-matching"])


# ------------------------------------------------------------------ schemas


class RouteInfo(BaseModel):
    route_type: str
    label: str | None
    #: 0-1, or null when the requirement names no account. Null is unknown,
    #: never "unreachable" — the priority is not discounted for it.
    reachability: float | None
    via_account_id: uuid.UUID | None = None


class SuggestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    requirement_id: uuid.UUID
    requirement_title: str | None = None
    account_name: str | None = None

    overall_score: float
    priority_score: float | None
    band: MatchBand
    confidence: float
    route: RouteInfo

    components: list[MatchComponent] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    restricted_components: list[str] = Field(default_factory=list)
    narrative: str | None = None

    weights_version: int | None
    engine_version: str
    computed_at: datetime


class ReverseRunResponse(BaseModel):
    resource_id: uuid.UUID
    resource_name: str
    availability_status: str
    available_from: date | None
    computed_at: datetime | None
    total: int
    suggestions: list[SuggestionResponse]


class BenchRow(BaseModel):
    resource_id: uuid.UUID
    resource_name: str
    headline: str | None
    availability_status: str
    available_from: date | None
    days_until_available: int | None
    blocks_deployment: bool
    top_suggestion: SuggestionResponse | None = None


class BenchRadarResponse(BaseModel):
    total: int
    on_bench_now: int
    without_a_suggestion: int
    rows: list[BenchRow]


class SweepResponse(BaseModel):
    examined: int
    raised: int
    skipped_duplicate: int


# -------------------------------------------------------------- serializing

_MARGIN_COMPONENTS = frozenset({"cost", "commercial"})
_STRUCTURED = {
    "components",
    "gaps",
    "reasons",
    "warnings",
    "missing_information",
    "route_type",
    "route_label",
    "route_account_id",
    "reachability",
}


async def _serialize(
    session: AsyncSession, matches: list[Match], *, actor: User
) -> list[SuggestionResponse]:
    if not matches:
        return []

    can_see_margin = Permission.FIELD_MARGIN in permissions_for(actor.role)

    rows = await session.execute(
        select(Requirement.id, Requirement.title).where(
            Requirement.id.in_([match.requirement_id for match in matches])
        )
    )
    titles: dict[uuid.UUID, str] = {row[0]: row[1] for row in rows}

    items: list[SuggestionResponse] = []
    for match in matches:
        payload: dict[str, Any] = match.to_dict(exclude=_STRUCTURED)
        response = SuggestionResponse.model_validate(
            payload
            | {
                "route": RouteInfo(
                    route_type=match.route_type or "UNKNOWN",
                    label=match.route_label,
                    reachability=match.reachability,
                    via_account_id=match.route_account_id,
                )
            }
        )
        response.requirement_title = titles.get(match.requirement_id)
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


def _run_response(
    resource: Resource, matches: list[SuggestionResponse], computed_at: datetime | None
) -> ReverseRunResponse:
    return ReverseRunResponse(
        resource_id=resource.id,
        resource_name=resource.full_name,
        availability_status=resource.availability_status.value,
        available_from=resource.available_from,
        computed_at=computed_at,
        total=len(matches),
        suggestions=matches,
    )


# --------------------------------------------------------------- endpoints


@router.post(
    "/resources/{resource_id}/run",
    response_model=ReverseRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Find this consultant's next billable seat",
)
async def run_reverse_matching(
    resource_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.REVERSE_MATCHING_RUN))],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> ReverseRunResponse:
    service = ReverseMatchingService(session)
    resource = await service.get_resource(resource_id)

    await service.run_for_resource(resource, actor=actor, limit=limit)
    stored = await service.stored_suggestions(resource_id)

    return _run_response(
        resource,
        await _serialize(session, stored, actor=actor),
        await service.last_computed(resource_id),
    )


@router.get(
    "/resources/{resource_id}",
    response_model=ReverseRunResponse,
    summary="Stored next-assignment options, ranked by redeployment priority",
)
async def get_reverse_matches(
    resource_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.REVERSE_MATCHING_READ))],
) -> ReverseRunResponse:
    service = ReverseMatchingService(session)
    resource = await service.get_resource(resource_id)
    stored = await service.stored_suggestions(resource_id)

    return _run_response(
        resource,
        await _serialize(session, stored, actor=actor),
        await service.last_computed(resource_id),
    )


@router.get(
    "/bench-radar",
    response_model=BenchRadarResponse,
    summary="Everyone on the bench or heading for it",
)
async def bench_radar(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.REVERSE_MATCHING_READ))],
    days_ahead: Annotated[int, Query(ge=1, le=365)] = 90,
) -> BenchRadarResponse:
    today = utcnow().date()
    board = await ReverseMatchingService(session).bench_radar(days_ahead=days_ahead)

    suggestions = await _serialize(
        session, [match for _, _, match in board if match is not None], actor=actor
    )
    by_id = {item.id: item for item in suggestions}

    rows: list[BenchRow] = []
    for resource, days, match in board:
        rows.append(
            BenchRow(
                resource_id=resource.id,
                resource_name=resource.full_name,
                headline=resource.headline,
                availability_status=resource.availability_status.value,
                available_from=resource.available_from,
                days_until_available=days,
                blocks_deployment=blocks_deployment(list(resource.documents), today=today),
                top_suggestion=by_id.get(match.id) if match is not None else None,
            )
        )

    return BenchRadarResponse(
        total=len(rows),
        on_bench_now=len([row for row in rows if row.days_until_available == 0]),
        # The number that matters: unbilled capacity with nowhere identified to
        # go. These are the people someone has to act on today.
        without_a_suggestion=len([row for row in rows if row.top_suggestion is None]),
        rows=rows,
    )


@router.post(
    "/bench-sweep",
    response_model=SweepResponse,
    summary="Run the milestone sweep now (normally scheduled daily)",
)
async def run_bench_sweep(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.REVERSE_MATCHING_RUN))],
) -> SweepResponse:
    result = await BenchSweepService(session).run(actor=actor)
    return SweepResponse(**result)


__all__ = ["router"]
