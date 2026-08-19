"""Account, routing, contact, project and activity schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.accounts import (
    AccountType,
    ActivityType,
    ProjectStatus,
    RelationshipStatus,
    RelationType,
)

# ------------------------------------------------------------------ accounts


class AccountBase(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    account_type: AccountType
    legal_name: str | None = Field(default=None, max_length=240)
    relationship_status: RelationshipStatus = RelationshipStatus.TARGET
    country: str | None = Field(default=None, min_length=2, max_length=2)
    city: str | None = Field(default=None, max_length=120)
    industry: str | None = Field(default=None, max_length=120)
    website: str | None = Field(default=None, max_length=255)

    is_existing_customer: bool = False
    is_existing_partner: bool = False
    is_approved_vendor: bool = False
    has_msa: bool = False
    contract_outsourcing_friendly: bool = False

    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    owner_id: uuid.UUID | None = None
    notes: str | None = None
    tags: list[str] | None = None

    @field_validator("name", "legal_name")
    @classmethod
    def _trim(cls, value: str | None) -> str | None:
        return value.strip() if value else value

    @field_validator("country")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class AccountCreate(AccountBase):
    pass


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    account_type: AccountType | None = None
    legal_name: str | None = Field(default=None, max_length=240)
    relationship_status: RelationshipStatus | None = None
    country: str | None = Field(default=None, min_length=2, max_length=2)
    city: str | None = Field(default=None, max_length=120)
    industry: str | None = Field(default=None, max_length=120)
    website: str | None = Field(default=None, max_length=255)
    is_existing_customer: bool | None = None
    is_existing_partner: bool | None = None
    is_approved_vendor: bool | None = None
    has_msa: bool | None = None
    contract_outsourcing_friendly: bool | None = None
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    owner_id: uuid.UUID | None = None
    notes: str | None = None
    tags: list[str] | None = None


class AddressabilitySignals(BaseModel):
    """A preview of the Addressability inputs this account currently satisfies.

    Not the score — Phase 9 computes that. This tells a user *now* which facts are
    still missing, so the record can be completed before scoring depends on it
    (SCORING.md section 1: missing data is penalised transparently, never silently).
    """

    contract_outsourcing_friendly: bool
    existing_customer: bool
    partner_or_prime_route: bool
    approved_vendor: bool
    decision_maker_known: bool

    signals_met: int
    signals_total: int
    missing: list[str]


class AccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    legal_name: str | None
    account_type: AccountType
    relationship_status: RelationshipStatus
    country: str | None
    city: str | None
    industry: str | None
    website: str | None

    is_existing_customer: bool
    is_existing_partner: bool
    is_approved_vendor: bool
    has_msa: bool
    contract_outsourcing_friendly: bool

    payment_terms_days: int | None
    owner_id: uuid.UUID | None
    owner_name: str | None = None
    notes: str | None
    tags: list[str] | None

    contact_count: int = 0
    project_count: int = 0
    decision_maker_count: int = 0
    route_count: int = 0
    addressability: AddressabilitySignals | None = None

    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------- routing


class AccountRouteCreate(BaseModel):
    to_account_id: uuid.UUID
    relation_type: RelationType
    is_preferred_route: bool = False
    notes: str | None = None


class AccountRouteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_account_id: uuid.UUID
    to_account_id: uuid.UUID
    to_account_name: str | None = None
    to_account_type: AccountType | None = None
    relation_type: RelationType
    is_preferred_route: bool
    notes: str | None
    created_at: datetime


# ------------------------------------------------------------------ contacts


class ContactCreate(BaseModel):
    account_id: uuid.UUID
    full_name: str = Field(min_length=2, max_length=160)
    title: str | None = Field(default=None, max_length=140)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=48)
    linkedin_url: str | None = Field(default=None, max_length=255)
    is_decision_maker: bool = False
    is_primary: bool = False
    notes: str | None = None


class ContactUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    title: str | None = Field(default=None, max_length=140)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=48)
    linkedin_url: str | None = Field(default=None, max_length=255)
    is_decision_maker: bool | None = None
    is_primary: bool | None = None
    is_active: bool | None = None
    notes: str | None = None


class ContactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    account_name: str | None = None
    full_name: str
    title: str | None
    email: str | None
    phone: str | None
    linkedin_url: str | None
    is_decision_maker: bool
    is_primary: bool
    is_active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------ projects


class ProjectCreate(BaseModel):
    account_id: uuid.UUID
    name: str = Field(min_length=2, max_length=200)
    code: str | None = Field(default=None, max_length=48)
    description: str | None = None
    status: ProjectStatus = ProjectStatus.PLANNED
    start_date: date | None = None
    end_date: date | None = None
    location: str | None = Field(default=None, max_length=160)
    prime_contractor_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    technology_ids: list[uuid.UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def _dates_are_ordered(self) -> ProjectCreate:
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        return self


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    code: str | None = Field(default=None, max_length=48)
    description: str | None = None
    status: ProjectStatus | None = None
    start_date: date | None = None
    end_date: date | None = None
    location: str | None = Field(default=None, max_length=160)
    prime_contractor_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    technology_ids: list[uuid.UUID] | None = None


class TechnologyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    category: str | None


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    account_id: uuid.UUID
    account_name: str | None = None
    name: str
    code: str | None
    description: str | None
    status: ProjectStatus
    start_date: date | None
    end_date: date | None
    location: str | None
    prime_contractor_id: uuid.UUID | None
    prime_contractor_name: str | None = None
    owner_id: uuid.UUID | None
    technologies: list[TechnologyResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------- activities


class ActivityCreate(BaseModel):
    activity_type: ActivityType
    subject: str = Field(min_length=2, max_length=240)
    body: str | None = None
    outcome: str | None = Field(default=None, max_length=240)
    occurred_at: datetime | None = None
    follow_up_at: datetime | None = None

    account_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _must_attach_to_something(self) -> ActivityCreate:
        if not (self.account_id or self.contact_id or self.project_id):
            raise ValueError("An activity must be attached to an account, contact or project")
        return self


class ActivityUpdate(BaseModel):
    subject: str | None = Field(default=None, min_length=2, max_length=240)
    body: str | None = None
    outcome: str | None = Field(default=None, max_length=240)
    occurred_at: datetime | None = None
    follow_up_at: datetime | None = None
    completed_at: datetime | None = None


class ActivityResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    activity_type: ActivityType
    subject: str
    body: str | None
    outcome: str | None
    occurred_at: datetime
    follow_up_at: datetime | None
    completed_at: datetime | None
    is_follow_up_open: bool = False
    is_follow_up_overdue: bool = False

    user_id: uuid.UUID | None
    user_name: str | None = None
    account_id: uuid.UUID | None
    account_name: str | None = None
    contact_id: uuid.UUID | None
    contact_name: str | None = None
    project_id: uuid.UUID | None
    project_name: str | None = None

    created_at: datetime


__all__ = [
    "AccountCreate",
    "AccountResponse",
    "AccountRouteCreate",
    "AccountRouteResponse",
    "AccountUpdate",
    "ActivityCreate",
    "ActivityResponse",
    "ActivityUpdate",
    "AddressabilitySignals",
    "ContactCreate",
    "ContactResponse",
    "ContactUpdate",
    "ProjectCreate",
    "ProjectResponse",
    "ProjectUpdate",
    "TechnologyResponse",
]
