"""Pipeline services: opportunities, submissions, interviews, communications.

The rule this module exists to enforce: **a consultant is never submitted twice
to the same requirement while a live submission exists.** The database enforces
it with a unique constraint; this layer catches the collision first so the
caller gets a useful answer — who submitted, when, and what happened to it —
rather than an integrity error.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import DuplicateSubmissionError, NotFoundError, ValidationError
from app.core.logging import get_logger, log_business_event
from app.core.permissions import Role
from app.db.types import utcnow
from app.engines.pipeline.stages import (
    STAGES_REQUIRING_REASON,
    STAGES_REQUIRING_SUBMISSION,
    SUBMISSION_STAGE_HINTS,
    OpportunityDecision,
    OpportunityStage,
    can_transition,
    is_terminal,
    stage_index,
)
from app.models.accounts import Contact
from app.models.demand import Requirement
from app.models.identity import AuditAction, User
from app.models.matching import Match, MatchDirection
from app.models.notifications import (
    Notification,
    NotificationCategory,
    NotificationSeverity,
)
from app.models.pipeline import (
    BLOCKING_SUBMISSION_STATUSES,
    Communication,
    CommunicationChannel,
    CommunicationDirection,
    CommunicationStatus,
    Interview,
    InterviewOutcome,
    Opportunity,
    OpportunityStageHistory,
    Submission,
    SubmissionHistory,
    SubmissionStatus,
)
from app.models.scoring import OpportunityScore
from app.models.talent import Resource
from app.services.audit import AuditService

logger = get_logger("pipeline")


class OpportunityService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)

    async def get(self, opportunity_id: uuid.UUID) -> Opportunity:
        opportunity = (
            await self.session.execute(select(Opportunity).where(Opportunity.id == opportunity_id))
        ).scalar_one_or_none()
        if opportunity is None:
            raise NotFoundError("opportunity", opportunity_id)
        return opportunity

    async def for_requirement(self, requirement_id: uuid.UUID) -> Opportunity | None:
        return (
            (
                await self.session.execute(
                    select(Opportunity).where(Opportunity.requirement_id == requirement_id)
                )
            )
            .scalars()
            .first()
        )

    async def ensure(
        self,
        requirement_id: uuid.UUID,
        *,
        actor: User | None = None,
        stage: OpportunityStage | None = None,
    ) -> Opportunity:
        """Get or create the opportunity for a requirement.

        1:1 with the requirement, so this is idempotent. Called on demand rather
        than on requirement creation: not every captured requirement is a
        pursuit, and manufacturing an opportunity for each one would inflate the
        funnel with things nobody decided to chase.
        """
        existing = await self.for_requirement(requirement_id)
        if existing is not None:
            return existing

        requirement = (
            await self.session.execute(select(Requirement).where(Requirement.id == requirement_id))
        ).scalar_one_or_none()
        if requirement is None:
            raise NotFoundError("requirement", requirement_id)

        opportunity = Opportunity(
            requirement_id=requirement_id,
            account_id=requirement.account_id,
            route_account_id=requirement.route_account_id,
            stage=stage or OpportunityStage.REQUIREMENT_IDENTIFIED,
            sales_owner_id=requirement.owner_id or (actor.id if actor else None),
            currency=requirement.rate_currency or "QAR",
        )
        self.session.add(opportunity)
        await self.session.flush()

        # Link any score already computed for this requirement, so Phase 9's
        # snapshots stop being orphaned the moment a pursuit begins.
        await self._link_scores(opportunity)

        self.session.add(
            OpportunityStageHistory(
                opportunity_id=opportunity.id,
                from_stage=None,
                to_stage=opportunity.stage,
                note="Opportunity opened",
                user_id=actor.id if actor else None,
            )
        )

        if actor is not None:
            await self.audit.record(
                AuditAction.OPPORTUNITY_CREATED,
                summary=f"Opened an opportunity for {requirement.title}",
                actor=actor,
                entity_type="opportunity",
                entity_id=opportunity.id,
            )
        log_business_event("opportunity_created", opportunity_id=str(opportunity.id))
        await self.session.flush()
        return opportunity

    async def _link_scores(self, opportunity: Opportunity) -> None:
        scores = await self.session.execute(
            select(OpportunityScore).where(
                OpportunityScore.requirement_id == opportunity.requirement_id,
                OpportunityScore.opportunity_id.is_(None),
            )
        )
        for score in scores.scalars().all():
            score.opportunity_id = opportunity.id

    async def change_stage(
        self,
        opportunity: Opportunity,
        target: OpportunityStage,
        *,
        actor: User,
        note: str | None = None,
    ) -> Opportunity:
        allowed, reason = can_transition(opportunity.stage, target)
        if not allowed:
            raise ValidationError(
                reason or "That stage change is not allowed",
                details=[{"field": "stage", "message": reason or "not allowed"}],
            )

        if target in STAGES_REQUIRING_REASON and not (note or "").strip():
            raise ValidationError(
                "Closing an opportunity needs a reason — a lost deal with no "
                "explanation teaches nobody anything.",
                details=[{"field": "note", "message": "A reason is required"}],
            )

        if target in STAGES_REQUIRING_SUBMISSION:
            live = await self._live_submission_count(opportunity)
            if live == 0:
                raise ValidationError(
                    f"Cannot move to {target.value} without a submission on this "
                    "opportunity. Record which CV went to the client first.",
                    details=[{"field": "stage", "message": "No submission recorded"}],
                )

        previous = opportunity.stage
        opportunity.stage = target

        if is_terminal(target):
            opportunity.closed_reason = note
            opportunity.closed_at = utcnow()
        elif is_terminal(previous):
            # Reopening clears the closure rather than leaving a stale reason
            # attached to a live opportunity.
            opportunity.closed_reason = None
            opportunity.closed_at = None

        self.session.add(
            OpportunityStageHistory(
                opportunity_id=opportunity.id,
                from_stage=previous,
                to_stage=target,
                note=note,
                user_id=actor.id,
            )
        )
        await self.audit.record(
            AuditAction.OPPORTUNITY_STAGE_CHANGED,
            summary=f"{previous.value} -> {target.value}",
            actor=actor,
            entity_type="opportunity",
            entity_id=opportunity.id,
        )
        await self.session.flush()
        return opportunity

    async def record_decision(
        self,
        opportunity: Opportunity,
        decision: OpportunityDecision,
        *,
        reason: str | None,
        actor: User,
    ) -> Opportunity:
        """The human answer to the score, kept separate from the stage.

        A team may decline something that scored 91, and that disagreement is
        the most interesting row in any post-mortem — so it is recorded rather
        than collapsed into a stage change.
        """
        if decision is OpportunityDecision.DECLINE and not (reason or "").strip():
            raise ValidationError(
                "Declining needs a reason.",
                details=[{"field": "reason", "message": "A reason is required"}],
            )

        opportunity.decision = decision
        opportunity.decision_reason = reason
        opportunity.decided_by = actor.id
        opportunity.decided_at = utcnow()

        await self.audit.record(
            AuditAction.OPPORTUNITY_DECISION,
            summary=f"Decision: {decision.value}",
            actor=actor,
            entity_type="opportunity",
            entity_id=opportunity.id,
        )
        await self.session.flush()
        return opportunity

    async def _live_submission_count(self, opportunity: Opportunity) -> int:
        return (
            await self.session.execute(
                select(func.count())
                .select_from(Submission)
                .where(
                    Submission.requirement_id == opportunity.requirement_id,
                    Submission.status.in_(list(BLOCKING_SUBMISSION_STATUSES)),
                )
            )
        ).scalar() or 0

    async def board(self, *, owner_id: uuid.UUID | None = None) -> list[Opportunity]:
        stmt = select(Opportunity)
        if owner_id is not None:
            stmt = stmt.where(Opportunity.sales_owner_id == owner_id)
        stmt = stmt.order_by(Opportunity.next_action_due_at.asc().nulls_last())
        return list((await self.session.execute(stmt)).scalars().all())

    async def history(self, opportunity_id: uuid.UUID) -> list[OpportunityStageHistory]:
        rows = await self.session.execute(
            select(OpportunityStageHistory)
            .where(OpportunityStageHistory.opportunity_id == opportunity_id)
            .order_by(OpportunityStageHistory.created_at.asc())
        )
        return list(rows.scalars().all())


def _duplicate_error(existing: Submission, *, submitter: str | None) -> DuplicateSubmissionError:
    """The Phase 10 definition of done: who submitted, when, and its status.

    A bare 409 tells a recruiter nothing they can act on; this tells them
    whether to chase the existing submission or pick somebody else.
    """
    return DuplicateSubmissionError(
        submitted_at=(
            existing.submitted_at.strftime("%d %b %Y")
            if existing.submitted_at
            else "not yet submitted"
        ),
        submitted_by=submitter or "another user",
        current_status=existing.status.value,
    )


class SubmissionService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.opportunities = OpportunityService(session)

    async def get(self, submission_id: uuid.UUID) -> Submission:
        submission = (
            await self.session.execute(select(Submission).where(Submission.id == submission_id))
        ).scalar_one_or_none()
        if submission is None:
            raise NotFoundError("submission", submission_id)
        return submission

    async def find_live(
        self, requirement_id: uuid.UUID, resource_id: uuid.UUID
    ) -> Submission | None:
        """An existing submission that would block a new one."""
        return (
            (
                await self.session.execute(
                    select(Submission).where(
                        Submission.requirement_id == requirement_id,
                        Submission.resource_id == resource_id,
                        Submission.status.in_(list(BLOCKING_SUBMISSION_STATUSES)),
                    )
                )
            )
            .scalars()
            .first()
        )

    async def check_duplicate(
        self, requirement_id: uuid.UUID, resource_id: uuid.UUID
    ) -> dict[str, Any] | None:
        """Pre-flight check, so the UI can warn before the user commits."""
        existing = await self.find_live(requirement_id, resource_id)
        if existing is None:
            return None
        return {
            "submission_id": existing.id,
            "status": existing.status.value,
            "submitted_at": existing.submitted_at,
            "submitted_by": await self._submitter_name(existing),
        }

    async def _submitter_name(self, submission: Submission) -> str | None:
        if submission.submitted_by is None:
            return None
        return (
            await self.session.execute(
                select(User.full_name).where(User.id == submission.submitted_by)
            )
        ).scalar()

    async def create(
        self,
        *,
        requirement_id: uuid.UUID,
        resource_id: uuid.UUID,
        actor: User,
        status: SubmissionStatus = SubmissionStatus.SUBMITTED,
        proposed_bill_rate: Any = None,
        proposed_bill_currency: str | None = None,
        proposed_bill_unit: str | None = None,
        cv_document_id: uuid.UUID | None = None,
        note: str | None = None,
    ) -> Submission:
        requirement = (
            await self.session.execute(select(Requirement).where(Requirement.id == requirement_id))
        ).scalar_one_or_none()
        if requirement is None:
            raise NotFoundError("requirement", requirement_id)

        resource = (
            await self.session.execute(select(Resource).where(Resource.id == resource_id))
        ).scalar_one_or_none()
        if resource is None:
            raise NotFoundError("resource", resource_id)

        # AD-7 again: a profile nobody has reviewed is not business data, and
        # certainly not something to put in front of a client.
        if resource.is_awaiting_review:
            raise ValidationError(
                f"{resource.full_name}'s profile came from a parsed CV and has not "
                "been reviewed. Accept the parse before submitting them.",
                details=[{"field": "resource_id", "message": "Profile awaiting review"}],
            )

        existing = await self.find_live(requirement_id, resource_id)
        if existing is not None:
            raise _duplicate_error(existing, submitter=await self._submitter_name(existing))

        opportunity = await self.opportunities.ensure(requirement_id, actor=actor)

        match_id = (
            await self.session.execute(
                select(Match.id).where(
                    Match.requirement_id == requirement_id,
                    Match.resource_id == resource_id,
                    Match.direction == MatchDirection.DEMAND_TO_RESOURCE,
                )
            )
        ).scalar()

        submission = Submission(
            opportunity_id=opportunity.id,
            requirement_id=requirement_id,
            resource_id=resource_id,
            match_id=match_id,
            status=status,
            submitted_by=actor.id,
            submitted_at=utcnow() if status is not SubmissionStatus.DRAFT else None,
            proposed_bill_rate=proposed_bill_rate,
            proposed_bill_currency=proposed_bill_currency,
            proposed_bill_unit=proposed_bill_unit,
            cv_document_id=cv_document_id,
            blocks_resubmission=True if status in BLOCKING_SUBMISSION_STATUSES else None,
        )
        self.session.add(submission)
        await self.session.flush()

        self.session.add(
            SubmissionHistory(
                submission_id=submission.id,
                from_status=None,
                to_status=status,
                note=note or "Submission created",
                user_id=actor.id,
            )
        )

        await self._advance_opportunity(opportunity, status, actor=actor)

        await self.audit.record(
            AuditAction.CV_SUBMITTED,
            summary=f"Submitted {resource.full_name} for {requirement.title}",
            actor=actor,
            entity_type="submission",
            entity_id=submission.id,
        )
        log_business_event(
            "cv_submitted",
            submission_id=str(submission.id),
            requirement_id=str(requirement_id),
            resource_id=str(resource_id),
        )
        await self.session.flush()
        return submission

    async def change_status(
        self,
        submission: Submission,
        target: SubmissionStatus,
        *,
        actor: User,
        note: str | None = None,
        client_feedback: str | None = None,
        rejection_reason: str | None = None,
    ) -> Submission:
        if submission.status is target:
            raise ValidationError(
                "The submission is already at that status.",
                details=[{"field": "status", "message": "No change"}],
            )

        if target is SubmissionStatus.REJECTED and not (rejection_reason or note or "").strip():
            raise ValidationError(
                "A rejection needs a reason — it is the most useful feedback the "
                "pipeline produces.",
                details=[{"field": "rejection_reason", "message": "A reason is required"}],
            )

        previous = submission.status
        submission.status = target
        # Keep the duplicate guard's discriminator in step with the status.
        submission.blocks_resubmission = True if target in BLOCKING_SUBMISSION_STATUSES else None

        if target is not SubmissionStatus.DRAFT and submission.submitted_at is None:
            submission.submitted_at = utcnow()
        if client_feedback:
            submission.client_feedback = client_feedback
        if rejection_reason:
            submission.rejection_reason = rejection_reason

        self.session.add(
            SubmissionHistory(
                submission_id=submission.id,
                from_status=previous,
                to_status=target,
                note=note,
                user_id=actor.id,
            )
        )

        if submission.opportunity_id is not None:
            opportunity = await self.opportunities.get(submission.opportunity_id)
            await self._advance_opportunity(opportunity, target, actor=actor)

        await self.audit.record(
            AuditAction.SUBMISSION_STATUS_CHANGED,
            summary=f"Submission {previous.value} -> {target.value}",
            actor=actor,
            entity_type="submission",
            entity_id=submission.id,
        )
        await self.session.flush()
        return submission

    async def _advance_opportunity(
        self, opportunity: Opportunity, status: SubmissionStatus, *, actor: User
    ) -> None:
        """Pull the opportunity forward when a submission outcome implies it.

        Only ever forward, and never past a closure: a recruiter marking one
        candidate as interviewing must not drag a lost opportunity back to life,
        and must not undo a later stage reached by a different candidate.
        """
        hint = SUBMISSION_STAGE_HINTS.get(status.value)
        if hint is None or is_terminal(opportunity.stage):
            return
        if stage_index(hint) <= stage_index(opportunity.stage):
            return

        await self.opportunities.change_stage(
            opportunity,
            hint,
            actor=actor,
            note=f"Advanced automatically by a submission moving to {status.value}",
        )

    async def for_requirement(self, requirement_id: uuid.UUID) -> list[Submission]:
        rows = await self.session.execute(
            select(Submission)
            .where(Submission.requirement_id == requirement_id)
            .order_by(Submission.created_at.desc())
        )
        return list(rows.scalars().all())

    async def history(self, submission_id: uuid.UUID) -> list[SubmissionHistory]:
        rows = await self.session.execute(
            select(SubmissionHistory)
            .where(SubmissionHistory.submission_id == submission_id)
            .order_by(SubmissionHistory.created_at.asc())
        )
        return list(rows.scalars().all())

    async def list_all(
        self, *, status: SubmissionStatus | None = None, limit: int = 100
    ) -> list[Submission]:
        stmt = select(Submission)
        if status is not None:
            stmt = stmt.where(Submission.status == status)
        stmt = stmt.order_by(Submission.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())


class InterviewService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)
        self.submissions = SubmissionService(session)

    async def get(self, interview_id: uuid.UUID) -> Interview:
        interview = (
            await self.session.execute(select(Interview).where(Interview.id == interview_id))
        ).scalar_one_or_none()
        if interview is None:
            raise NotFoundError("interview", interview_id)
        return interview

    async def schedule(
        self,
        *,
        submission_id: uuid.UUID,
        scheduled_at: datetime,
        actor: User,
        duration_minutes: int = 60,
        mode: Any = None,
        interviewer_name: str | None = None,
        interviewer_contact_id: uuid.UUID | None = None,
        location_or_link: str | None = None,
    ) -> Interview:
        submission = await self.submissions.get(submission_id)

        if scheduled_at <= utcnow():
            raise ValidationError(
                "An interview cannot be scheduled in the past.",
                details=[{"field": "scheduled_at", "message": "Must be in the future"}],
            )

        if interviewer_contact_id is not None:
            exists = (
                await self.session.execute(
                    select(Contact.id).where(Contact.id == interviewer_contact_id)
                )
            ).scalar()
            if exists is None:
                raise NotFoundError("contact", interviewer_contact_id)

        rounds = (
            await self.session.execute(
                select(func.max(Interview.round_number)).where(
                    Interview.submission_id == submission_id
                )
            )
        ).scalar() or 0

        interview = Interview(
            submission_id=submission_id,
            scheduled_at=scheduled_at,
            duration_minutes=duration_minutes,
            mode=mode or "VIDEO",
            interviewer_name=interviewer_name,
            interviewer_contact_id=interviewer_contact_id,
            location_or_link=location_or_link,
            round_number=rounds + 1,
            outcome=InterviewOutcome.SCHEDULED,
            created_by=actor.id,
        )
        self.session.add(interview)
        await self.session.flush()

        # Scheduling an interview moves the submission, which in turn pulls the
        # opportunity to INTERVIEW. One action, one consistent pipeline.
        if submission.status not in {
            SubmissionStatus.INTERVIEW,
            SubmissionStatus.SELECTED,
        }:
            await self.submissions.change_status(
                submission,
                SubmissionStatus.INTERVIEW,
                actor=actor,
                note=f"Interview round {interview.round_number} scheduled",
            )

        await self._raise_reminder(interview, submission, actor=actor)

        await self.audit.record(
            AuditAction.INTERVIEW_CREATED,
            summary=f"Interview round {interview.round_number} scheduled",
            actor=actor,
            entity_type="interview",
            entity_id=interview.id,
        )
        log_business_event("interview_created", interview_id=str(interview.id))
        await self.session.flush()
        return interview

    async def _raise_reminder(
        self, interview: Interview, submission: Submission, *, actor: User
    ) -> None:
        """One reminder per interview, deduped like every other alert."""
        resource_name = (
            await self.session.execute(
                select(Resource.full_name).where(Resource.id == submission.resource_id)
            )
        ).scalar() or "the candidate"

        dedupe_key = f"interview:{interview.id}"
        already = (
            await self.session.execute(
                select(Notification.id).where(Notification.dedupe_key == dedupe_key)
            )
        ).scalar()
        if already is not None:
            return

        lead_time = interview.scheduled_at - utcnow()
        severity = (
            NotificationSeverity.WARNING
            if lead_time <= timedelta(hours=48)
            else NotificationSeverity.INFO
        )

        self.session.add(
            Notification(
                user_id=submission.submitted_by or actor.id,
                role_target=Role.SALES,
                category=NotificationCategory.INTERVIEW_REMINDER,
                severity=severity,
                title=f"Interview: {resource_name}, round {interview.round_number}",
                body=(
                    f"{interview.mode.value.title()} interview on "
                    f"{interview.scheduled_at:%d %b %Y at %H:%M} UTC"
                    + (f" with {interview.interviewer_name}" if interview.interviewer_name else "")
                    + "."
                ),
                entity_type="interview",
                entity_id=interview.id,
                action_url=f"/sales/interviews?interview={interview.id}",
                payload={
                    "submission_id": str(submission.id),
                    "scheduled_at": interview.scheduled_at.isoformat(),
                    "round_number": interview.round_number,
                },
                dedupe_key=dedupe_key,
            )
        )
        interview.reminder_sent_at = utcnow()
        await self.session.flush()

    async def record_outcome(
        self,
        interview: Interview,
        outcome: InterviewOutcome,
        *,
        actor: User,
        feedback: str | None = None,
    ) -> Interview:
        interview.outcome = outcome
        if feedback:
            interview.feedback = feedback

        submission = await self.submissions.get(interview.submission_id)

        # A failed interview does not silently close the submission — a human
        # decides whether to reject or hold — but a pass moves it forward.
        if outcome is InterviewOutcome.PASSED and submission.status not in {
            SubmissionStatus.SELECTED,
        }:
            await self.submissions.change_status(
                submission,
                SubmissionStatus.SELECTED,
                actor=actor,
                note=f"Passed interview round {interview.round_number}",
            )

        await self.audit.record(
            AuditAction.INTERVIEW_OUTCOME_RECORDED,
            summary=f"Interview outcome: {outcome.value}",
            actor=actor,
            entity_type="interview",
            entity_id=interview.id,
        )
        await self.session.flush()
        return interview

    async def upcoming(self, *, days_ahead: int = 30) -> list[Interview]:
        horizon = utcnow() + timedelta(days=days_ahead)
        rows = await self.session.execute(
            select(Interview)
            .where(
                Interview.scheduled_at <= horizon,
                Interview.outcome == InterviewOutcome.SCHEDULED,
            )
            .order_by(Interview.scheduled_at.asc())
        )
        return list(rows.scalars().all())

    async def for_submission(self, submission_id: uuid.UUID) -> list[Interview]:
        rows = await self.session.execute(
            select(Interview)
            .where(Interview.submission_id == submission_id)
            .order_by(Interview.round_number.asc())
        )
        return list(rows.scalars().all())


class CommunicationService:
    """Send-and-log.

    The log is written whether or not the message actually goes out. With
    `EMAIL_TRANSPORT=log` — the default in development and in the test suite —
    nothing is transmitted, and the row records exactly that rather than
    claiming a send that never happened.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)

    async def log(
        self,
        *,
        channel: CommunicationChannel,
        actor: User,
        direction: CommunicationDirection = CommunicationDirection.OUTBOUND,
        subject: str | None = None,
        body: str | None = None,
        to_addresses: list[str] | None = None,
        cc_addresses: list[str] | None = None,
        opportunity_id: uuid.UUID | None = None,
        submission_id: uuid.UUID | None = None,
        contact_id: uuid.UUID | None = None,
        resource_id: uuid.UUID | None = None,
        send: bool = False,
    ) -> Communication:
        status = CommunicationStatus.LOGGED
        error: str | None = None
        sent_at: datetime | None = None

        if send and channel is CommunicationChannel.EMAIL:
            if not to_addresses:
                raise ValidationError(
                    "An email needs at least one recipient.",
                    details=[{"field": "to_addresses", "message": "Required to send"}],
                )
            transport = settings.EMAIL_TRANSPORT
            if transport == "log":
                # Honest about the fallback: the record says LOGGED, not SENT.
                logger.info(
                    "email_not_transmitted",
                    reason="EMAIL_TRANSPORT=log",
                    recipients=len(to_addresses),
                )
                status = CommunicationStatus.LOGGED
            else:
                status = CommunicationStatus.QUEUED
                sent_at = utcnow()

        communication = Communication(
            direction=direction,
            channel=channel,
            subject=subject,
            body=body,
            to_addresses=to_addresses,
            cc_addresses=cc_addresses,
            status=status,
            error_detail=error,
            sent_at=sent_at,
            opportunity_id=opportunity_id,
            submission_id=submission_id,
            contact_id=contact_id,
            resource_id=resource_id,
            user_id=actor.id,
        )
        self.session.add(communication)
        await self.session.flush()
        return communication

    async def timeline(
        self,
        *,
        opportunity_id: uuid.UUID | None = None,
        submission_id: uuid.UUID | None = None,
        limit: int = 100,
    ) -> list[Communication]:
        stmt = select(Communication)
        if opportunity_id is not None:
            stmt = stmt.where(Communication.opportunity_id == opportunity_id)
        if submission_id is not None:
            stmt = stmt.where(Communication.submission_id == submission_id)
        stmt = stmt.order_by(Communication.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = [
    "CommunicationService",
    "InterviewService",
    "OpportunityService",
    "SubmissionService",
]
