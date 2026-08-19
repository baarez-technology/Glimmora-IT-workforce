"""Requirement, parsing and review schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.demand import (
    ContractType,
    DeadlineState,
    PrioritySource,
    RateUnit,
    RequirementSource,
    RequirementStatus,
    ReviewStatus,
    SkillImportance,
    WorkMode,
)

# ------------------------------------------------------------------- skills


class RequirementSkillInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    importance: SkillImportance = SkillImportance.MANDATORY
    min_years: int | None = Field(default=None, ge=0, le=45)

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str) -> str:
        return value.strip()


class RequirementSkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    skill_id: uuid.UUID
    name: str
    category: str | None = None
    importance: SkillImportance
    min_years: int | None


# -------------------------------------------------------------- requirements


class RequirementBase(BaseModel):
    title: str = Field(min_length=3, max_length=240)
    role: str | None = Field(default=None, max_length=160)
    description_raw: str | None = None

    location: str | None = Field(default=None, max_length=160)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    work_mode: WorkMode | None = None
    contract_type: ContractType | None = None

    experience_min_years: int | None = Field(default=None, ge=0, le=45)
    experience_max_years: int | None = Field(default=None, ge=0, le=45)
    duration_months: int | None = Field(default=None, ge=1, le=120)
    positions: int = Field(default=1, ge=1, le=50)
    start_by_date: date | None = None
    availability_requirement: str | None = Field(default=None, max_length=160)

    rate_min: Decimal | None = Field(default=None, ge=0)
    rate_max: Decimal | None = Field(default=None, ge=0)
    rate_currency: str | None = Field(default=None, min_length=3, max_length=3)
    rate_unit: RateUnit | None = None

    account_id: uuid.UUID | None = None
    end_customer_id: uuid.UUID | None = None
    route_account_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None

    priority_source: PrioritySource = PrioritySource.P6_EXTERNAL_APPROVED
    source_detail: str | None = Field(default=None, max_length=255)
    external_reference: str | None = Field(default=None, max_length=120)
    response_deadline_at: datetime | None = None
    owner_id: uuid.UUID | None = None
    notes: str | None = None

    @field_validator("country", "rate_currency")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value

    @model_validator(mode="after")
    def _ranges_are_ordered(self) -> RequirementBase:
        if (
            self.experience_min_years is not None
            and self.experience_max_years is not None
            and self.experience_max_years < self.experience_min_years
        ):
            raise ValueError("experience_max_years must not be below experience_min_years")
        if (
            self.rate_min is not None
            and self.rate_max is not None
            and self.rate_max < self.rate_min
        ):
            raise ValueError("rate_max must not be below rate_min")
        if (self.rate_min is not None or self.rate_max is not None) and not self.rate_unit:
            raise ValueError("rate_unit is required when a rate is given")
        return self


class RequirementCreate(RequirementBase):
    source: RequirementSource = RequirementSource.MANUAL
    skills: list[RequirementSkillInput] = Field(default_factory=list)


class RequirementUpdate(BaseModel):
    """Every field optional; only what is sent is changed."""

    title: str | None = Field(default=None, min_length=3, max_length=240)
    role: str | None = Field(default=None, max_length=160)
    description_raw: str | None = None
    location: str | None = Field(default=None, max_length=160)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    work_mode: WorkMode | None = None
    contract_type: ContractType | None = None
    experience_min_years: int | None = Field(default=None, ge=0, le=45)
    experience_max_years: int | None = Field(default=None, ge=0, le=45)
    duration_months: int | None = Field(default=None, ge=1, le=120)
    positions: int | None = Field(default=None, ge=1, le=50)
    start_by_date: date | None = None
    availability_requirement: str | None = Field(default=None, max_length=160)
    rate_min: Decimal | None = Field(default=None, ge=0)
    rate_max: Decimal | None = Field(default=None, ge=0)
    rate_currency: str | None = Field(default=None, min_length=3, max_length=3)
    rate_unit: RateUnit | None = None
    account_id: uuid.UUID | None = None
    end_customer_id: uuid.UUID | None = None
    route_account_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    priority_source: PrioritySource | None = None
    source_detail: str | None = Field(default=None, max_length=255)
    external_reference: str | None = Field(default=None, max_length=120)
    response_deadline_at: datetime | None = None
    owner_id: uuid.UUID | None = None
    is_active: bool | None = None
    notes: str | None = None
    skills: list[RequirementSkillInput] | None = None

    @field_validator("country", "rate_currency")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class RequirementStatusChange(BaseModel):
    status: RequirementStatus
    reason: str | None = Field(default=None, max_length=255)


class DeadlineInfo(BaseModel):
    state: DeadlineState
    deadline: datetime | None
    hours_remaining: float | None
    is_overdue: bool
    label: str


class RequirementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    role: str | None
    description_raw: str | None

    location: str | None
    country: str | None
    work_mode: WorkMode | None
    contract_type: ContractType | None

    experience_min_years: int | None
    experience_max_years: int | None
    duration_months: int | None
    positions: int
    start_by_date: date | None
    availability_requirement: str | None

    rate_min: Decimal | None
    rate_max: Decimal | None
    rate_currency: str | None
    rate_unit: RateUnit | None

    account_id: uuid.UUID | None
    account_name: str | None = None
    end_customer_id: uuid.UUID | None
    end_customer_name: str | None = None
    route_account_id: uuid.UUID | None
    route_account_name: str | None = None
    project_id: uuid.UUID | None
    project_name: str | None = None

    priority_source: PrioritySource
    source: RequirementSource
    source_detail: str | None
    external_reference: str | None
    status: RequirementStatus
    is_active: bool
    owner_id: uuid.UUID | None
    owner_name: str | None = None
    notes: str | None

    response_deadline_at: datetime | None
    deadline: DeadlineInfo | None = None

    review_status: ReviewStatus
    parse_confidence: float | None
    parse_model: str | None
    parsed_at: datetime | None
    needs_review: bool = False

    skills: list[RequirementSkillResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class RequirementStatusHistoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_status: RequirementStatus | None
    to_status: RequirementStatus
    user_id: uuid.UUID | None
    user_name: str | None = None
    reason: str | None
    created_at: datetime


# ------------------------------------------------------------------ parsing


class ParseTextRequest(BaseModel):
    text: str = Field(min_length=20, max_length=200_000)
    source: RequirementSource = RequirementSource.JD_PASTE
    source_detail: str | None = Field(default=None, max_length=255)
    priority_source: PrioritySource | None = None
    account_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None


class ParsedFieldResponse(BaseModel):
    """One extracted field, with what a reviewer needs to verify it."""

    field: str
    label: str
    value: object = None
    confidence: float
    #: HIGH pre-fills and can be accepted at a glance; MEDIUM must be confirmed;
    #: LOW is left for the reviewer to fill in.
    level: str
    requires_confirmation: bool
    evidence: str | None = None
    evidence_start: int | None = None
    evidence_end: int | None = None


class ParseResultResponse(BaseModel):
    requirement_id: uuid.UUID
    source_text: str
    provider: str
    model_id: str
    used_fallback: bool
    overall_confidence: float
    fields: list[ParsedFieldResponse]
    unresolved_skills: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    #: Fields the reviewer must confirm before the requirement can be accepted.
    confirmation_required: list[str] = Field(default_factory=list)


class AcceptParseRequest(BaseModel):
    """The reviewed values. Anything omitted keeps the parsed value."""

    updates: RequirementUpdate = Field(default_factory=RequirementUpdate)
    skills: list[RequirementSkillInput] | None = None
    confirmed_fields: list[str] = Field(default_factory=list)


class RequirementDeadlineBoard(BaseModel):
    urgent: list[RequirementResponse]
    due_soon: list[RequirementResponse]
    safe: list[RequirementResponse]
    expired: list[RequirementResponse]
    counts: dict[str, int]


__all__ = [
    "AcceptParseRequest",
    "DeadlineInfo",
    "ParseResultResponse",
    "ParseTextRequest",
    "ParsedFieldResponse",
    "RequirementCreate",
    "RequirementDeadlineBoard",
    "RequirementResponse",
    "RequirementSkillInput",
    "RequirementSkillResponse",
    "RequirementStatusChange",
    "RequirementStatusHistoryResponse",
    "RequirementUpdate",
]
