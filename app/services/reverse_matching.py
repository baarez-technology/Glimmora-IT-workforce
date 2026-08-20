"""Reverse matching and the zero-bench sweep.

The forward service asks "who fills this seat?". This one asks "where does this
person go next?", and the sweep asks it *on a schedule* so nobody has to
remember to. A consultant reaching the bench unnoticed is the most expensive
routine failure a staffing business has.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.logging import get_logger, log_business_event
from app.core.permissions import Role
from app.db.types import utcnow
from app.engines.matching.bench import BenchMilestone, evaluate_resource
from app.engines.matching.engine import ENGINE_VERSION
from app.engines.matching.reverse import (
    RedeploymentSuggestion,
    RouteType,
    rank_suggestions,
    resolve_route,
)
from app.models.demand import Requirement
from app.models.identity import AuditAction, User
from app.models.matching import Match, MatchDirection, ScoringConfigKind
from app.models.notifications import (
    Notification,
    NotificationCategory,
    NotificationSeverity,
)
from app.models.talent import AvailabilityStatus, Resource
from app.repositories.talent import ResourceRepository
from app.services.audit import AuditService
from app.services.matching import ScoringConfigService, _score_of, effective_thresholds
from app.services.matching_views import (
    build_account_views,
    build_preferred_routes,
    build_requirement_views,
    build_resource_views,
)

logger = get_logger("reverse_matching")


class ReverseMatchingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.resources = ResourceRepository(session)
        self.configs = ScoringConfigService(session)
        self.audit = AuditService(session)

    # -------------------------------------------------------------- loading
    async def _open_requirements(self) -> list[Requirement]:
        """Every requirement worth suggesting.

        Filtered in SQL rather than in Python: on a real pipeline most
        requirements are closed, and loading them all to discard them is the
        difference between one query and a slow screen.
        """
        rows = await self.session.execute(
            select(Requirement).where(
                Requirement.is_active.is_(True),
                Requirement.review_status == "ACCEPTED",
            )
        )
        return [requirement for requirement in rows.scalars().all() if requirement.is_open]

    # ------------------------------------------------------------ suggesting
    async def suggest_for_resource(
        self,
        resource: Resource,
        *,
        limit: int | None = None,
        today: date | None = None,
    ) -> list[RedeploymentSuggestion]:
        """Rank open requirements for one resource. Computes, does not persist."""
        reference = today or utcnow().date()
        config = await self.configs.active(ScoringConfigKind.MATCH_WEIGHTS)
        weights = config.payload["weights"]
        thresholds = effective_thresholds(config)

        resource_views = await build_resource_views(self.session, [resource], today=reference)
        if not resource_views:
            return []

        requirements = await self._open_requirements()
        if not requirements:
            return []

        requirement_views = await build_requirement_views(self.session, requirements)

        # Route resolution needs the end customer and any intermediary, so both
        # sides of every route are loaded in one pass.
        account_ids = {r.account_id for r in requirements if r.account_id}
        account_ids |= {r.end_customer_id for r in requirements if r.end_customer_id}
        routes = await build_preferred_routes(self.session, account_ids)
        account_ids |= {via for via, _ in routes.values()}
        account_ids |= {r.route_account_id for r in requirements if r.route_account_id}
        accounts = await build_account_views(self.session, account_ids)

        candidates = []
        for requirement in requirements:
            view = requirement_views.get(requirement.id)
            if view is None:
                continue

            account = accounts.get(requirement.account_id) if requirement.account_id else None

            # An explicit route on the requirement beats the account's general
            # one: Sales recorded it for this specific seat.
            if requirement.route_account_id:
                via = accounts.get(requirement.route_account_id)
                via_is_preferred = True
            elif account is not None:
                recorded = routes.get(account.id)
                via = accounts.get(recorded[0]) if recorded else None
                via_is_preferred = bool(recorded and recorded[1])
            else:
                via, via_is_preferred = None, False

            route = resolve_route(
                account=account,
                via=via,
                via_is_preferred=via_is_preferred,
                thresholds=thresholds,
            )
            candidates.append(
                (
                    view,
                    route,
                    account.name if account else None,
                    requirement.is_open,
                    requirement.is_awaiting_review,
                )
            )

        return rank_suggestions(
            resource_views[0],
            candidates,
            weights=weights,
            thresholds=thresholds,
            today=reference,
            limit=limit or int(thresholds.get("reverse_match_limit", 10)),
        )

    async def run_for_resource(
        self,
        resource: Resource,
        *,
        actor: User | None = None,
        limit: int | None = None,
        today: date | None = None,
    ) -> list[RedeploymentSuggestion]:
        """Compute, persist and audit. The persisted snapshot is what the UI reads."""
        suggestions = await self.suggest_for_resource(resource, limit=limit, today=today)
        config = await self.configs.active(ScoringConfigKind.MATCH_WEIGHTS)
        await self._persist(resource.id, suggestions, weights_version=config.version, actor=actor)

        if actor is not None:
            await self.audit.record(
                AuditAction.REVERSE_MATCH_GENERATED,
                summary=(
                    f"Found {len(suggestions)} next-assignment options for {resource.full_name}"
                ),
                actor=actor,
                entity_type="resource",
                entity_id=resource.id,
            )
        log_business_event(
            "reverse_match_generated",
            resource_id=str(resource.id),
            suggestions=len(suggestions),
        )
        return suggestions

    async def _persist(
        self,
        resource_id: uuid.UUID,
        suggestions: list[RedeploymentSuggestion],
        *,
        weights_version: int,
        actor: User | None,
    ) -> None:
        """Replace this resource's reverse snapshot.

        Scoped to `RESOURCE_TO_DEMAND` so it never disturbs forward matches for
        the same pair — the two directions answer different questions and are
        recomputed on different triggers.
        """
        await self.session.execute(
            delete(Match).where(
                Match.resource_id == resource_id,
                Match.direction == MatchDirection.RESOURCE_TO_DEMAND,
            )
        )

        computed_at = utcnow()
        for suggestion in suggestions:
            result = suggestion.match
            route = suggestion.route
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
                    requirement_id=suggestion.requirement_id,
                    resource_id=resource_id,
                    direction=MatchDirection.RESOURCE_TO_DEMAND,
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
                    warnings=result.warnings,
                    missing_information=suggestion.missing_information,
                    narrative=result.narrative,
                    route_type=route.route_type.value,
                    route_label=route.label,
                    route_account_id=route.via_account_id,
                    reachability=route.reachability,
                    priority_score=suggestion.priority_score,
                    weights_version=weights_version,
                    engine_version=ENGINE_VERSION,
                    computed_at=computed_at,
                    computed_by=actor.id if actor else None,
                )
            )
        await self.session.flush()

    # --------------------------------------------------------------- reading
    async def stored_suggestions(self, resource_id: uuid.UUID) -> list[Match]:
        stmt = (
            select(Match)
            .where(
                Match.resource_id == resource_id,
                Match.direction == MatchDirection.RESOURCE_TO_DEMAND,
            )
            .order_by(Match.priority_score.desc(), Match.overall_score.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def last_computed(self, resource_id: uuid.UUID) -> datetime | None:
        stmt = select(func.max(Match.computed_at)).where(
            Match.resource_id == resource_id,
            Match.direction == MatchDirection.RESOURCE_TO_DEMAND,
        )
        return (await self.session.execute(stmt)).scalar()

    async def get_resource(self, resource_id: uuid.UUID) -> Resource:
        resource = await self.resources.get(resource_id)
        if resource is None:
            raise NotFoundError("resource", resource_id)
        return resource

    # ----------------------------------------------------------- bench radar
    async def bench_radar(
        self, *, days_ahead: int = 90, today: date | None = None
    ) -> list[tuple[Resource, int | None, Match | None]]:
        """Everyone on the bench or heading for it, soonest first.

        Returns (resource, days_until_available, best stored suggestion). The
        suggestion comes from the stored snapshot rather than being recomputed,
        so opening the radar is cheap however many people are on it.
        """
        reference = today or utcnow().date()
        horizon = reference + timedelta(days=days_ahead)

        rows = await self.session.execute(
            self.resources.select().where(
                Resource.availability_status.in_(
                    [
                        AvailabilityStatus.AVAILABLE,
                        AvailabilityStatus.AVAILABLE_SOON,
                        AvailabilityStatus.DEPLOYED,
                    ]
                ),
                Resource.review_status == "ACCEPTED",
            )
        )
        resources = list(rows.scalars().unique().all())

        approaching: list[Resource] = []
        for resource in resources:
            is_free_now = resource.availability_status is AvailabilityStatus.AVAILABLE
            ends_within_horizon = (
                resource.available_from is not None and resource.available_from <= horizon
            )
            if is_free_now or ends_within_horizon:
                approaching.append(resource)

        if not approaching:
            return []

        best = await self._best_suggestions({resource.id for resource in approaching})

        board = []
        for resource in approaching:
            if resource.availability_status is AvailabilityStatus.AVAILABLE:
                days: int | None = 0
            elif resource.available_from is not None:
                days = (resource.available_from - reference).days
            else:
                days = None
            board.append((resource, days, best.get(resource.id)))

        # Soonest first; unknown dates last, because a date nobody recorded is
        # not evidence of a distant one.
        board.sort(key=lambda row: (row[1] is None, row[1] if row[1] is not None else 0))
        return board

    async def _best_suggestions(self, resource_ids: set[uuid.UUID]) -> dict[uuid.UUID, Match]:
        if not resource_ids:
            return {}
        rows = await self.session.execute(
            select(Match)
            .where(
                Match.resource_id.in_(resource_ids),
                Match.direction == MatchDirection.RESOURCE_TO_DEMAND,
            )
            .order_by(Match.priority_score.desc())
        )
        best: dict[uuid.UUID, Match] = {}
        for match in rows.scalars().all():
            best.setdefault(match.resource_id, match)
        return best


class BenchSweepService:
    """The daily milestone sweep (MATCHING.md section 2, zero-bench sweep)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.reverse = ReverseMatchingService(session)
        self.audit = AuditService(session)

    async def run(self, *, today: date | None = None, actor: User | None = None) -> dict[str, int]:
        reference = today or utcnow().date()
        config = await self.reverse.configs.active(ScoringConfigKind.MATCH_WEIGHTS)
        thresholds = effective_thresholds(config)
        milestones = list(thresholds.get("bench_milestones", [90, 60, 30, 15, 7]))
        min_priority = float(thresholds.get("bench_alert_min_priority", 45))

        rows = await self.session.execute(
            self.reverse.resources.select().where(
                Resource.availability_status.in_(
                    [AvailabilityStatus.DEPLOYED, AvailabilityStatus.AVAILABLE_SOON]
                ),
                Resource.review_status == "ACCEPTED",
                Resource.available_from.is_not(None),
            )
        )
        resources = list(rows.scalars().unique().all())

        examined = 0
        raised = 0
        skipped_duplicate = 0

        for resource in resources:
            examined += 1
            milestone = evaluate_resource(
                resource_id=resource.id,
                available_from=resource.available_from,
                availability_status=resource.availability_status.value,
                today=reference,
                milestones=milestones,
            )
            if milestone is None:
                continue

            if await self._already_raised(milestone.dedupe_key):
                skipped_duplicate += 1
                continue

            suggestions = await self.reverse.run_for_resource(resource, today=reference)
            worthwhile = [s for s in suggestions if s.priority_score >= min_priority]

            await self._raise_alert(resource, milestone, worthwhile)
            raised += 1

        if actor is not None:
            await self.audit.record(
                AuditAction.BENCH_SWEEP_RUN,
                summary=f"Bench sweep examined {examined} consultants, raised {raised} alerts",
                actor=actor,
                entity_type="system",
                entity_id=None,
            )
        log_business_event(
            "bench_sweep_complete",
            examined=examined,
            raised=raised,
            skipped_duplicate=skipped_duplicate,
        )
        return {"examined": examined, "raised": raised, "skipped_duplicate": skipped_duplicate}

    async def _already_raised(self, dedupe_key: str) -> bool:
        found = await self.session.execute(
            select(Notification.id).where(Notification.dedupe_key == dedupe_key)
        )
        return found.scalar() is not None

    async def _raise_alert(
        self,
        resource: Resource,
        milestone: BenchMilestone,
        suggestions: list[RedeploymentSuggestion],
    ) -> None:
        severity = NotificationSeverity(milestone.severity.value)

        if suggestions:
            top = suggestions[0]
            body = (
                f"{milestone.headline}. Best next seat: {top.requirement_title} "
                f"— {top.route.label} (match {top.overall_score:g}%, "
                f"priority {top.priority_score:g})."
            )
        else:
            # Silence here would be a bug, not good news: nothing found is
            # exactly when a human needs to start looking.
            body = (
                f"{milestone.headline}. No open requirement currently fits — "
                f"this consultant needs a seat found manually."
            )

        payload: dict[str, Any] = {
            "resource_id": str(resource.id),
            "milestone_days": milestone.milestone_days,
            "days_remaining": milestone.days_remaining,
            "available_on": milestone.available_on.isoformat(),
            "suggestions": [
                {
                    "requirement_id": str(item.requirement_id),
                    "requirement_title": item.requirement_title,
                    "account_name": item.account_name,
                    "route": item.route.label,
                    "route_type": item.route.route_type.value,
                    "match_score": item.overall_score,
                    "priority_score": item.priority_score,
                }
                for item in suggestions[:5]
            ],
        }

        self.session.add(
            Notification(
                role_target=Role.HR_RESOURCING,
                user_id=resource.owner_id,
                category=NotificationCategory.BENCH_REDEPLOYMENT,
                severity=severity,
                title=f"{resource.full_name} — {milestone.headline.lower()}",
                body=body,
                entity_type="resource",
                entity_id=resource.id,
                action_url=f"/intelligence/reverse-matching?resource={resource.id}",
                payload=payload,
                dedupe_key=milestone.dedupe_key,
            )
        )
        await self.session.flush()


__all__ = ["BenchSweepService", "ReverseMatchingService", "RouteType"]
