"""Notifications: the inbox, and the sweeps that fill it.

Phase 8 built the table for the zero-bench alert. This completes the set — SLA
deadlines, document expiry, overdue follow-ups and project endings — under one
rule that every sweep obeys:

**A fact alerts once.** Every alert carries a `dedupe_key` derived from the fact
it describes, never from the run that found it. A sweep that re-raises the same
warning every morning trains people to dismiss the whole notification system,
which costs more than the alert was ever worth.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.logging import get_logger, log_business_event
from app.core.permissions import Role
from app.db.types import utcnow
from app.models.accounts import Project, ProjectStatus
from app.models.demand import Requirement
from app.models.identity import User
from app.models.notifications import (
    Notification,
    NotificationCategory,
    NotificationSeverity,
)
from app.models.pipeline import Opportunity
from app.repositories.talent import DocumentRepository
from app.services.documents import expiry_status, reminder_milestone

logger = get_logger("notifications")

#: SLA milestones in hours. A VMS window is 24-48 hours, so an alert at 24 hours
#: out is a planning note and one at 2 hours is an emergency.
SLA_MILESTONES: list[int] = [48, 24, 8, 2]

SLA_SEVERITY: dict[int, NotificationSeverity] = {
    48: NotificationSeverity.INFO,
    24: NotificationSeverity.WARNING,
    8: NotificationSeverity.WARNING,
    2: NotificationSeverity.CRITICAL,
}

#: Days before a project ends that Sales should be thinking about the next one.
PROJECT_ENDING_MILESTONES: list[int] = [60, 30, 14]


def sla_milestone(hours_remaining: float) -> int | None:
    """The tightest SLA milestone a countdown has reached.

    Tightest rather than exact, so an hourly sweep that misses a run still
    fires the milestone it passed through.
    """
    if hours_remaining < 0:
        return None
    reached = [m for m in sorted(SLA_MILESTONES) if hours_remaining <= m]
    return reached[0] if reached else None


class NotificationService:
    """The inbox. Reads are scoped to the caller — always."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _visible_to(self, actor: User) -> Any:
        """Addressed to this person, or broadcast to their role."""
        return or_(
            Notification.user_id == actor.id,
            Notification.role_target == actor.role,
        )

    async def inbox(
        self,
        actor: User,
        *,
        unread_only: bool = False,
        category: NotificationCategory | None = None,
        limit: int = 50,
    ) -> list[Notification]:
        stmt = select(Notification).where(self._visible_to(actor))
        if unread_only:
            stmt = stmt.where(Notification.is_read.is_(False))
        if category is not None:
            stmt = stmt.where(Notification.category == category)
        stmt = stmt.order_by(Notification.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def unread_count(self, actor: User) -> dict[str, Any]:
        rows = await self.session.execute(
            select(Notification.category, Notification.severity, func.count())
            .where(self._visible_to(actor), Notification.is_read.is_(False))
            .group_by(Notification.category, Notification.severity)
        )

        total = 0
        critical = 0
        by_category: dict[str, int] = {}
        for category, severity, count in rows:
            total += count
            by_category[category.value] = by_category.get(category.value, 0) + count
            if severity is NotificationSeverity.CRITICAL:
                critical += count

        return {"total": total, "critical": critical, "by_category": by_category}

    async def mark_read(self, notification_id: uuid.UUID, *, actor: User) -> Notification:
        notification = (
            (
                await self.session.execute(
                    select(Notification).where(
                        Notification.id == notification_id, self._visible_to(actor)
                    )
                )
            )
            .scalars()
            .first()
        )
        # A 404 rather than a 403: revealing that somebody else's notification
        # exists is itself a small leak.
        if notification is None:
            raise NotFoundError("notification", notification_id)

        notification.mark_read(when=utcnow())
        await self.session.flush()
        return notification

    async def mark_all_read(self, actor: User) -> int:
        rows = await self.session.execute(
            select(Notification).where(self._visible_to(actor), Notification.is_read.is_(False))
        )
        now = utcnow()
        count = 0
        for notification in rows.scalars().all():
            notification.mark_read(when=now)
            count += 1
        await self.session.flush()
        return count

    # ------------------------------------------------------------- raising
    async def raise_once(
        self,
        *,
        dedupe_key: str,
        category: NotificationCategory,
        severity: NotificationSeverity,
        title: str,
        body: str | None = None,
        user_id: uuid.UUID | None = None,
        role_target: Role | None = None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        action_url: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Notification | None:
        """Create the alert, or return None if this fact already alerted."""
        existing = (
            await self.session.execute(
                select(Notification.id).where(Notification.dedupe_key == dedupe_key)
            )
        ).scalar()
        if existing is not None:
            return None

        notification = Notification(
            user_id=user_id,
            role_target=role_target,
            category=category,
            severity=severity,
            title=title,
            body=body,
            entity_type=entity_type,
            entity_id=entity_id,
            action_url=action_url,
            payload=payload,
            dedupe_key=dedupe_key,
        )
        self.session.add(notification)
        await self.session.flush()
        log_business_event("notification_sent", category=category.value, severity=severity.value)
        return notification


class NotificationSweeps:
    """The scheduled producers. Each is idempotent by dedupe key."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.notifications = NotificationService(session)

    # --------------------------------------------------- submission SLA
    async def sweep_submission_sla(self, *, now: datetime | None = None) -> dict[str, int]:
        """Requirements with a response deadline approaching.

        Runs hourly, because a VMS window can be 24 hours and a daily sweep
        would routinely notice it on the day it closed.
        """
        reference = now or utcnow()
        horizon = reference + timedelta(hours=max(SLA_MILESTONES))

        rows = await self.session.execute(
            select(Requirement).where(
                Requirement.is_active.is_(True),
                Requirement.response_deadline_at.is_not(None),
                Requirement.response_deadline_at > reference,
                Requirement.response_deadline_at <= horizon,
            )
        )

        examined = raised = 0
        for requirement in rows.scalars().all():
            examined += 1
            deadline = requirement.response_deadline_at
            if deadline is None:
                continue

            hours = (deadline - reference).total_seconds() / 3600
            milestone = sla_milestone(hours)
            if milestone is None:
                continue

            created = await self.notifications.raise_once(
                dedupe_key=f"sla:{requirement.id}:{milestone}",
                category=NotificationCategory.SUBMISSION_SLA,
                severity=SLA_SEVERITY.get(milestone, NotificationSeverity.WARNING),
                title=f"{requirement.title} — {milestone}h to submit",
                body=(
                    f"The submission window closes {deadline:%d %b %Y at %H:%M} UTC. "
                    f"{'Submit now.' if milestone <= 8 else 'Shortlist and submit.'}"
                ),
                user_id=requirement.owner_id,
                role_target=Role.SALES,
                entity_type="requirement",
                entity_id=requirement.id,
                action_url=f"/demand/requirements/{requirement.id}",
                payload={"milestone_hours": milestone, "deadline": deadline.isoformat()},
            )
            if created is not None:
                raised += 1

        log_business_event("notification_sent", sweep="submission_sla", raised=raised)
        return {"examined": examined, "raised": raised}

    # ------------------------------------------------- document expiry
    async def sweep_document_expiry(self, *, today: date | None = None) -> dict[str, int]:
        """Visas and work permits approaching expiry.

        An expired work permit stops a consultant working, which stops billing
        on a live deployment — so this is revenue protection, not administration.
        """
        reference = today or utcnow().date()
        documents = await DocumentRepository(self.session).expiring(
            before=reference + timedelta(days=90), work_authorisation_only=False
        )

        examined = raised = 0
        for document in documents:
            examined += 1
            status = expiry_status(document.expiry_date, today=reference)
            if status.days_remaining is None:
                continue

            milestone = 0 if status.is_expired else reminder_milestone(status.days_remaining)
            if milestone is None:
                continue

            severity = (
                NotificationSeverity.CRITICAL
                if status.is_expired or milestone <= 15
                else NotificationSeverity.WARNING
            )
            headline = (
                f"expired on {document.expiry_date:%d %b %Y}"
                if status.is_expired
                else f"expires in {status.days_remaining} days"
            )

            created = await self.notifications.raise_once(
                dedupe_key=f"document:{document.id}:{milestone}",
                category=NotificationCategory.DOCUMENT_EXPIRY,
                severity=severity,
                title=f"{document.doc_type.value.replace('_', ' ').title()} {headline}",
                body=(
                    "An expired work authorisation blocks deployment and stops billing. "
                    "Renew it or the consultant cannot be placed."
                    if status.is_expired
                    else "Start the renewal before it blocks a placement."
                ),
                role_target=Role.HR_RESOURCING,
                entity_type="resource_document",
                entity_id=document.id,
                action_url="/talent/documents",
                payload={
                    "resource_id": str(document.resource_id),
                    "expiry_date": document.expiry_date.isoformat()
                    if document.expiry_date
                    else None,
                    "milestone_days": milestone,
                },
            )
            if created is not None:
                raised += 1

        log_business_event("notification_sent", sweep="document_expiry", raised=raised)
        return {"examined": examined, "raised": raised}

    # ------------------------------------------------ overdue follow-ups
    async def sweep_follow_up_overdue(self, *, now: datetime | None = None) -> dict[str, int]:
        """Opportunities whose next action has slipped.

        Deduped on the due date rather than the opportunity, so rescheduling the
        action produces a fresh alert when *that* one slips — but the same
        missed date never nags twice.
        """
        reference = now or utcnow()

        rows = await self.session.execute(
            select(Opportunity).where(
                Opportunity.next_action_due_at.is_not(None),
                Opportunity.next_action_due_at < reference,
                Opportunity.stage.not_in(["LOST", "DROPPED"]),
            )
        )

        examined = raised = 0
        for opportunity in rows.scalars().all():
            examined += 1
            due = opportunity.next_action_due_at
            if due is None:
                continue

            overdue_days = (reference - due).days
            created = await self.notifications.raise_once(
                dedupe_key=f"followup:{opportunity.id}:{due:%Y-%m-%d}",
                category=NotificationCategory.FOLLOW_UP_OVERDUE,
                severity=(
                    NotificationSeverity.CRITICAL
                    if overdue_days >= 3
                    else NotificationSeverity.WARNING
                ),
                title=f"Overdue: {opportunity.next_action or 'next action'}",
                body=(
                    f"Due {due:%d %b %Y}"
                    + (f", {overdue_days} days ago" if overdue_days > 0 else "")
                    + ". A pipeline stops moving one missed follow-up at a time."
                ),
                user_id=opportunity.sales_owner_id,
                role_target=Role.SALES,
                entity_type="opportunity",
                entity_id=opportunity.id,
                action_url="/sales/pipeline",
                payload={"due_at": due.isoformat(), "overdue_days": overdue_days},
            )
            if created is not None:
                raised += 1

        log_business_event("notification_sent", sweep="follow_up_overdue", raised=raised)
        return {"examined": examined, "raised": raised}

    # -------------------------------------------------- project ending
    async def sweep_project_ending(self, *, today: date | None = None) -> dict[str, int]:
        """Client projects approaching their end — a renewal conversation."""
        reference = today or utcnow().date()
        horizon = reference + timedelta(days=max(PROJECT_ENDING_MILESTONES))

        rows = await self.session.execute(
            select(Project).where(
                Project.status == ProjectStatus.ACTIVE,
                Project.end_date.is_not(None),
                Project.end_date >= reference,
                Project.end_date <= horizon,
            )
        )

        examined = raised = 0
        for project in rows.scalars().all():
            examined += 1
            if project.end_date is None:
                continue

            days = (project.end_date - reference).days
            reached = [m for m in sorted(PROJECT_ENDING_MILESTONES) if days <= m]
            if not reached:
                continue
            milestone = reached[0]

            created = await self.notifications.raise_once(
                dedupe_key=f"project:{project.id}:{milestone}",
                category=NotificationCategory.PROJECT_ENDING,
                severity=(
                    NotificationSeverity.WARNING if milestone <= 30 else NotificationSeverity.INFO
                ),
                title=f"{project.name} ends in {days} days",
                body="Open the renewal or extension conversation before it closes.",
                role_target=Role.SALES,
                entity_type="project",
                entity_id=project.id,
                action_url="/accounts/projects",
                payload={"end_date": project.end_date.isoformat(), "milestone_days": milestone},
            )
            if created is not None:
                raised += 1

        log_business_event("notification_sent", sweep="project_ending", raised=raised)
        return {"examined": examined, "raised": raised}

    async def run_all(self, *, now: datetime | None = None) -> dict[str, dict[str, int]]:
        reference = now or utcnow()
        return {
            "submission_sla": await self.sweep_submission_sla(now=reference),
            "document_expiry": await self.sweep_document_expiry(today=reference.date()),
            "follow_up_overdue": await self.sweep_follow_up_overdue(now=reference),
            "project_ending": await self.sweep_project_ending(today=reference.date()),
        }


__all__ = [
    "PROJECT_ENDING_MILESTONES",
    "SLA_MILESTONES",
    "SLA_SEVERITY",
    "NotificationService",
    "NotificationSweeps",
    "sla_milestone",
]
