"""Pipeline endpoints: opportunities, submissions, interviews, communications."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import SessionDep, require
from app.core.permissions import Permission, permissions_for
from app.engines.pipeline.stages import (
    STAGE_LABELS,
    STAGE_ORDER,
    TERMINAL_STAGES,
    OpportunityDecision,
    OpportunityStage,
    next_suggested,
)
from app.models.delivery import Deployment
from app.models.demand import Requirement
from app.models.identity import User
from app.models.pipeline import (
    CommunicationChannel,
    CommunicationDirection,
    InterviewMode,
    InterviewOutcome,
    Opportunity,
    Submission,
    SubmissionStatus,
)
from app.models.talent import Resource
from app.services.pipeline import (
    CommunicationService,
    InterviewService,
    OpportunityService,
    SubmissionService,
)

opportunities_router = APIRouter(prefix="/opportunities", tags=["opportunities"])
submissions_router = APIRouter(prefix="/submissions", tags=["submissions"])
interviews_router = APIRouter(prefix="/interviews", tags=["interviews"])
communications_router = APIRouter(prefix="/communications", tags=["communications"])


# ------------------------------------------------------------------ schemas


class StageInfo(BaseModel):
    value: str
    label: str
    is_terminal: bool
    order: int


class OpportunityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    requirement_id: uuid.UUID
    requirement_title: str | None = None
    account_id: uuid.UUID | None
    stage: OpportunityStage
    stage_label: str = ""
    next_stage: OpportunityStage | None = None
    is_open: bool = True

    sales_owner_id: uuid.UUID | None
    resourcing_owner_id: uuid.UUID | None
    next_action: str | None
    next_action_due_at: datetime | None

    probability_percent: int | None
    expected_monthly_revenue: Decimal | None = None
    expected_margin_percent: float | None = None
    contract_value: Decimal | None = None
    currency: str
    restricted_fields: list[str] = Field(default_factory=list)

    decision: OpportunityDecision | None
    decision_reason: str | None
    decided_at: datetime | None
    closed_reason: str | None
    closed_at: datetime | None

    submission_count: int = 0
    created_at: datetime
    updated_at: datetime


class OpportunityCreate(BaseModel):
    requirement_id: uuid.UUID


class OpportunityUpdate(BaseModel):
    sales_owner_id: uuid.UUID | None = None
    resourcing_owner_id: uuid.UUID | None = None
    next_action: str | None = Field(default=None, max_length=255)
    next_action_due_at: datetime | None = None
    probability_percent: int | None = Field(default=None, ge=0, le=100)


class StageChange(BaseModel):
    stage: OpportunityStage
    note: str | None = None


class DecisionRequest(BaseModel):
    decision: OpportunityDecision
    reason: str | None = None


class StageHistoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_stage: OpportunityStage | None
    to_stage: OpportunityStage
    note: str | None
    user_id: uuid.UUID | None
    created_at: datetime


class SubmissionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    opportunity_id: uuid.UUID | None
    requirement_id: uuid.UUID
    requirement_title: str | None = None
    resource_id: uuid.UUID
    resource_name: str | None = None
    match_id: uuid.UUID | None
    status: SubmissionStatus
    submitted_by: uuid.UUID | None
    submitted_at: datetime | None

    proposed_bill_rate: Decimal | None = None
    proposed_bill_currency: str | None = None
    proposed_bill_unit: str | None = None
    restricted_fields: list[str] = Field(default_factory=list)

    client_feedback: str | None
    rejection_reason: str | None
    interview_count: int = 0
    #: Set once this submission has been turned into a deployment. A submission
    #: stays SELECTED forever after the handover, so status alone cannot tell a
    #: caller whether deploying is still possible -- without this the UI offers
    #: a Deploy button that can only ever return 409.
    deployment_id: uuid.UUID | None = None
    created_at: datetime


class SubmissionCreate(BaseModel):
    requirement_id: uuid.UUID
    resource_id: uuid.UUID
    status: SubmissionStatus = SubmissionStatus.SUBMITTED
    proposed_bill_rate: Decimal | None = Field(default=None, ge=0)
    proposed_bill_currency: str | None = Field(default=None, min_length=3, max_length=3)
    proposed_bill_unit: str | None = Field(default=None, max_length=16)
    cv_document_id: uuid.UUID | None = None
    note: str | None = None


class SubmissionStatusChange(BaseModel):
    status: SubmissionStatus
    note: str | None = None
    client_feedback: str | None = None
    rejection_reason: str | None = None


class DuplicateCheckResponse(BaseModel):
    is_duplicate: bool
    submission_id: uuid.UUID | None = None
    status: str | None = None
    submitted_at: datetime | None = None
    submitted_by: str | None = None


class SubmissionHistoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: SubmissionStatus | None
    to_status: SubmissionStatus
    note: str | None
    user_id: uuid.UUID | None
    created_at: datetime


class InterviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    submission_id: uuid.UUID
    resource_name: str | None = None
    scheduled_at: datetime
    duration_minutes: int
    mode: InterviewMode
    interviewer_name: str | None
    interviewer_contact_id: uuid.UUID | None
    location_or_link: str | None
    round_number: int
    outcome: InterviewOutcome
    feedback: str | None
    reminder_sent_at: datetime | None
    created_at: datetime


class InterviewCreate(BaseModel):
    submission_id: uuid.UUID
    scheduled_at: datetime
    duration_minutes: int = Field(default=60, ge=5, le=480)
    mode: InterviewMode = InterviewMode.VIDEO
    interviewer_name: str | None = Field(default=None, max_length=160)
    interviewer_contact_id: uuid.UUID | None = None
    location_or_link: str | None = Field(default=None, max_length=500)


class InterviewOutcomeRequest(BaseModel):
    outcome: InterviewOutcome
    feedback: str | None = None


class CommunicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    direction: CommunicationDirection
    channel: CommunicationChannel
    subject: str | None
    body: str | None
    to_addresses: list[str] | None
    status: str
    sent_at: datetime | None
    opportunity_id: uuid.UUID | None
    submission_id: uuid.UUID | None
    user_id: uuid.UUID | None
    created_at: datetime


class CommunicationCreate(BaseModel):
    channel: CommunicationChannel
    direction: CommunicationDirection = CommunicationDirection.OUTBOUND
    subject: str | None = Field(default=None, max_length=240)
    body: str | None = None
    to_addresses: list[str] | None = None
    cc_addresses: list[str] | None = None
    opportunity_id: uuid.UUID | None = None
    submission_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    resource_id: uuid.UUID | None = None
    #: Attempt delivery as well as logging. With EMAIL_TRANSPORT=log the row
    #: still records LOGGED rather than claiming a send that never happened.
    send: bool = False


# -------------------------------------------------------------- serializing

#: Commercial fields on the pipeline. Gated on `margin:view` like everywhere
#: else, and named when withheld rather than silently absent.
_OPPORTUNITY_MONEY = ("expected_monthly_revenue", "expected_margin_percent", "contract_value")
_SUBMISSION_MONEY = ("proposed_bill_rate", "proposed_bill_currency", "proposed_bill_unit")


async def _serialize_opportunities(
    session: AsyncSession, rows: list[Opportunity], *, actor: User
) -> list[OpportunityResponse]:
    if not rows:
        return []

    can_see_margin = Permission.FIELD_MARGIN in permissions_for(actor.role)

    titles = {
        row[0]: row[1]
        for row in await session.execute(
            select(Requirement.id, Requirement.title).where(
                Requirement.id.in_([item.requirement_id for item in rows])
            )
        )
    }
    counts = {
        row[0]: row[1]
        for row in await session.execute(
            select(Submission.opportunity_id, func.count())
            .where(Submission.opportunity_id.in_([item.id for item in rows]))
            .group_by(Submission.opportunity_id)
        )
    }

    items: list[OpportunityResponse] = []
    for opportunity in rows:
        response = OpportunityResponse.model_validate(opportunity)
        response.requirement_title = titles.get(opportunity.requirement_id)
        response.stage_label = STAGE_LABELS[opportunity.stage]
        response.next_stage = next_suggested(opportunity.stage)
        response.is_open = opportunity.stage not in TERMINAL_STAGES
        response.submission_count = counts.get(opportunity.id, 0)

        if not can_see_margin:
            response.expected_monthly_revenue = None
            response.expected_margin_percent = None
            response.contract_value = None
            response.restricted_fields = list(_OPPORTUNITY_MONEY)

        items.append(response)
    return items


async def _serialize_submissions(
    session: AsyncSession, rows: list[Submission], *, actor: User
) -> list[SubmissionResponse]:
    if not rows:
        return []

    from app.models.pipeline import Interview

    can_see_rate = Permission.FIELD_BILLING_RATE in permissions_for(actor.role)

    names = {
        row[0]: row[1]
        for row in await session.execute(
            select(Resource.id, Resource.full_name).where(
                Resource.id.in_([item.resource_id for item in rows])
            )
        )
    }
    titles = {
        row[0]: row[1]
        for row in await session.execute(
            select(Requirement.id, Requirement.title).where(
                Requirement.id.in_([item.requirement_id for item in rows])
            )
        )
    }
    interview_counts = {
        row[0]: row[1]
        for row in await session.execute(
            select(Interview.submission_id, func.count())
            .where(Interview.submission_id.in_([item.id for item in rows]))
            .group_by(Interview.submission_id)
        )
    }
    deployment_ids = {
        row[0]: row[1]
        for row in await session.execute(
            select(Deployment.submission_id, Deployment.id).where(
                Deployment.submission_id.in_([item.id for item in rows])
            )
        )
    }

    items: list[SubmissionResponse] = []
    for submission in rows:
        response = SubmissionResponse.model_validate(submission)
        response.resource_name = names.get(submission.resource_id)
        response.requirement_title = titles.get(submission.requirement_id)
        response.interview_count = interview_counts.get(submission.id, 0)
        response.deployment_id = deployment_ids.get(submission.id)

        if not can_see_rate:
            response.proposed_bill_rate = None
            response.proposed_bill_currency = None
            response.proposed_bill_unit = None
            response.restricted_fields = list(_SUBMISSION_MONEY)

        items.append(response)
    return items


# ------------------------------------------------------------ opportunities


@opportunities_router.get("/stages", response_model=list[StageInfo], summary="The pipeline stages")
async def stages(
    _: Annotated[User, Depends(require(Permission.OPPORTUNITY_READ))],
) -> list[StageInfo]:
    ordered = [*STAGE_ORDER, *sorted(TERMINAL_STAGES, key=lambda item: item.value)]
    return [
        StageInfo(
            value=stage.value,
            label=STAGE_LABELS[stage],
            is_terminal=stage in TERMINAL_STAGES,
            order=index,
        )
        for index, stage in enumerate(ordered)
    ]


@opportunities_router.get(
    "", response_model=list[OpportunityResponse], summary="The pipeline board"
)
async def board(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.OPPORTUNITY_READ))],
    mine: Annotated[bool, Query()] = False,
) -> list[OpportunityResponse]:
    rows = await OpportunityService(session).board(owner_id=actor.id if mine else None)
    return await _serialize_opportunities(session, rows, actor=actor)


@opportunities_router.post(
    "",
    response_model=OpportunityResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Open an opportunity for a requirement",
)
async def open_opportunity(
    payload: OpportunityCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.OPPORTUNITY_WRITE))],
) -> OpportunityResponse:
    opportunity = await OpportunityService(session).ensure(payload.requirement_id, actor=actor)
    return (await _serialize_opportunities(session, [opportunity], actor=actor))[0]


@opportunities_router.get("/{opportunity_id}", response_model=OpportunityResponse)
async def get_opportunity(
    opportunity_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.OPPORTUNITY_READ))],
) -> OpportunityResponse:
    opportunity = await OpportunityService(session).get(opportunity_id)
    return (await _serialize_opportunities(session, [opportunity], actor=actor))[0]


@opportunities_router.patch("/{opportunity_id}", response_model=OpportunityResponse)
async def update_opportunity(
    opportunity_id: uuid.UUID,
    payload: OpportunityUpdate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.OPPORTUNITY_WRITE))],
) -> OpportunityResponse:
    service = OpportunityService(session)
    opportunity = await service.get(opportunity_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(opportunity, key, value)
    await session.flush()
    return (await _serialize_opportunities(session, [opportunity], actor=actor))[0]


@opportunities_router.post("/{opportunity_id}/stage", response_model=OpportunityResponse)
async def change_stage(
    opportunity_id: uuid.UUID,
    payload: StageChange,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.OPPORTUNITY_WRITE))],
) -> OpportunityResponse:
    service = OpportunityService(session)
    opportunity = await service.get(opportunity_id)
    await service.change_stage(opportunity, payload.stage, actor=actor, note=payload.note)
    return (await _serialize_opportunities(session, [opportunity], actor=actor))[0]


@opportunities_router.post("/{opportunity_id}/decision", response_model=OpportunityResponse)
async def record_decision(
    opportunity_id: uuid.UUID,
    payload: DecisionRequest,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.OPPORTUNITY_WRITE))],
) -> OpportunityResponse:
    service = OpportunityService(session)
    opportunity = await service.get(opportunity_id)
    await service.record_decision(opportunity, payload.decision, reason=payload.reason, actor=actor)
    return (await _serialize_opportunities(session, [opportunity], actor=actor))[0]


@opportunities_router.get("/{opportunity_id}/history", response_model=list[StageHistoryEntry])
async def stage_history(
    opportunity_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.OPPORTUNITY_READ))],
) -> list[StageHistoryEntry]:
    rows = await OpportunityService(session).history(opportunity_id)
    return [StageHistoryEntry.model_validate(row) for row in rows]


# -------------------------------------------------------------- submissions


@submissions_router.get(
    "/check-duplicate",
    response_model=DuplicateCheckResponse,
    summary="Warn before submitting, not after",
)
async def check_duplicate(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.SUBMISSION_READ))],
    requirement_id: Annotated[uuid.UUID, Query()],
    resource_id: Annotated[uuid.UUID, Query()],
) -> DuplicateCheckResponse:
    found = await SubmissionService(session).check_duplicate(requirement_id, resource_id)
    if found is None:
        return DuplicateCheckResponse(is_duplicate=False)
    return DuplicateCheckResponse(is_duplicate=True, **found)


@submissions_router.get("", response_model=list[SubmissionResponse])
async def list_submissions(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.SUBMISSION_READ))],
    requirement_id: Annotated[uuid.UUID | None, Query()] = None,
    submission_status: Annotated[SubmissionStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[SubmissionResponse]:
    service = SubmissionService(session)
    rows = (
        await service.for_requirement(requirement_id)
        if requirement_id is not None
        else await service.list_all(status=submission_status, limit=limit)
    )
    if requirement_id is not None and submission_status is not None:
        rows = [row for row in rows if row.status is submission_status]
    return await _serialize_submissions(session, rows, actor=actor)


@submissions_router.post(
    "",
    response_model=SubmissionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Put a consultant forward",
)
async def create_submission(
    payload: SubmissionCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.SUBMISSION_WRITE))],
) -> SubmissionResponse:
    submission = await SubmissionService(session).create(
        requirement_id=payload.requirement_id,
        resource_id=payload.resource_id,
        actor=actor,
        status=payload.status,
        proposed_bill_rate=payload.proposed_bill_rate,
        proposed_bill_currency=payload.proposed_bill_currency,
        proposed_bill_unit=payload.proposed_bill_unit,
        cv_document_id=payload.cv_document_id,
        note=payload.note,
    )
    return (await _serialize_submissions(session, [submission], actor=actor))[0]


@submissions_router.get("/{submission_id}", response_model=SubmissionResponse)
async def get_submission(
    submission_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.SUBMISSION_READ))],
) -> SubmissionResponse:
    submission = await SubmissionService(session).get(submission_id)
    return (await _serialize_submissions(session, [submission], actor=actor))[0]


@submissions_router.post("/{submission_id}/status", response_model=SubmissionResponse)
async def change_submission_status(
    submission_id: uuid.UUID,
    payload: SubmissionStatusChange,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.SUBMISSION_WRITE))],
) -> SubmissionResponse:
    service = SubmissionService(session)
    submission = await service.get(submission_id)
    await service.change_status(
        submission,
        payload.status,
        actor=actor,
        note=payload.note,
        client_feedback=payload.client_feedback,
        rejection_reason=payload.rejection_reason,
    )
    return (await _serialize_submissions(session, [submission], actor=actor))[0]


@submissions_router.get("/{submission_id}/history", response_model=list[SubmissionHistoryEntry])
async def submission_history(
    submission_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.SUBMISSION_READ))],
) -> list[SubmissionHistoryEntry]:
    rows = await SubmissionService(session).history(submission_id)
    return [SubmissionHistoryEntry.model_validate(row) for row in rows]


# --------------------------------------------------------------- interviews


async def _serialize_interviews(session: AsyncSession, rows: list[Any]) -> list[InterviewResponse]:
    if not rows:
        return []

    submissions = {
        row.id: row
        for row in (
            await session.execute(
                select(Submission).where(Submission.id.in_([item.submission_id for item in rows]))
            )
        ).scalars()
    }
    names = {
        row[0]: row[1]
        for row in await session.execute(
            select(Resource.id, Resource.full_name).where(
                Resource.id.in_([item.resource_id for item in submissions.values()])
            )
        )
    }

    items: list[InterviewResponse] = []
    for interview in rows:
        response = InterviewResponse.model_validate(interview)
        submission = submissions.get(interview.submission_id)
        if submission is not None:
            response.resource_name = names.get(submission.resource_id)
        items.append(response)
    return items


@interviews_router.get("", response_model=list[InterviewResponse], summary="Upcoming interviews")
async def upcoming_interviews(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.INTERVIEW_READ))],
    days_ahead: Annotated[int, Query(ge=1, le=365)] = 30,
    submission_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[InterviewResponse]:
    service = InterviewService(session)
    rows = (
        await service.for_submission(submission_id)
        if submission_id is not None
        else await service.upcoming(days_ahead=days_ahead)
    )
    return await _serialize_interviews(session, rows)


@interviews_router.post(
    "",
    response_model=InterviewResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Schedule an interview — raises a reminder",
)
async def schedule_interview(
    payload: InterviewCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.INTERVIEW_WRITE))],
) -> InterviewResponse:
    interview = await InterviewService(session).schedule(
        submission_id=payload.submission_id,
        scheduled_at=payload.scheduled_at,
        actor=actor,
        duration_minutes=payload.duration_minutes,
        mode=payload.mode,
        interviewer_name=payload.interviewer_name,
        interviewer_contact_id=payload.interviewer_contact_id,
        location_or_link=payload.location_or_link,
    )
    return (await _serialize_interviews(session, [interview]))[0]


@interviews_router.post("/{interview_id}/outcome", response_model=InterviewResponse)
async def record_interview_outcome(
    interview_id: uuid.UUID,
    payload: InterviewOutcomeRequest,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.INTERVIEW_WRITE))],
) -> InterviewResponse:
    service = InterviewService(session)
    interview = await service.get(interview_id)
    await service.record_outcome(interview, payload.outcome, actor=actor, feedback=payload.feedback)
    return (await _serialize_interviews(session, [interview]))[0]


# ----------------------------------------------------------- communications


@communications_router.get("", response_model=list[CommunicationResponse])
async def timeline(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.COMMUNICATION_READ))],
    opportunity_id: Annotated[uuid.UUID | None, Query()] = None,
    submission_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[CommunicationResponse]:
    rows = await CommunicationService(session).timeline(
        opportunity_id=opportunity_id, submission_id=submission_id
    )
    return [CommunicationResponse.model_validate(row) for row in rows]


@communications_router.post(
    "",
    response_model=CommunicationResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Log a communication, optionally sending it",
)
async def log_communication(
    payload: CommunicationCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.COMMUNICATION_WRITE))],
) -> CommunicationResponse:
    communication = await CommunicationService(session).log(
        channel=payload.channel,
        actor=actor,
        direction=payload.direction,
        subject=payload.subject,
        body=payload.body,
        to_addresses=payload.to_addresses,
        cc_addresses=payload.cc_addresses,
        opportunity_id=payload.opportunity_id,
        submission_id=payload.submission_id,
        contact_id=payload.contact_id,
        resource_id=payload.resource_id,
        send=payload.send,
    )
    return CommunicationResponse.model_validate(communication)


__all__ = [
    "communications_router",
    "interviews_router",
    "opportunities_router",
    "submissions_router",
]
