"""Opportunity scoring, the commercial calculator, and rule simulation.

Every response here conforms to the explainability contract in SCORING.md
section 6: score, band, confidence, components, factors, positives, risks,
missing information, recommended action, narrative, and the rule versions that
produced it. A bare number is never returned.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import SessionDep, require
from app.core.permissions import Permission, permissions_for
from app.db.types import utcnow
from app.engines.scoring.commercial import CommercialInput, calculate
from app.engines.scoring.config import DEFAULT_CURRENCY_RATES
from app.models.demand import Requirement
from app.models.identity import User
from app.models.matching import ScoringConfigKind
from app.models.scoring import OpportunityScore
from app.services.scoring import ScoringService, effective

router = APIRouter(prefix="/scoring", tags=["scoring"])


# ------------------------------------------------------------------ schemas


class ScoreComponent(BaseModel):
    key: str
    label: str
    score: float | None
    weight: float
    contribution: float


class ScoreFactor(BaseModel):
    key: str
    label: str
    #: MET | NOT_MET | NOT_APPLICABLE | UNKNOWN. The distinction between NOT_MET
    #: and UNKNOWN is the difference between a useful score and a misleading one.
    state: str
    points: float
    max_points: float
    evidence: str | None = None


class CommercialSubScoreOut(BaseModel):
    key: str
    label: str
    points: float
    max_points: float
    evidence: str


class CommercialFigures(BaseModel):
    monthly_revenue: Decimal | None = None
    monthly_cost: Decimal | None = None
    gross_profit: Decimal | None = None
    margin_percent: float | None = None
    contract_value: Decimal | None = None
    total_profit: Decimal | None = None
    duration_months: int
    positions: int
    currency: str
    is_converted: bool
    one_off_total: Decimal
    one_off_monthly: Decimal
    missing_information: list[str] = Field(default_factory=list)


class ScoreResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID | None = None
    requirement_id: uuid.UUID
    requirement_title: str | None = None

    score: float
    band: str
    confidence: float

    talent_match_score: float | None = None
    addressability_score: float | None = None
    addressability_band: str | None = None
    supply_gate: float | None = None
    commercial_score: float | None = None

    components: list[ScoreComponent] = Field(default_factory=list)
    factors: list[ScoreFactor] = Field(default_factory=list)
    commercial_breakdown: list[CommercialSubScoreOut] = Field(default_factory=list)
    commercial: CommercialFigures | None = None

    positives: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    suppressors: list[str] = Field(default_factory=list)
    recommended_action: str | None = None
    narrative: str | None = None
    restricted_fields: list[str] = Field(default_factory=list)

    addressability_config_version: int | None = None
    commercial_config_version: int | None = None
    opportunity_config_version: int | None = None
    engine_version: str
    computed_at: datetime


class CommercialPreviewRequest(BaseModel):
    """What-if calculator. Persists nothing."""

    bill_rate: Decimal | None = Field(default=None, ge=0)
    bill_unit: str | None = None
    bill_currency: str | None = Field(default=None, min_length=3, max_length=3)
    cost_rate: Decimal | None = Field(default=None, ge=0)
    cost_unit: str | None = None
    cost_currency: str | None = Field(default=None, min_length=3, max_length=3)
    visa_cost: Decimal | None = Field(default=None, ge=0)
    insurance_cost: Decimal | None = Field(default=None, ge=0)
    other_cost: Decimal | None = Field(default=None, ge=0)
    duration_months: int | None = Field(default=None, ge=1, le=120)
    positions: int = Field(default=1, ge=1, le=50)


class SimulationRow(BaseModel):
    requirement_id: uuid.UUID
    requirement_title: str | None
    before_score: float
    after_score: float
    delta: float
    before_band: str
    after_band: str
    band_changed: bool


class SimulationResponse(BaseModel):
    kind: ScoringConfigKind
    evaluated: int
    changed: int
    band_changes: int
    average_delta: float
    distribution_before: dict[str, int]
    distribution_after: dict[str, int]
    rows: list[SimulationRow]


# -------------------------------------------------------------- serializing

#: Money fields. Roles without `margin:view` see the score and its reasoning but
#: not the underlying figures — the score is not itself a commercial secret.
_MONEY_FIELDS = (
    "monthly_revenue",
    "monthly_cost",
    "gross_profit",
    "margin_percent",
    "contract_value",
    "total_profit",
)


def _from_snapshot(snapshot: OpportunityScore, *, actor: User, title: str | None) -> ScoreResponse:
    can_see_margin = Permission.FIELD_MARGIN in permissions_for(actor.role)

    stored = snapshot.components or {}
    response = ScoreResponse(
        id=snapshot.id,
        requirement_id=snapshot.requirement_id,
        requirement_title=title,
        score=float(snapshot.opportunity_score),
        band=snapshot.band.value,
        confidence=snapshot.confidence,
        talent_match_score=float(snapshot.talent_match_score)
        if snapshot.talent_match_score is not None
        else None,
        addressability_score=float(snapshot.addressability_score)
        if snapshot.addressability_score is not None
        else None,
        addressability_band=snapshot.addressability_band.value
        if snapshot.addressability_band
        else None,
        supply_gate=snapshot.supply_gate,
        commercial_score=float(snapshot.commercial_score)
        if snapshot.commercial_score is not None
        else None,
        components=[ScoreComponent(key=key, **value) for key, value in stored.items()],
        factors=[ScoreFactor(**item) for item in snapshot.factor_breakdown or []],
        commercial_breakdown=[
            CommercialSubScoreOut(**item) for item in snapshot.commercial_breakdown or []
        ],
        positives=[str(item) for item in snapshot.positives or []],
        risks=[str(item) for item in snapshot.risks or []],
        missing_information=[str(item) for item in snapshot.missing_information or []],
        suppressors=[str(item) for item in snapshot.suppressors or []],
        recommended_action=snapshot.recommended_action,
        narrative=snapshot.narrative,
        addressability_config_version=snapshot.addressability_config_version,
        commercial_config_version=snapshot.commercial_config_version,
        opportunity_config_version=snapshot.opportunity_config_version,
        engine_version=snapshot.engine_version,
        computed_at=snapshot.computed_at,
    )

    if can_see_margin:
        response.commercial = CommercialFigures(
            monthly_revenue=snapshot.monthly_revenue,
            monthly_cost=snapshot.monthly_cost,
            gross_profit=snapshot.gross_profit,
            margin_percent=snapshot.margin_percent,
            contract_value=snapshot.contract_value,
            total_profit=snapshot.total_profit,
            duration_months=0,
            positions=1,
            currency=snapshot.currency,
            is_converted=snapshot.is_converted,
            one_off_total=Decimal("0"),
            one_off_monthly=Decimal("0"),
        )
    else:
        response.restricted_fields = list(_MONEY_FIELDS)

    return response


async def _title(session: Any, requirement_id: uuid.UUID) -> str | None:
    return (
        await session.execute(select(Requirement.title).where(Requirement.id == requirement_id))
    ).scalar()


# --------------------------------------------------------------- endpoints


@router.post(
    "/requirements/{requirement_id}/recompute",
    response_model=ScoreResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Compute and persist the Opportunity Score",
)
async def recompute(
    requirement_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.SCORING_RUN))],
) -> ScoreResponse:
    _, _, _, snapshot = await ScoringService(session).score_requirement(requirement_id, actor=actor)
    assert snapshot is not None
    return _from_snapshot(snapshot, actor=actor, title=await _title(session, requirement_id))


@router.get(
    "/requirements/{requirement_id}/explain",
    response_model=ScoreResponse,
    summary="The full explainability object for the current score",
)
async def explain(
    requirement_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.SCORING_READ))],
) -> ScoreResponse:
    service = ScoringService(session)
    snapshot = await service.current(requirement_id)
    if snapshot is None:
        # Nothing computed yet: score on demand rather than returning an empty
        # shell, but do not persist — reads must not have side effects.
        opportunity, addressability, commercial, _ = await service.score_requirement(
            requirement_id, persist=False
        )
        talent = opportunity.component("talent_match")
        return ScoreResponse(
            requirement_id=requirement_id,
            requirement_title=await _title(session, requirement_id),
            score=opportunity.score,
            band=opportunity.band.value,
            confidence=opportunity.confidence,
            talent_match_score=talent.score if talent else None,
            addressability_score=addressability.score,
            addressability_band=addressability.band.value,
            supply_gate=addressability.supply_gate,
            commercial_score=commercial.score,
            components=[
                ScoreComponent(
                    key=item.key,
                    label=item.label,
                    score=item.score,
                    weight=item.weight,
                    contribution=item.contribution,
                )
                for item in opportunity.components
            ],
            factors=[ScoreFactor(**item) for item in opportunity.factors],
            positives=opportunity.positives,
            risks=opportunity.risks,
            missing_information=opportunity.missing_information,
            suppressors=opportunity.suppressors,
            recommended_action=opportunity.recommended_action,
            narrative=opportunity.narrative,
            engine_version=opportunity.engine_version,
            computed_at=utcnow(),
        )
    return _from_snapshot(snapshot, actor=actor, title=await _title(session, requirement_id))


@router.get(
    "/requirements/{requirement_id}/history",
    response_model=list[ScoreResponse],
    summary="Score history — snapshots are never overwritten",
)
async def history(
    requirement_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.SCORING_READ))],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ScoreResponse]:
    snapshots = await ScoringService(session).history(requirement_id, limit=limit)
    title = await _title(session, requirement_id)
    return [_from_snapshot(item, actor=actor, title=title) for item in snapshots]


@router.get(
    "/opportunities",
    response_model=list[ScoreResponse],
    summary="Current scores, ranked",
)
async def ranked(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.SCORING_READ))],
    band: Annotated[str | None, Query(max_length=24)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ScoreResponse]:
    snapshots = await ScoringService(session).ranked(limit=limit, band=band)
    if not snapshots:
        return []

    rows = await session.execute(
        select(Requirement.id, Requirement.title).where(
            Requirement.id.in_([item.requirement_id for item in snapshots])
        )
    )
    titles = {row[0]: row[1] for row in rows}
    return [
        _from_snapshot(item, actor=actor, title=titles.get(item.requirement_id))
        for item in snapshots
    ]


@router.post(
    "/commercial/preview",
    response_model=CommercialFigures,
    summary="What-if commercial calculator — persists nothing",
)
async def commercial_preview(
    payload: CommercialPreviewRequest,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.COMMERCIAL_RUN))],
) -> CommercialFigures:
    config = await ScoringService(session).configs.active(ScoringConfigKind.COMMERCIAL_BANDS)
    bands = effective(ScoringConfigKind.COMMERCIAL_BANDS, config.payload)
    rates = {**DEFAULT_CURRENCY_RATES, **(bands.get("currency_rates") or {})}

    calculation = calculate(CommercialInput(**payload.model_dump()), bands=bands, rates=rates)
    # `CommercialCalculation` uses slots, so asdict() rather than __dict__.
    return CommercialFigures(**asdict(calculation))


@router.post(
    "/configurations/{config_id}/simulate",
    response_model=SimulationResponse,
    summary="Re-score recent requirements under a draft rule set, before activating it",
)
async def simulate(
    config_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.SCORING_CONFIG_EDIT))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SimulationResponse:
    from app.services.simulation import SimulationService

    return SimulationResponse(
        **await SimulationService(session).simulate(config_id, limit=limit, actor=actor)
    )


__all__ = ["router"]
