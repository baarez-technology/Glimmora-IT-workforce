"""Resource, document and CV-parsing schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.talent import (
    AssessmentStatus,
    AvailabilityStatus,
    DocumentExpiryState,
    DocumentType,
    Proficiency,
    ResourceType,
    VisaStatus,
)

# ------------------------------------------------------------------- skills


class ResourceSkillInput(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    years: float | None = Field(default=None, ge=0, le=50)
    proficiency: Proficiency = Proficiency.INTERMEDIATE
    last_used_year: int | None = Field(default=None, ge=1980, le=2100)
    is_primary: bool = False

    @field_validator("name")
    @classmethod
    def _trim(cls, value: str) -> str:
        return value.strip()


class ResourceSkillResponse(BaseModel):
    id: uuid.UUID
    skill_id: uuid.UUID
    name: str
    category: str | None = None
    years: float | None
    proficiency: Proficiency
    last_used_year: int | None
    is_primary: bool


# --------------------------------------------------------------- experience


class ResourceExperienceInput(BaseModel):
    company: str | None = Field(default=None, max_length=200)
    project_name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=160)
    description: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    is_current: bool = False
    location: str | None = Field(default=None, max_length=160)
    technologies: list[str] | None = None


class ResourceExperienceResponse(ResourceExperienceInput):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID


class ResourceCertificationInput(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    issuer: str | None = Field(default=None, max_length=160)
    issued_at: date | None = None
    expires_at: date | None = None
    credential_id: str | None = Field(default=None, max_length=120)


class ResourceCertificationResponse(ResourceCertificationInput):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    expiry_state: DocumentExpiryState = DocumentExpiryState.NOT_APPLICABLE


# ---------------------------------------------------------------- documents


class DocumentExpiryInfo(BaseModel):
    state: DocumentExpiryState
    expiry_date: date | None
    days_remaining: int | None
    is_expired: bool
    label: str


class ResourceDocumentCreate(BaseModel):
    doc_type: DocumentType
    title: str | None = Field(default=None, max_length=200)
    issue_date: date | None = None
    expiry_date: date | None = None
    issuing_country: str | None = Field(default=None, min_length=2, max_length=2)
    reference_number: str | None = Field(default=None, max_length=120)
    notes: str | None = None

    @field_validator("issuing_country")
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class ResourceDocumentUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    issue_date: date | None = None
    expiry_date: date | None = None
    issuing_country: str | None = Field(default=None, min_length=2, max_length=2)
    reference_number: str | None = Field(default=None, max_length=120)
    notes: str | None = None


class ResourceDocumentResponse(BaseModel):
    id: uuid.UUID
    resource_id: uuid.UUID
    resource_name: str | None = None
    document_id: uuid.UUID
    doc_type: DocumentType
    doc_type_label: str
    title: str | None
    original_filename: str
    content_type: str
    size_bytes: int
    issue_date: date | None
    #: Present only for callers holding `document.personal:view`.
    reference_number: str | None = None
    issuing_country: str | None
    notes: str | None
    expiry: DocumentExpiryInfo
    is_work_authorisation: bool
    can_download: bool = False
    created_at: datetime


# ---------------------------------------------------------------- resources


class ResourceBase(BaseModel):
    full_name: str = Field(min_length=2, max_length=160)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=48)
    resource_type: ResourceType = ResourceType.CONSULTANT
    headline: str | None = Field(default=None, max_length=240)
    summary: str | None = None

    total_experience_years: float | None = Field(default=None, ge=0, le=60)
    relevant_experience_years: float | None = Field(default=None, ge=0, le=60)

    current_location_country: str | None = Field(default=None, min_length=2, max_length=2)
    current_location_city: str | None = Field(default=None, max_length=120)
    willing_to_relocate: bool = False
    nationality: str | None = Field(default=None, max_length=64)

    visa_status: VisaStatus = VisaStatus.UNKNOWN
    visa_country: str | None = Field(default=None, min_length=2, max_length=2)

    availability_status: AvailabilityStatus = AvailabilityStatus.NOT_AVAILABLE
    available_from: date | None = None
    notice_period_days: int = Field(default=0, ge=0, le=365)

    expected_cost_amount: Decimal | None = Field(default=None, ge=0)
    expected_cost_currency: str | None = Field(default=None, min_length=3, max_length=3)
    expected_cost_unit: str | None = Field(default=None, max_length=16)
    target_billing_amount: Decimal | None = Field(default=None, ge=0)
    target_billing_currency: str | None = Field(default=None, min_length=3, max_length=3)
    target_billing_unit: str | None = Field(default=None, max_length=16)

    assessment_status: AssessmentStatus = AssessmentStatus.NOT_ASSESSED
    assessment_score: int | None = Field(default=None, ge=0, le=100)
    assessment_notes: str | None = None

    partner_account_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    notes: str | None = None

    @field_validator(
        "current_location_country",
        "visa_country",
        "expected_cost_currency",
        "target_billing_currency",
    )
    @classmethod
    def _upper(cls, value: str | None) -> str | None:
        return value.upper() if value else value


class ResourceCreate(ResourceBase):
    skills: list[ResourceSkillInput] = Field(default_factory=list)
    experience: list[ResourceExperienceInput] = Field(default_factory=list)
    certifications: list[ResourceCertificationInput] = Field(default_factory=list)


class ResourceUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=160)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=48)
    resource_type: ResourceType | None = None
    headline: str | None = Field(default=None, max_length=240)
    summary: str | None = None
    total_experience_years: float | None = Field(default=None, ge=0, le=60)
    relevant_experience_years: float | None = Field(default=None, ge=0, le=60)
    current_location_country: str | None = Field(default=None, min_length=2, max_length=2)
    current_location_city: str | None = Field(default=None, max_length=120)
    willing_to_relocate: bool | None = None
    nationality: str | None = Field(default=None, max_length=64)
    visa_status: VisaStatus | None = None
    visa_country: str | None = Field(default=None, min_length=2, max_length=2)
    availability_status: AvailabilityStatus | None = None
    available_from: date | None = None
    notice_period_days: int | None = Field(default=None, ge=0, le=365)
    expected_cost_amount: Decimal | None = Field(default=None, ge=0)
    expected_cost_currency: str | None = Field(default=None, min_length=3, max_length=3)
    expected_cost_unit: str | None = Field(default=None, max_length=16)
    target_billing_amount: Decimal | None = Field(default=None, ge=0)
    target_billing_currency: str | None = Field(default=None, min_length=3, max_length=3)
    target_billing_unit: str | None = Field(default=None, max_length=16)
    assessment_status: AssessmentStatus | None = None
    assessment_score: int | None = Field(default=None, ge=0, le=100)
    assessment_notes: str | None = None
    partner_account_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    notes: str | None = None
    skills: list[ResourceSkillInput] | None = None
    experience: list[ResourceExperienceInput] | None = None
    certifications: list[ResourceCertificationInput] | None = None


class ResourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str | None
    full_name: str
    email: str | None
    phone: str | None
    resource_type: ResourceType
    headline: str | None
    summary: str | None

    total_experience_years: float | None
    relevant_experience_years: float | None

    current_location_country: str | None
    current_location_city: str | None
    willing_to_relocate: bool
    nationality: str | None

    visa_status: VisaStatus
    visa_country: str | None

    availability_status: AvailabilityStatus
    available_from: date | None
    notice_period_days: int
    ready_from: date | None = None

    # Stripped for callers without the matching field permission.
    expected_cost_amount: Decimal | None = None
    expected_cost_currency: str | None = None
    expected_cost_unit: str | None = None
    target_billing_amount: Decimal | None = None
    target_billing_currency: str | None = None
    target_billing_unit: str | None = None

    assessment_status: AssessmentStatus
    assessment_score: int | None
    assessment_notes: str | None

    partner_account_id: uuid.UUID | None
    partner_account_name: str | None = None
    owner_id: uuid.UUID | None
    owner_name: str | None = None
    notes: str | None

    review_status: str
    source: str
    parse_confidence: float | None
    parse_model: str | None
    parsed_at: datetime | None
    needs_review: bool = False

    is_bench: bool = False
    #: The worst state across this resource's work-authorisation documents.
    work_authorisation: DocumentExpiryInfo | None = None
    blocks_deployment: bool = False

    skills: list[ResourceSkillResponse] = Field(default_factory=list)
    experience: list[ResourceExperienceResponse] = Field(default_factory=list)
    certifications: list[ResourceCertificationResponse] = Field(default_factory=list)
    document_count: int = 0

    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------- CV parsing


class DuplicateMatch(BaseModel):
    """A candidate already on file who looks like the same person."""

    resource_id: uuid.UUID
    full_name: str
    email: str | None
    reason: str
    confidence: float


class ParsedCVField(BaseModel):
    field: str
    label: str
    value: object = None
    confidence: float
    level: str
    requires_confirmation: bool
    evidence: str | None = None


class CVParseResponse(BaseModel):
    resource_id: uuid.UUID
    source_text: str
    provider: str
    model_id: str
    used_fallback: bool
    overall_confidence: float
    fields: list[ParsedCVField]
    warnings: list[str] = Field(default_factory=list)
    confirmation_required: list[str] = Field(default_factory=list)
    duplicates: list[DuplicateMatch] = Field(default_factory=list)


class AcceptCVRequest(BaseModel):
    updates: ResourceUpdate = Field(default_factory=ResourceUpdate)
    skills: list[ResourceSkillInput] | None = None
    confirmed_fields: list[str] = Field(default_factory=list)


class ExpiringDocumentsSummary(BaseModel):
    expired: list[ResourceDocumentResponse]
    expiring_soon: list[ResourceDocumentResponse]
    counts: dict[str, int]


__all__ = [
    "AcceptCVRequest",
    "CVParseResponse",
    "DocumentExpiryInfo",
    "DuplicateMatch",
    "ExpiringDocumentsSummary",
    "ParsedCVField",
    "ResourceBase",
    "ResourceCertificationInput",
    "ResourceCertificationResponse",
    "ResourceCreate",
    "ResourceDocumentCreate",
    "ResourceDocumentResponse",
    "ResourceDocumentUpdate",
    "ResourceExperienceInput",
    "ResourceExperienceResponse",
    "ResourceResponse",
    "ResourceSkillInput",
    "ResourceSkillResponse",
    "ResourceUpdate",
]
