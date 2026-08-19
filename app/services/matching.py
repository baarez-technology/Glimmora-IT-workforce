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
from sqlalchemy.orm import selectinload

from app.ai.vocabulary import SKILL_TO_TECHNOLOGY, SKILL_VOCABULARY
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import log_business_event
from app.db.types import utcnow
from app.engines.matching.config import default_payload, validate_weights
from app.engines.matching.engine import (
    ENGINE_VERSION,
    MatchResult,
    RequirementView,
    ResourceView,
    apply_hard_filters,
    score_match,
)
from app.models.demand import Requirement, RequirementSkill, SkillImportance
from app.models.identity import AuditAction, User
from app.models.matching import Match, MatchDirection, ScoringConfigKind, ScoringConfiguration
from app.models.talent import Resource, ResourceSkill
from app.repositories.talent import DocumentRepository, ResourceRepository
from app.services.audit import AuditService
from app.services.documents import work_authorisation_state
from app.services.resources import ResourceService


def _score_of(result: MatchResult, key: str) -> float | None:
    """Column value for one component, or NULL when it was not assessable."""
    component = result.component(key)
    return component.score if component else None


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
        # has a configuration to read.
        return await self.create(
            kind, name=f"{kind.value} v1", payload=default_payload(), activate=True
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
        rows = await self.session.execute(
            select(RequirementSkill)
            .where(RequirementSkill.requirement_id == requirement.id)
            .options(selectinload(RequirementSkill.skill))
        )
        mandatory: list[str] = []
        preferred: list[str] = []
        required_years: dict[str, int | None] = {}
        technologies: set[str] = set()

        for link in rows.scalars().all():
            name = link.skill.name
            required_years[name] = link.min_years
            if link.importance is SkillImportance.MANDATORY:
                mandatory.append(name)
            else:
                preferred.append(name)
            family = SKILL_VOCABULARY.get(name, (None, []))[0]
            if family in SKILL_TO_TECHNOLOGY:
                technologies.add(SKILL_TO_TECHNOLOGY[family])

        return RequirementView(
            id=requirement.id,
            title=requirement.title,
            mandatory_skills=mandatory,
            preferred_skills=preferred,
            required_years=required_years,
            technologies=technologies,
            experience_min_years=requirement.experience_min_years,
            country=requirement.country,
            location=requirement.location,
            work_mode=requirement.work_mode.value if requirement.work_mode else None,
            start_by_date=requirement.start_by_date,
            rate_max=requirement.rate_max or requirement.rate_min,
            rate_unit=requirement.rate_unit.value if requirement.rate_unit else None,
            positions=requirement.positions,
        )

    async def _resource_views(
        self, resources: list[Resource], *, today: date
    ) -> list[ResourceView]:
        """One batched query for skills; documents come from the eager load."""
        if not resources:
            return []

        ids = [resource.id for resource in resources]
        rows = await self.session.execute(
            select(ResourceSkill)
            .where(ResourceSkill.resource_id.in_(ids))
            .options(selectinload(ResourceSkill.skill))
        )
        by_resource: dict[uuid.UUID, list[ResourceSkill]] = {}
        for link in rows.scalars().all():
            by_resource.setdefault(link.resource_id, []).append(link)

        views: list[ResourceView] = []
        for resource in resources:
            links = by_resource.get(resource.id, [])
            skills = {link.skill.name: link.years for link in links}
            last_used = {link.skill.name: link.last_used_year for link in links}

            technologies: set[str] = set()
            primary: set[str] = set()
            for link in links:
                family = SKILL_VOCABULARY.get(link.skill.name, (None, []))[0]
                technology = SKILL_TO_TECHNOLOGY.get(family) if family else None
                if technology:
                    technologies.add(technology)
                    if link.is_primary:
                        primary.add(technology)

            authorisation = work_authorisation_state(list(resource.documents), today=today)

            views.append(
                ResourceView(
                    id=resource.id,
                    full_name=resource.full_name,
                    skills=skills,
                    skill_last_used=last_used,
                    primary_technologies=primary,
                    technologies=technologies,
                    total_experience_years=resource.total_experience_years,
                    country=resource.current_location_country,
                    city=resource.current_location_city,
                    willing_to_relocate=resource.willing_to_relocate,
                    ready_from=ResourceService.ready_from(resource, today=today),
                    notice_period_days=resource.notice_period_days,
                    available_from=resource.available_from,
                    expected_cost=resource.expected_cost_amount,
                    expected_cost_unit=resource.expected_cost_unit,
                    work_authorisation_state=authorisation.state.value,
                    work_authorisation_days=authorisation.days_remaining,
                    needs_review=resource.is_awaiting_review,
                    availability_status=resource.availability_status.value,
                )
            )
        return views

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
        thresholds = config.payload["thresholds"]

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

        views = await self._resource_views(list(candidates), today=reference)

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


__all__ = ["MatchingService", "ScoringConfigService"]
