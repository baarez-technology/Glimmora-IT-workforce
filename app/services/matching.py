"""Matching service: load, filter, score, persist.

Loads every candidate's skills in one query rather than per row — a 2,000
resource pool must not become 2,000 round trips (MATCHING.md section 6).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import log_business_event
from app.db.types import utcnow
from app.engines.matching.config import DEFAULT_THRESHOLDS, default_payload, validate_weights
from app.engines.matching.engine import (
    ENGINE_VERSION,
    MatchResult,
    RequirementView,
    apply_hard_filters,
    score_match,
)
from app.models.demand import Requirement
from app.models.identity import AuditAction, User
from app.models.matching import Match, MatchDirection, ScoringConfigKind, ScoringConfiguration
from app.models.talent import Resource
from app.repositories.talent import DocumentRepository, ResourceRepository
from app.services.audit import AuditService
from app.services.matching_views import (
    build_requirement_views,
    build_resource_views,
)


def _score_of(result: MatchResult, key: str) -> float | None:
    """Column value for one component, or NULL when it was not assessable."""
    component = result.component(key)
    return component.score if component else None


def defaults_for(kind: ScoringConfigKind) -> dict[str, Any]:
    """The shipped defaults for one rule set."""
    from app.engines.scoring.config import (
        DEFAULT_ADDRESSABILITY_RULES,
        DEFAULT_COMMERCIAL_BANDS,
        DEFAULT_OPPORTUNITY_WEIGHTS,
    )

    payloads: dict[ScoringConfigKind, dict[str, Any]] = {
        ScoringConfigKind.MATCH_WEIGHTS: default_payload(),
        ScoringConfigKind.ADDRESSABILITY_RULES: dict(DEFAULT_ADDRESSABILITY_RULES),
        ScoringConfigKind.COMMERCIAL_BANDS: dict(DEFAULT_COMMERCIAL_BANDS),
        ScoringConfigKind.OPPORTUNITY_WEIGHTS: dict(DEFAULT_OPPORTUNITY_WEIGHTS),
    }
    return payloads[kind]


def effective_thresholds(config: ScoringConfiguration) -> dict[str, Any]:
    """Stored thresholds layered over the shipped defaults.

    A stored configuration is a snapshot taken when it was published, so it
    cannot contain keys the code learned about afterwards. Reading it raw means
    every release that adds a threshold breaks every database that already has
    a configuration in it — which is exactly what happened when reverse
    matching introduced the reachability keys.

    Merging keeps operator intent (a stored value always wins) while letting new
    keys arrive with their documented default. Weights are deliberately *not*
    merged: they must sum to 100 and are validated whole at write time, so a
    silently back-filled weight would corrupt the total.
    """
    stored = config.payload.get("thresholds") or {}
    return {**DEFAULT_THRESHOLDS, **stored}


class ScoringConfigService:
    """Versioned scoring rules. Changing a rule is an Admin action (AD-2)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)

    async def active(self, kind: ScoringConfigKind) -> ScoringConfiguration:
        stmt = select(ScoringConfiguration).where(
            ScoringConfiguration.kind == kind, ScoringConfiguration.is_active.is_(True)
        )
        config = (await self.session.execute(stmt)).scalars().first()
        if config is not None:
            return config
        # First run: seed v1 from the documented defaults so the engine always
        # has a configuration to read. Each kind gets *its own* defaults —
        # seeding addressability rules with matching weights would be worse
        # than having no configuration at all.
        return await self.create(
            kind, name=f"{kind.value} v1", payload=defaults_for(kind), activate=True
        )

    async def list_configs(
        self, kind: ScoringConfigKind | None = None
    ) -> list[ScoringConfiguration]:
        stmt = select(ScoringConfiguration)
        if kind is not None:
            stmt = stmt.where(ScoringConfiguration.kind == kind)
        stmt = stmt.order_by(ScoringConfiguration.kind, ScoringConfiguration.version.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def create(
        self,
        kind: ScoringConfigKind,
        *,
        name: str,
        payload: dict[str, Any],
        notes: str | None = None,
        activate: bool = False,
        actor: User | None = None,
    ) -> ScoringConfiguration:
        if kind is ScoringConfigKind.MATCH_WEIGHTS:
            try:
                validate_weights(payload.get("weights", {}))
            except ValueError as exc:
                raise ValidationError(
                    str(exc), details=[{"field": "weights", "message": str(exc)}]
                ) from exc
        else:
            from app.services.scoring import validate_payload

            validate_payload(kind, payload)

        highest = (
            await self.session.execute(
                select(func.max(ScoringConfiguration.version)).where(
                    ScoringConfiguration.kind == kind
                )
            )
        ).scalar()

        config = ScoringConfiguration(
            kind=kind,
            name=name,
            version=(highest or 0) + 1,
            payload=payload,
            notes=notes,
            created_by=actor.id if actor else None,
        )
        self.session.add(config)
        await self.session.flush()

        if activate:
            await self.activate(config.id, actor=actor)
        return config

    async def activate(
        self, config_id: uuid.UUID, *, actor: User | None = None
    ) -> ScoringConfiguration:
        config = (
            await self.session.execute(
                select(ScoringConfiguration).where(ScoringConfiguration.id == config_id)
            )
        ).scalar_one_or_none()
        if config is None:
            raise NotFoundError("scoring configuration", config_id)

        # Exactly one active configuration per kind.
        others = await self.session.execute(
            select(ScoringConfiguration).where(
                ScoringConfiguration.kind == config.kind,
                ScoringConfiguration.is_active.is_(True),
                ScoringConfiguration.id != config.id,
            )
        )
        for other in others.scalars().all():
            other.is_active = False

        config.is_active = True
        await self.session.flush()

        if actor is not None:
            await self.audit.record(
                AuditAction.SCORING_CONFIG_CHANGED,
                summary=f"Activated {config.label}",
                actor=actor,
                entity_type="scoring_configuration",
                entity_id=config.id,
            )
        return config


class MatchingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.resources = ResourceRepository(session)
        self.documents = DocumentRepository(session)
        self.configs = ScoringConfigService(session)
        self.audit = AuditService(session)

    # ------------------------------------------------------------- loading
    async def _requirement_view(self, requirement: Requirement) -> RequirementView:
        views = await build_requirement_views(self.session, [requirement])
        return views[requirement.id]

    # ------------------------------------------------------------ matching
    async def run_for_requirement(
        self,
        requirement: Requirement,
        *,
        actor: User,
        limit: int = 25,
        today: date | None = None,
    ) -> list[MatchResult]:
        reference = today or utcnow().date()
        config = await self.configs.active(ScoringConfigKind.MATCH_WEIGHTS)
        weights = config.payload["weights"]
        thresholds = effective_thresholds(config)

        requirement_view = await self._requirement_view(requirement)

        # Only reviewed resources are matchable: AI output is not business data
        # until a human accepts it (AD-7).
        candidates = (
            (
                await self.session.execute(
                    self.resources.select()
                    .where(Resource.review_status == "ACCEPTED")
                    .limit(thresholds.get("max_candidates", 200))
                )
            )
            .scalars()
            .unique()
            .all()
        )

        views = await build_resource_views(self.session, list(candidates), today=reference)

        results: list[MatchResult] = []
        for view in views:
            outcome = apply_hard_filters(requirement_view, view, thresholds)
            if not outcome.included:
                continue
            results.append(
                score_match(
                    requirement_view,
                    view,
                    weights=weights,
                    thresholds=thresholds,
                    today=reference,
                )
            )

        results.sort(key=lambda item: item.overall_score, reverse=True)
        results = results[:limit]

        await self._persist(requirement.id, results, config=config, actor=actor)

        await self.audit.record(
            AuditAction.MATCH_GENERATED,
            summary=f"Matched {len(results)} resources against {requirement.title}",
            actor=actor,
            entity_type="requirement",
            entity_id=requirement.id,
        )
        log_business_event(
            "match_generated",
            requirement_id=str(requirement.id),
            candidates=len(views),
            matches=len(results),
        )
        return results

    async def _persist(
        self,
        requirement_id: uuid.UUID,
        results: list[MatchResult],
        *,
        config: ScoringConfiguration,
        actor: User,
    ) -> None:
        """Replace the previous snapshot for this requirement."""
        await self.session.execute(
            delete(Match).where(
                Match.requirement_id == requirement_id,
                Match.direction == MatchDirection.DEMAND_TO_RESOURCE,
            )
        )

        computed_at = utcnow()
        for result in results:
            components = {
                component.key: {
                    "label": component.label,
                    "score": component.score,
                    "weight": component.weight,
                    "contribution": component.contribution,
                    "evidence": component.evidence,
                    "detail": component.detail,
                }
                for component in result.components
            }

            self.session.add(
                Match(
                    requirement_id=requirement_id,
                    resource_id=result.resource_id,
                    direction=MatchDirection.DEMAND_TO_RESOURCE,
                    overall_score=result.overall_score,
                    band=result.band,
                    skill_score=_score_of(result, "skills"),
                    experience_score=_score_of(result, "experience"),
                    technology_score=_score_of(result, "technology"),
                    availability_score=_score_of(result, "availability"),
                    location_score=_score_of(result, "location"),
                    cost_score=_score_of(result, "cost"),
                    commercial_score=_score_of(result, "commercial"),
                    semantic_score=result.semantic_score,
                    confidence=result.confidence,
                    components=components,
                    gaps=result.gaps,
                    reasons=result.reasons,
                    # Already leads with the suppressor text (engine.build_warnings).
                    warnings=result.warnings,
                    missing_information=result.missing_information,
                    narrative=result.narrative,
                    weights_version=config.version,
                    engine_version=ENGINE_VERSION,
                    computed_at=computed_at,
                    computed_by=actor.id,
                )
            )
        await self.session.flush()

    async def stored_matches(self, requirement_id: uuid.UUID) -> list[Match]:
        stmt = (
            select(Match)
            .where(
                Match.requirement_id == requirement_id,
                Match.direction == MatchDirection.DEMAND_TO_RESOURCE,
            )
            .order_by(Match.overall_score.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def stored_match(self, requirement_id: uuid.UUID, resource_id: uuid.UUID) -> Match:
        stmt = select(Match).where(
            Match.requirement_id == requirement_id,
            Match.resource_id == resource_id,
            Match.direction == MatchDirection.DEMAND_TO_RESOURCE,
        )
        match = (await self.session.execute(stmt)).scalars().first()
        if match is None:
            raise NotFoundError("match", resource_id)
        return match

    async def last_computed(self, requirement_id: uuid.UUID) -> datetime | None:
        stmt = select(func.max(Match.computed_at)).where(Match.requirement_id == requirement_id)
        return (await self.session.execute(stmt)).scalar()


__all__ = [
    "MatchingService",
    "ScoringConfigService",
    "defaults_for",
    "effective_thresholds",
]
