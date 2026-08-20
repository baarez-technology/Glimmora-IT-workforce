"""Role-aware dashboards.

Four views over the same data, because the four roles ask different questions:

* **Management** — is the business making money, and is it growing?
* **Sales** — what do I have to move today?
* **Resourcing** — who is unbilled, and what expires soon?
* **Admin** — is the system healthy and being used correctly?

Each is assembled from the numbers the phases before it already record. Nothing
here computes a new business figure: a dashboard that disagrees with the screen
it summarises is worse than no dashboard.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.types import utcnow
from app.engines.pipeline.stages import (
    STAGE_LABELS,
    STAGE_ORDER,
    TERMINAL_STAGES,
    OpportunityStage,
)
from app.models.accounts import Account
from app.models.delivery import Deployment, DeploymentStatus
from app.models.demand import Requirement
from app.models.identity import AuditLog, User
from app.models.matching import Match
from app.models.notifications import Notification
from app.models.pipeline import (
    BLOCKING_SUBMISSION_STATUSES,
    Interview,
    InterviewOutcome,
    Opportunity,
    Submission,
)
from app.models.scoring import OpportunityScore
from app.models.talent import AvailabilityStatus, Resource
from app.repositories.talent import DocumentRepository
from app.services.delivery import BillingService
from app.services.documents import expiry_status


async def _count(session: AsyncSession, stmt: Any) -> int:
    return (await session.execute(stmt)).scalar() or 0


class DashboardService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.billing = BillingService(session)

    # ------------------------------------------------------------- funnel
    async def funnel(self) -> dict[str, Any]:
        """Requirement through to billing, counted from the real records.

        Stage counts come from `opportunities`, not from a derived guess, so the
        funnel reconciles with the pipeline board exactly.
        """
        rows = await self.session.execute(
            select(Opportunity.stage, func.count()).group_by(Opportunity.stage)
        )
        by_stage: dict[OpportunityStage, int] = {row[0]: row[1] for row in rows}

        requirements = await _count(
            self.session,
            select(func.count()).select_from(Requirement).where(Requirement.is_active.is_(True)),
        )

        def rung(stage: OpportunityStage) -> dict[str, Any]:
            return {
                "stage": stage.value,
                "label": STAGE_LABELS[stage],
                "count": by_stage.get(stage, 0),
            }

        stages = [rung(stage) for stage in STAGE_ORDER]
        closed = [rung(stage) for stage in sorted(TERMINAL_STAGES, key=lambda item: item.value)]

        open_total = sum(by_stage.get(stage, 0) for stage in STAGE_ORDER)
        deployed = by_stage.get(OpportunityStage.DEPLOYED, 0)

        return {
            "active_requirements": requirements,
            "stages": stages,
            "closed": closed,
            "open_total": open_total,
            # Deliberately not a "win rate": with a young pipeline that number
            # would be noise presented as insight.
            "reached_deployment": deployed,
        }

    # --------------------------------------------------------- management
    async def management(self) -> dict[str, Any]:
        headline = await self.billing.headline()
        trend = await self.billing.monthly_summary(months=6)

        active_deployments = await _count(
            self.session,
            select(func.count())
            .select_from(Deployment)
            .where(Deployment.status == DeploymentStatus.ACTIVE),
        )
        bench = await _count(
            self.session,
            select(func.count())
            .select_from(Resource)
            .where(
                Resource.availability_status == AvailabilityStatus.AVAILABLE,
                Resource.review_status == "ACCEPTED",
            ),
        )
        accounts = await _count(
            self.session,
            select(func.count()).select_from(Account).where(Account.deleted_at.is_(None)),
        )

        scored = await self.session.execute(
            select(OpportunityScore.band, func.count())
            .where(OpportunityScore.is_current.is_(True))
            .group_by(OpportunityScore.band)
        )

        return {
            "headline": headline,
            "trend": trend,
            "active_deployments": active_deployments,
            "bench_count": bench,
            "accounts": accounts,
            "opportunity_bands": {band.value: count for band, count in scored},
            "funnel": await self.funnel(),
        }

    # -------------------------------------------------------------- sales
    async def sales(self, *, actor: User) -> dict[str, Any]:
        now = utcnow()

        mine = await _count(
            self.session,
            select(func.count())
            .select_from(Opportunity)
            .where(
                Opportunity.sales_owner_id == actor.id,
                Opportunity.stage.not_in(list(TERMINAL_STAGES)),
            ),
        )
        overdue = await _count(
            self.session,
            select(func.count())
            .select_from(Opportunity)
            .where(
                Opportunity.stage.not_in(list(TERMINAL_STAGES)),
                Opportunity.next_action_due_at.is_not(None),
                Opportunity.next_action_due_at < now,
            ),
        )
        unowned = await _count(
            self.session,
            select(func.count())
            .select_from(Opportunity)
            .where(
                Opportunity.stage.not_in(list(TERMINAL_STAGES)),
                Opportunity.sales_owner_id.is_(None),
            ),
        )
        live_submissions = await _count(
            self.session,
            select(func.count())
            .select_from(Submission)
            .where(Submission.status.in_(list(BLOCKING_SUBMISSION_STATUSES))),
        )
        upcoming_interviews = await _count(
            self.session,
            select(func.count())
            .select_from(Interview)
            .where(
                Interview.outcome == InterviewOutcome.SCHEDULED,
                Interview.scheduled_at >= now,
                Interview.scheduled_at <= now + timedelta(days=14),
            ),
        )

        # Requirements with a live SLA clock. The board that needs action today.
        deadlines = await _count(
            self.session,
            select(func.count())
            .select_from(Requirement)
            .where(
                Requirement.is_active.is_(True),
                Requirement.response_deadline_at.is_not(None),
                Requirement.response_deadline_at >= now,
                Requirement.response_deadline_at <= now + timedelta(days=2),
            ),
        )

        top = await self.session.execute(
            select(
                OpportunityScore.requirement_id,
                OpportunityScore.opportunity_score,
                OpportunityScore.band,
                Requirement.title,
            )
            .join(Requirement, Requirement.id == OpportunityScore.requirement_id)
            .where(OpportunityScore.is_current.is_(True))
            .order_by(OpportunityScore.opportunity_score.desc())
            .limit(5)
        )

        return {
            "my_open_opportunities": mine,
            "overdue_next_actions": overdue,
            "unowned_opportunities": unowned,
            "live_submissions": live_submissions,
            "interviews_next_14_days": upcoming_interviews,
            "sla_due_within_48h": deadlines,
            "top_opportunities": [
                {
                    "requirement_id": str(requirement_id),
                    "title": title,
                    "score": float(score),
                    "band": band.value,
                }
                for requirement_id, score, band, title in top
            ],
        }

    # ---------------------------------------------------------------- hr
    async def hr(self) -> dict[str, Any]:
        today = utcnow().date()

        total = await _count(
            self.session,
            select(func.count())
            .select_from(Resource)
            .where(Resource.deleted_at.is_(None), Resource.review_status == "ACCEPTED"),
        )
        bench = await _count(
            self.session,
            select(func.count())
            .select_from(Resource)
            .where(
                Resource.availability_status == AvailabilityStatus.AVAILABLE,
                Resource.review_status == "ACCEPTED",
            ),
        )
        deployed = await _count(
            self.session,
            select(func.count())
            .select_from(Resource)
            .where(Resource.availability_status == AvailabilityStatus.DEPLOYED),
        )
        awaiting_review = await _count(
            self.session,
            select(func.count())
            .select_from(Resource)
            .where(Resource.review_status == "PENDING_REVIEW", Resource.deleted_at.is_(None)),
        )

        ending = await _count(
            self.session,
            select(func.count())
            .select_from(Deployment)
            .where(
                Deployment.status == DeploymentStatus.ACTIVE,
                Deployment.end_date.is_not(None),
                Deployment.end_date <= today + timedelta(days=30),
            ),
        )

        # Reuse the same repository the expiry board reads, so the tile and
        # the screen it links to can never disagree.
        documents = await DocumentRepository(self.session).expiring(
            before=today + timedelta(days=60)
        )
        expired = [
            doc for doc in documents if expiry_status(doc.expiry_date, today=today).is_expired
        ]

        # Bench consultants with no reverse-match suggestion recorded. The
        # number Phase 8 exists to drive to zero.
        with_suggestions = select(Match.resource_id).where(Match.direction == "RESOURCE_TO_DEMAND")
        uncovered = await _count(
            self.session,
            select(func.count())
            .select_from(Resource)
            .where(
                Resource.availability_status == AvailabilityStatus.AVAILABLE,
                Resource.review_status == "ACCEPTED",
                Resource.id.not_in(with_suggestions),
            ),
        )

        return {
            "total_resources": total,
            "bench_count": bench,
            "deployed_count": deployed,
            "awaiting_review": awaiting_review,
            "deployments_ending_30d": ending,
            "bench_without_a_suggestion": uncovered,
            "documents_expired": len(expired),
            "documents_expiring_soon": len(documents) - len(expired),
        }

    # -------------------------------------------------------------- admin
    async def admin(self) -> dict[str, Any]:
        now = utcnow()

        users = await _count(
            self.session,
            select(func.count()).select_from(User).where(User.is_active.is_(True)),
        )
        recent_audit = await _count(
            self.session,
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.created_at >= now - timedelta(days=7)),
        )
        unread = await _count(
            self.session,
            select(func.count()).select_from(Notification).where(Notification.is_read.is_(False)),
        )

        by_action = await self.session.execute(
            select(AuditLog.action, func.count())
            .where(AuditLog.created_at >= now - timedelta(days=7))
            .group_by(AuditLog.action)
            .order_by(func.count().desc())
            .limit(8)
        )

        # Data-quality signals: a scoring engine is only as good as what has
        # been recorded, so the gaps are surfaced rather than hidden.
        unscored = await _count(
            self.session,
            select(func.count())
            .select_from(Requirement)
            .where(
                Requirement.is_active.is_(True),
                Requirement.id.not_in(
                    select(OpportunityScore.requirement_id).where(
                        OpportunityScore.is_current.is_(True)
                    )
                ),
            ),
        )
        unpriced = await _count(
            self.session,
            select(func.count())
            .select_from(Requirement)
            .where(
                Requirement.is_active.is_(True),
                Requirement.rate_max.is_(None),
                Requirement.rate_min.is_(None),
            ),
        )

        return {
            "active_users": users,
            "audit_events_7d": recent_audit,
            "unread_notifications": unread,
            "top_actions_7d": [
                {"action": action.value, "count": count} for action, count in by_action
            ],
            "active_requirements_unscored": unscored,
            "active_requirements_unpriced": unpriced,
        }


__all__ = ["DashboardService"]
