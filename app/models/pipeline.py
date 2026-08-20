"""The sales pipeline: opportunities, submissions, interviews, communications.

AD-5: the **opportunity** is the unit of pursuit and the **submission** is the
unit of candidate. One requirement produces one opportunity; that opportunity
carries many submissions. Conflating them is why staffing tools lose track of
which CV went where.

The load-bearing constraint in this module is `submission_active_unique`: a
partial unique index that makes it impossible to submit the same consultant to
the same requirement twice while a live submission exists. Duplicate submission
is the classic staffing embarrassment — the client receives the same CV from two
of your recruiters — and the database, not the service layer, is where that has
to be prevented.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseEntity
from app.db.types import GUID, JSONType, MoneyType, StrEnumType, UTCDateTime
from app.engines.pipeline.stages import OpportunityDecision, OpportunityStage


class SubmissionStatus(StrEnum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    SHORTLISTED = "SHORTLISTED"
    INTERVIEW = "INTERVIEW"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    ON_HOLD = "ON_HOLD"


#: Statuses that still occupy the seat. A withdrawn or rejected candidate may be
#: resubmitted later — circumstances change — so only these block a duplicate.
BLOCKING_SUBMISSION_STATUSES: frozenset[SubmissionStatus] = frozenset(
    {
        SubmissionStatus.DRAFT,
        SubmissionStatus.SUBMITTED,
        SubmissionStatus.SHORTLISTED,
        SubmissionStatus.INTERVIEW,
        SubmissionStatus.SELECTED,
        SubmissionStatus.ON_HOLD,
    }
)


class InterviewMode(StrEnum):
    PHONE = "PHONE"
    VIDEO = "VIDEO"
    ONSITE = "ONSITE"
    TECHNICAL_TEST = "TECHNICAL_TEST"


class InterviewOutcome(StrEnum):
    SCHEDULED = "SCHEDULED"
    COMPLETED = "COMPLETED"
    PASSED = "PASSED"
    FAILED = "FAILED"
    NO_SHOW = "NO_SHOW"
    RESCHEDULED = "RESCHEDULED"
    CANCELLED = "CANCELLED"


class CommunicationDirection(StrEnum):
    OUTBOUND = "OUTBOUND"
    INBOUND = "INBOUND"


class CommunicationChannel(StrEnum):
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    MEETING = "MEETING"
    NOTE = "NOTE"


class CommunicationStatus(StrEnum):
    LOGGED = "LOGGED"
    QUEUED = "QUEUED"
    SENT = "SENT"
    FAILED = "FAILED"


# ---------------------------------------------------------------- opportunity


class Opportunity(BaseEntity):
    """One pursuit decision, 1:1 with a requirement."""

    __tablename__ = "opportunities"

    requirement_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    route_account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )

    stage: Mapped[OpportunityStage] = mapped_column(
        StrEnumType(OpportunityStage),
        nullable=False,
        default=OpportunityStage.REQUIREMENT_IDENTIFIED,
        index=True,
    )

    sales_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    resourcing_owner_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: What happens next, and by when. An opportunity with no next action is how
    #: a pipeline quietly stops moving.
    next_action: Mapped[str | None] = mapped_column(String(255), nullable=True)
    next_action_due_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, index=True
    )

    probability_percent: Mapped[int | None] = mapped_column(nullable=True)
    expected_monthly_revenue: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    expected_margin_percent: Mapped[float | None] = mapped_column(nullable=True)
    contract_value: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="QAR")

    #: The human answer to the score, deliberately separate from the stage: a
    #: team can decline something that scored well, and the disagreement is
    #: exactly what a post-mortem needs to see.
    decision: Mapped[OpportunityDecision | None] = mapped_column(
        StrEnumType(OpportunityDecision), nullable=True, index=True
    )
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    closed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    submissions: Mapped[list[Submission]] = relationship(
        back_populates="opportunity",
        cascade="all, delete-orphan",
        lazy="raise",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_opportunities_stage_owner", "stage", "sales_owner_id"),
        Index("ix_opportunities_due", "next_action_due_at", "stage"),
    )

    @property
    def is_open(self) -> bool:
        from app.engines.pipeline.stages import is_terminal

        return not is_terminal(self.stage)


class OpportunityStageHistory(BaseEntity):
    """Every stage move, so the funnel is countable and reversals are visible."""

    __tablename__ = "opportunity_stage_history"

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_stage: Mapped[OpportunityStage | None] = mapped_column(
        StrEnumType(OpportunityStage), nullable=True
    )
    to_stage: Mapped[OpportunityStage] = mapped_column(
        StrEnumType(OpportunityStage), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


# ---------------------------------------------------------------- submission


class Submission(BaseEntity):
    """One consultant put forward for one requirement."""

    __tablename__ = "submissions"

    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=True, index=True
    )
    requirement_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The match snapshot that justified this submission, so "why did we send
    #: this person?" is answerable months later.
    match_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("matches.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[SubmissionStatus] = mapped_column(
        StrEnumType(SubmissionStatus),
        nullable=False,
        default=SubmissionStatus.DRAFT,
        index=True,
    )

    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)

    proposed_bill_rate: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    proposed_bill_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    proposed_bill_unit: Mapped[str | None] = mapped_column(String(16), nullable=True)

    cv_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )

    client_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Set when a submission was created despite an existing one, so the
    #: relationship is recorded rather than the second one silently existing.
    duplicate_of_submission_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("submissions.id", ondelete="SET NULL"), nullable=True
    )

    #: Mirrors `status in BLOCKING_SUBMISSION_STATUSES`, maintained in the
    #: service. It exists solely so the duplicate guard can be a plain unique
    #: index — partial indexes are not portable across our two backends, and a
    #: guarantee this important must not depend on which database is running.
    blocks_resubmission: Mapped[bool | None] = mapped_column(nullable=True)

    opportunity: Mapped[Opportunity | None] = relationship(
        back_populates="submissions", lazy="raise"
    )
    interviews: Mapped[list[Interview]] = relationship(
        back_populates="submission",
        cascade="all, delete-orphan",
        lazy="raise",
        passive_deletes=True,
    )

    __table_args__ = (
        # NULL is distinct in a unique index on every backend we target, so a
        # closed submission (blocks_resubmission = NULL) never collides, while
        # two live ones for the same pair cannot both exist.
        UniqueConstraint(
            "requirement_id",
            "resource_id",
            "blocks_resubmission",
            name="submission_active_unique",
        ),
        Index("ix_submissions_status_submitted", "status", "submitted_at"),
        Index("ix_submissions_pair", "requirement_id", "resource_id"),
    )


class SubmissionHistory(BaseEntity):
    __tablename__ = "submission_history"

    submission_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[SubmissionStatus | None] = mapped_column(
        StrEnumType(SubmissionStatus), nullable=True
    )
    to_status: Mapped[SubmissionStatus] = mapped_column(
        StrEnumType(SubmissionStatus), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


# ----------------------------------------------------------------- interview


class Interview(BaseEntity):
    __tablename__ = "interviews"

    submission_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    duration_minutes: Mapped[int] = mapped_column(nullable=False, default=60)
    mode: Mapped[InterviewMode] = mapped_column(
        StrEnumType(InterviewMode), nullable=False, default=InterviewMode.VIDEO
    )

    interviewer_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    interviewer_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    location_or_link: Mapped[str | None] = mapped_column(String(500), nullable=True)
    round_number: Mapped[int] = mapped_column(nullable=False, default=1)

    outcome: Mapped[InterviewOutcome] = mapped_column(
        StrEnumType(InterviewOutcome),
        nullable=False,
        default=InterviewOutcome.SCHEDULED,
        index=True,
    )
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    submission: Mapped[Submission] = relationship(back_populates="interviews", lazy="raise")

    __table_args__ = (
        Index("ix_interviews_upcoming", "scheduled_at", "outcome"),
        UniqueConstraint("submission_id", "round_number", name="interview_round_unique"),
    )


# ------------------------------------------------------------- communication


class Communication(BaseEntity):
    """Send-and-log (SOW section 10 NEW).

    Every outbound message is recorded whether or not it was actually
    transmitted: with `EMAIL_TRANSPORT=log` the row still exists with status
    LOGGED, so the activity history is complete in every deployment shape.
    """

    __tablename__ = "communications"

    direction: Mapped[CommunicationDirection] = mapped_column(
        StrEnumType(CommunicationDirection),
        nullable=False,
        default=CommunicationDirection.OUTBOUND,
    )
    channel: Mapped[CommunicationChannel] = mapped_column(
        StrEnumType(CommunicationChannel), nullable=False, index=True
    )
    subject: Mapped[str | None] = mapped_column(String(240), nullable=True)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)

    to_addresses: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)
    cc_addresses: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)

    status: Mapped[CommunicationStatus] = mapped_column(
        StrEnumType(CommunicationStatus),
        nullable=False,
        default=CommunicationStatus.LOGGED,
        index=True,
    )
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=True, index=True
    )
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("submissions.id", ondelete="CASCADE"), nullable=True, index=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("contacts.id", ondelete="SET NULL"), nullable=True
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("resources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (Index("ix_communications_timeline", "opportunity_id", "created_at"),)


__all__ = [
    "BLOCKING_SUBMISSION_STATUSES",
    "Communication",
    "CommunicationChannel",
    "CommunicationDirection",
    "CommunicationStatus",
    "Interview",
    "InterviewMode",
    "InterviewOutcome",
    "Opportunity",
    "OpportunityStageHistory",
    "Submission",
    "SubmissionHistory",
    "SubmissionStatus",
]
