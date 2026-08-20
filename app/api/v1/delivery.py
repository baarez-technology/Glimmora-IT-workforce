"""Deployments, billing and the role-aware dashboards."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import SessionDep, require
from app.core.permissions import Permission, permissions_for
from app.models.delivery import BillingStatus, Deployment, DeploymentStatus
from app.models.identity import User
from app.models.talent import Resource
from app.services.dashboards import DashboardService
from app.services.delivery import BillingService, DeploymentService

deployments_router = APIRouter(prefix="/deployments", tags=["deployments"])
billing_router = APIRouter(prefix="/billing", tags=["billing"])
dashboard_router = APIRouter(prefix="/dashboard", tags=["dashboards"])


# ------------------------------------------------------------------ schemas


class DeploymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    resource_id: uuid.UUID
    resource_name: str | None = None
    account_id: uuid.UUID | None
    requirement_id: uuid.UUID | None
    submission_id: uuid.UUID | None

    role_title: str
    location: str | None
    start_date: date
    end_date: date | None
    actual_end_date: date | None
    effective_end: date | None = None
    days_remaining: int | None = None
    status: DeploymentStatus

    bill_rate: Decimal | None = None
    bill_currency: str
    bill_unit: str
    cost_rate: Decimal | None = None
    cost_currency: str
    cost_unit: str
    restricted_fields: list[str] = Field(default_factory=list)

    working_days_per_month: int
    hours_per_day: int
    extension_of_deployment_id: uuid.UUID | None
    end_reason: str | None
    created_at: datetime


class DeploymentCreate(BaseModel):
    submission_id: uuid.UUID
    start_date: date
    end_date: date | None = None
    role_title: str | None = Field(default=None, max_length=160)
    bill_rate: Decimal | None = Field(default=None, ge=0)
    cost_rate: Decimal | None = Field(default=None, ge=0)


class DeploymentUpdate(BaseModel):
    role_title: str | None = Field(default=None, max_length=160)
    location: str | None = Field(default=None, max_length=160)
    end_date: date | None = None
    status: DeploymentStatus | None = None
    bill_rate: Decimal | None = Field(default=None, ge=0)
    cost_rate: Decimal | None = Field(default=None, ge=0)
    visa_cost: Decimal | None = Field(default=None, ge=0)
    insurance_cost: Decimal | None = Field(default=None, ge=0)
    other_cost: Decimal | None = Field(default=None, ge=0)
    working_days_per_month: int | None = Field(default=None, ge=1, le=31)
    hours_per_day: int | None = Field(default=None, ge=1, le=24)
    notes: str | None = None


class DeploymentEnd(BaseModel):
    actual_end_date: date
    reason: str | None = None


class DeploymentExtend(BaseModel):
    start_date: date
    end_date: date | None = None
    bill_rate: Decimal | None = Field(default=None, ge=0)
    cost_rate: Decimal | None = Field(default=None, ge=0)


class EndingSoonRow(BaseModel):
    deployment: DeploymentResponse
    days_remaining: int


class BillingRecordResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    deployment_id: uuid.UUID
    resource_name: str | None = None
    role_title: str | None = None
    period_year: int
    period_month: int
    period_label: str = ""

    revenue_amount: Decimal
    cost_amount: Decimal
    gross_profit: Decimal
    margin_percent: float | None
    currency: str

    status: BillingStatus
    is_estimated: bool
    billable_days: int | None
    notes: str | None
    created_at: datetime


class BillingRecordCreate(BaseModel):
    """A month entered by hand rather than projected (ASSUMPTIONS.md A15)."""

    deployment_id: uuid.UUID
    period_year: int = Field(ge=2000, le=2100)
    period_month: int = Field(ge=1, le=12)
    revenue_amount: Decimal = Field(ge=0)
    cost_amount: Decimal = Field(default=Decimal("0"), ge=0)
    status: BillingStatus = BillingStatus.CONFIRMED
    notes: str | None = None


class BillingRecordUpdate(BaseModel):
    revenue_amount: Decimal | None = Field(default=None, ge=0)
    cost_amount: Decimal | None = Field(default=None, ge=0)
    status: BillingStatus | None = None
    notes: str | None = None


class BillingConfirm(BaseModel):
    revenue_amount: Decimal | None = Field(default=None, ge=0)
    cost_amount: Decimal | None = Field(default=None, ge=0)
    notes: str | None = None


class ProjectionResult(BaseModel):
    created: int
    updated: int
    #: Periods a human had already confirmed. Left untouched, and counted so the
    #: caller can see the generator did not overwrite anybody's work.
    protected: int
    deployments: int | None = None


class MonthlySummaryRow(BaseModel):
    period: str
    year: int
    month: int
    confirmed_revenue: Decimal
    confirmed_cost: Decimal
    confirmed_profit: Decimal
    confirmed_margin_percent: float | None = None
    projected_revenue: Decimal
    projected_cost: Decimal
    projected_profit: Decimal
    records: int


class HeadlineResponse(BaseModel):
    period: str | None
    confirmed_revenue: Decimal
    projected_revenue: Decimal
    confirmed_margin_percent: float | None
    lifetime_confirmed_revenue: Decimal
    lifetime_gross_profit: Decimal
    #: Projections nobody has checked yet. Shown so the headline is never read
    #: as the whole picture.
    unconfirmed_periods: int


# -------------------------------------------------------------- serializing

_DEPLOYMENT_COST_FIELDS = ("cost_rate", "cost_currency", "cost_unit")
_DEPLOYMENT_BILL_FIELDS = ("bill_rate", "bill_currency", "bill_unit")


async def _serialize_deployments(
    session: AsyncSession, rows: list[Deployment], *, actor: User
) -> list[DeploymentResponse]:
    if not rows:
        return []

    granted = permissions_for(actor.role)
    can_see_cost = Permission.FIELD_RESOURCE_COST in granted
    can_see_bill = Permission.FIELD_BILLING_RATE in granted

    names = {
        row[0]: row[1]
        for row in await session.execute(
            select(Resource.id, Resource.full_name).where(
                Resource.id.in_([item.resource_id for item in rows])
            )
        )
    }
    today = date.today()

    items: list[DeploymentResponse] = []
    for deployment in rows:
        response = DeploymentResponse.model_validate(deployment)
        response.resource_name = names.get(deployment.resource_id)
        response.effective_end = deployment.effective_end
        response.days_remaining = (
            (deployment.effective_end - today).days if deployment.effective_end else None
        )

        withheld: list[str] = []
        if not can_see_cost:
            response.cost_rate = None
            withheld.extend(_DEPLOYMENT_COST_FIELDS)
        if not can_see_bill:
            response.bill_rate = None
            withheld.extend(_DEPLOYMENT_BILL_FIELDS)
        response.restricted_fields = withheld

        items.append(response)
    return items


async def _serialize_billing(session: AsyncSession, rows: list[Any]) -> list[BillingRecordResponse]:
    if not rows:
        return []

    deployments = {
        row.id: row
        for row in (
            await session.execute(
                select(Deployment).where(Deployment.id.in_([item.deployment_id for item in rows]))
            )
        ).scalars()
    }
    names = {
        row[0]: row[1]
        for row in await session.execute(
            select(Resource.id, Resource.full_name).where(
                Resource.id.in_([d.resource_id for d in deployments.values()])
            )
        )
    }

    items: list[BillingRecordResponse] = []
    for record in rows:
        response = BillingRecordResponse.model_validate(record)
        response.period_label = record.period_label
        deployment = deployments.get(record.deployment_id)
        if deployment is not None:
            response.role_title = deployment.role_title
            response.resource_name = names.get(deployment.resource_id)
        items.append(response)
    return items


# -------------------------------------------------------------- deployments


@deployments_router.get("/ending-soon", response_model=list[EndingSoonRow])
async def ending_soon(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.DEPLOYMENT_READ))],
    days_ahead: Annotated[int, Query(ge=1, le=365)] = 90,
) -> list[EndingSoonRow]:
    board = await DeploymentService(session).ending_soon(days_ahead=days_ahead)
    serialized = await _serialize_deployments(
        session, [deployment for deployment, _ in board], actor=actor
    )
    by_id = {item.id: item for item in serialized}
    return [
        EndingSoonRow(deployment=by_id[deployment.id], days_remaining=days)
        for deployment, days in board
        if deployment.id in by_id
    ]


@deployments_router.get("", response_model=list[DeploymentResponse])
async def list_deployments(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.DEPLOYMENT_READ))],
    deployment_status: Annotated[DeploymentStatus | None, Query(alias="status")] = None,
    account_id: Annotated[uuid.UUID | None, Query()] = None,
    resource_id: Annotated[uuid.UUID | None, Query()] = None,
    ending_before: Annotated[date | None, Query()] = None,
) -> list[DeploymentResponse]:
    rows = await DeploymentService(session).list_deployments(
        status=deployment_status,
        account_id=account_id,
        resource_id=resource_id,
        ending_before=ending_before,
    )
    return await _serialize_deployments(session, rows, actor=actor)


@deployments_router.post(
    "",
    response_model=DeploymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Deploy a selected candidate",
)
async def create_deployment(
    payload: DeploymentCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.DEPLOYMENT_WRITE))],
) -> DeploymentResponse:
    deployment = await DeploymentService(session).create_from_submission(
        payload.submission_id,
        actor=actor,
        start_date=payload.start_date,
        end_date=payload.end_date,
        role_title=payload.role_title,
        bill_rate=payload.bill_rate,
        cost_rate=payload.cost_rate,
    )
    return (await _serialize_deployments(session, [deployment], actor=actor))[0]


@deployments_router.get("/{deployment_id}", response_model=DeploymentResponse)
async def get_deployment(
    deployment_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.DEPLOYMENT_READ))],
) -> DeploymentResponse:
    deployment = await DeploymentService(session).get(deployment_id)
    return (await _serialize_deployments(session, [deployment], actor=actor))[0]


@deployments_router.patch("/{deployment_id}", response_model=DeploymentResponse)
async def update_deployment(
    deployment_id: uuid.UUID,
    payload: DeploymentUpdate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.DEPLOYMENT_WRITE))],
) -> DeploymentResponse:
    service = DeploymentService(session)
    deployment = await service.get(deployment_id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(deployment, key, value)
    await session.flush()
    return (await _serialize_deployments(session, [deployment], actor=actor))[0]


@deployments_router.post("/{deployment_id}/end", response_model=DeploymentResponse)
async def end_deployment(
    deployment_id: uuid.UUID,
    payload: DeploymentEnd,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.DEPLOYMENT_WRITE))],
) -> DeploymentResponse:
    service = DeploymentService(session)
    deployment = await service.get(deployment_id)
    await service.end(
        deployment,
        actor=actor,
        actual_end_date=payload.actual_end_date,
        reason=payload.reason,
    )
    return (await _serialize_deployments(session, [deployment], actor=actor))[0]


@deployments_router.post(
    "/{deployment_id}/extend",
    response_model=DeploymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a linked successor deployment",
)
async def extend_deployment(
    deployment_id: uuid.UUID,
    payload: DeploymentExtend,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.DEPLOYMENT_WRITE))],
) -> DeploymentResponse:
    service = DeploymentService(session)
    deployment = await service.get(deployment_id)
    successor = await service.extend(
        deployment,
        actor=actor,
        start_date=payload.start_date,
        end_date=payload.end_date,
        bill_rate=payload.bill_rate,
        cost_rate=payload.cost_rate,
    )
    return (await _serialize_deployments(session, [successor], actor=actor))[0]


# ------------------------------------------------------------------ billing


@billing_router.get("/records", response_model=list[BillingRecordResponse])
async def list_billing_records(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.BILLING_READ))],
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
    record_status: Annotated[BillingStatus | None, Query(alias="status")] = None,
    deployment_id: Annotated[uuid.UUID | None, Query()] = None,
) -> list[BillingRecordResponse]:
    rows = await BillingService(session).list_records(
        year=year, month=month, status=record_status, deployment_id=deployment_id
    )
    return await _serialize_billing(session, rows)


@billing_router.post(
    "/records",
    response_model=BillingRecordResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a month by hand",
)
async def create_billing_record(
    payload: BillingRecordCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.BILLING_WRITE))],
) -> BillingRecordResponse:
    record = await BillingService(session).create_record(
        deployment_id=payload.deployment_id,
        period_year=payload.period_year,
        period_month=payload.period_month,
        revenue_amount=payload.revenue_amount,
        cost_amount=payload.cost_amount,
        actor=actor,
        status=payload.status,
        notes=payload.notes,
    )
    return (await _serialize_billing(session, [record]))[0]


@billing_router.patch(
    "/records/{record_id}",
    response_model=BillingRecordResponse,
    summary="Correct a month — profit and margin are re-derived",
)
async def update_billing_record(
    record_id: uuid.UUID,
    payload: BillingRecordUpdate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.BILLING_WRITE))],
) -> BillingRecordResponse:
    service = BillingService(session)
    record = await service.get(record_id)
    await service.update_record(
        record,
        actor=actor,
        revenue_amount=payload.revenue_amount,
        cost_amount=payload.cost_amount,
        status=payload.status,
        notes=payload.notes,
    )
    return (await _serialize_billing(session, [record]))[0]


@billing_router.post(
    "/generate-projections",
    response_model=ProjectionResult,
    summary="Generate PROJECTED rows — never overwrites a confirmed month",
)
async def generate_projections(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.BILLING_WRITE))],
    deployment_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ProjectionResult:
    service = BillingService(session)
    if deployment_id is not None:
        deployment = await DeploymentService(session).get(deployment_id)
        return ProjectionResult(
            **await service.generate_projections(deployment, actor=actor), deployments=1
        )
    return ProjectionResult(**await service.generate_for_all(actor=actor))


@billing_router.post("/records/{record_id}/confirm", response_model=BillingRecordResponse)
async def confirm_billing_record(
    record_id: uuid.UUID,
    payload: BillingConfirm,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.BILLING_WRITE))],
) -> BillingRecordResponse:
    service = BillingService(session)
    record = await service.get(record_id)
    await service.confirm(
        record,
        actor=actor,
        revenue_amount=payload.revenue_amount,
        cost_amount=payload.cost_amount,
        notes=payload.notes,
    )
    return (await _serialize_billing(session, [record]))[0]


@billing_router.get("/summary", response_model=list[MonthlySummaryRow])
async def billing_summary(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.BILLING_READ))],
    months: Annotated[int, Query(ge=1, le=36)] = 12,
) -> list[MonthlySummaryRow]:
    rows = await BillingService(session).monthly_summary(months=months)
    return [MonthlySummaryRow(**row) for row in rows]


@billing_router.get(
    "/monthly-revenue",
    response_model=HeadlineResponse,
    summary="The headline metric — confirmed and projected kept apart",
)
async def monthly_revenue(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.BILLING_READ))],
) -> HeadlineResponse:
    return HeadlineResponse(**await BillingService(session).headline())


# --------------------------------------------------------------- dashboards


@dashboard_router.get("/funnel", summary="Requirement through to billing")
async def funnel(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.OPPORTUNITY_READ))],
) -> dict[str, Any]:
    return await DashboardService(session).funnel()


@dashboard_router.get("/management")
async def management_dashboard(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.DASHBOARD_MANAGEMENT))],
) -> dict[str, Any]:
    return await DashboardService(session).management()


@dashboard_router.get("/sales")
async def sales_dashboard(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.DASHBOARD_SALES))],
) -> dict[str, Any]:
    return await DashboardService(session).sales(actor=actor)


@dashboard_router.get("/hr")
async def hr_dashboard(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.DASHBOARD_HR))],
) -> dict[str, Any]:
    return await DashboardService(session).hr()


@dashboard_router.get("/admin")
async def admin_dashboard(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.DASHBOARD_ADMIN))],
) -> dict[str, Any]:
    return await DashboardService(session).admin()


__all__ = ["billing_router", "dashboard_router", "deployments_router"]
