"""The Glimmora IT Talent Cloud (DATABASE.md section 3.4).

Two things here are not ordinary CRUD:

* **Availability** drives the zero-bench engine in Phase 8, so `available_from`
  and `notice_period_days` are first-class rather than notes.
* **Document expiry** is revenue protection, not administration. In the Gulf an
  expired QID or work permit stops billing on a live deployment, so expiry is a
  tracked date with derived state and scheduled reminders (SOW section 6 NEW).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseEntity, SoftDeleteEntity
from app.db.types import GUID, JSONType, MoneyType, StrEnumType, UTCDateTime


class ResourceType(StrEnum):
    """SOW section 6 — the seven categories the talent cloud must cover."""

    EMPLOYEE = "EMPLOYEE"
    BENCH = "BENCH"
    CONSULTANT = "CONSULTANT"
    FREELANCER = "FREELANCER"
    PARTNER_RESOURCE = "PARTNER_RESOURCE"
    PREVIOUS_CANDIDATE = "PREVIOUS_CANDIDATE"
    PRE_VETTED_CANDIDATE = "PRE_VETTED_CANDIDATE"


class AvailabilityStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    AVAILABLE_SOON = "AVAILABLE_SOON"
    DEPLOYED = "DEPLOYED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class VisaStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    VALID = "VALID"
    IN_PROCESS = "IN_PROCESS"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class AssessmentStatus(StrEnum):
    """V1 stores the result only; the assessment engine is Phase 2."""

    NOT_ASSESSED = "NOT_ASSESSED"
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"


class Proficiency(StrEnum):
    BASIC = "BASIC"
    INTERMEDIATE = "INTERMEDIATE"
    ADVANCED = "ADVANCED"
    EXPERT = "EXPERT"


class DocumentType(StrEnum):
    CV = "CV"
    ID = "ID"
    PASSPORT = "PASSPORT"
    VISA = "VISA"
    WORK_PERMIT = "WORK_PERMIT"
    QID = "QID"
    CONTRACT = "CONTRACT"
    CERTIFICATE = "CERTIFICATE"
    OTHER = "OTHER"


#: Documents whose lapse stops a consultant working — the ones the expiry sweep
#: exists for.
WORK_AUTHORISATION_TYPES = frozenset(
    {DocumentType.VISA, DocumentType.WORK_PERMIT, DocumentType.QID}
)

#: Documents holding personal identifiers, gated by the personal-document
#: permissions in SECURITY.md section 3.
PERSONAL_DOCUMENT_TYPES = frozenset(
    {
        DocumentType.ID,
        DocumentType.PASSPORT,
        DocumentType.VISA,
        DocumentType.WORK_PERMIT,
        DocumentType.QID,
        DocumentType.CONTRACT,
    }
)


class DocumentExpiryState(StrEnum):
    """Derived from `expiry_date`, never stored — a stale state is a lie."""

    NOT_APPLICABLE = "NOT_APPLICABLE"
    VALID = "VALID"
    EXPIRING_SOON = "EXPIRING_SOON"
    EXPIRED = "EXPIRED"


class Resource(SoftDeleteEntity):
    __tablename__ = "resources"

    code: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(48), nullable=True, index=True)

    resource_type: Mapped[ResourceType] = mapped_column(
        StrEnumType(ResourceType), nullable=False, default=ResourceType.CONSULTANT, index=True
    )
    headline: Mapped[str | None] = mapped_column(String(240), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    total_experience_years: Mapped[float | None] = mapped_column(nullable=True)
    relevant_experience_years: Mapped[float | None] = mapped_column(nullable=True)

    current_location_country: Mapped[str | None] = mapped_column(
        String(2), nullable=True, index=True
    )
    current_location_city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    willing_to_relocate: Mapped[bool] = mapped_column(default=False, nullable=False)
    nationality: Mapped[str | None] = mapped_column(String(64), nullable=True)

    visa_status: Mapped[VisaStatus] = mapped_column(
        StrEnumType(VisaStatus), nullable=False, default=VisaStatus.UNKNOWN, index=True
    )
    visa_country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    # --- availability: the zero-bench engine reads these -------------------
    availability_status: Mapped[AvailabilityStatus] = mapped_column(
        StrEnumType(AvailabilityStatus),
        nullable=False,
        default=AvailabilityStatus.NOT_AVAILABLE,
        index=True,
    )
    available_from: Mapped[date | None] = mapped_column(nullable=True, index=True)
    notice_period_days: Mapped[int] = mapped_column(default=0, nullable=False)

    # --- commercial (field-permission gated) --------------------------------
    expected_cost_amount: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    expected_cost_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    expected_cost_unit: Mapped[str | None] = mapped_column(String(16), nullable=True)
    target_billing_amount: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    target_billing_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    target_billing_unit: Mapped[str | None] = mapped_column(String(16), nullable=True)

    assessment_status: Mapped[AssessmentStatus] = mapped_column(
        StrEnumType(AssessmentStatus), nullable=False, default=AssessmentStatus.NOT_ASSESSED
    )
    assessment_score: Mapped[int | None] = mapped_column(nullable=True)
    assessment_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    partner_account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # --- AI provenance (AD-7) ------------------------------------------------
    source: Mapped[str] = mapped_column(String(24), nullable=False, default="MANUAL")
    review_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="ACCEPTED", index=True
    )
    parsed_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    parse_confidence: Mapped[float | None] = mapped_column(nullable=True)
    parse_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    parsed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    skills: Mapped[list[ResourceSkill]] = relationship(
        back_populates="resource", cascade="all, delete-orphan", lazy="raise", passive_deletes=True
    )
    experience: Mapped[list[ResourceExperience]] = relationship(
        back_populates="resource", cascade="all, delete-orphan", lazy="raise", passive_deletes=True
    )
    certifications: Mapped[list[ResourceCertification]] = relationship(
        back_populates="resource", cascade="all, delete-orphan", lazy="raise", passive_deletes=True
    )
    documents: Mapped[list[ResourceDocument]] = relationship(
        back_populates="resource", cascade="all, delete-orphan", lazy="raise", passive_deletes=True
    )

    __table_args__ = (
        Index("ix_resources_availability", "availability_status", "available_from"),
        Index("ix_resources_type_active", "resource_type", "deleted_at"),
    )

    @property
    def is_bench(self) -> bool:
        """Unbilled capacity — the number the zero-bench engine drives to zero."""
        return self.resource_type is ResourceType.BENCH or (
            self.availability_status is AvailabilityStatus.AVAILABLE
            and self.resource_type
            in {ResourceType.EMPLOYEE, ResourceType.CONSULTANT, ResourceType.BENCH}
        )

    @property
    def is_awaiting_review(self) -> bool:
        return self.review_status == "PENDING_REVIEW"


class ResourceSkill(BaseEntity):
    __tablename__ = "resource_skills"

    resource_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False, index=True
    )
    years: Mapped[float | None] = mapped_column(nullable=True)
    proficiency: Mapped[Proficiency] = mapped_column(
        StrEnumType(Proficiency), nullable=False, default=Proficiency.INTERMEDIATE
    )
    #: Recency matters to matching: a skill last used six years ago is weaker.
    last_used_year: Mapped[int | None] = mapped_column(nullable=True)
    is_primary: Mapped[bool] = mapped_column(default=False, nullable=False)

    resource: Mapped[Resource] = relationship(back_populates="skills", lazy="raise")
    skill: Mapped[Any] = relationship("Skill", lazy="selectin")

    __table_args__ = (UniqueConstraint("resource_id", "skill_id", name="resource_skill_unique"),)


class ResourceExperience(BaseEntity):
    __tablename__ = "resource_experience"

    resource_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    project_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role: Mapped[str | None] = mapped_column(String(160), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_date: Mapped[date | None] = mapped_column(nullable=True)
    end_date: Mapped[date | None] = mapped_column(nullable=True)
    is_current: Mapped[bool] = mapped_column(default=False, nullable=False)
    location: Mapped[str | None] = mapped_column(String(160), nullable=True)
    technologies: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)

    resource: Mapped[Resource] = relationship(back_populates="experience", lazy="raise")

    __table_args__ = (Index("ix_resource_experience_order", "resource_id", "start_date"),)


class ResourceCertification(BaseEntity):
    __tablename__ = "resource_certifications"

    resource_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    issuer: Mapped[str | None] = mapped_column(String(160), nullable=True)
    issued_at: Mapped[date | None] = mapped_column(nullable=True)
    expires_at: Mapped[date | None] = mapped_column(nullable=True, index=True)
    credential_id: Mapped[str | None] = mapped_column(String(120), nullable=True)

    resource: Mapped[Resource] = relationship(back_populates="certifications", lazy="raise")


class Document(BaseEntity):
    """File metadata. The bytes live in object storage under an opaque key."""

    __tablename__ = "documents"

    storage_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    storage_backend: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ResourceDocument(BaseEntity):
    """A typed, expiry-tracked document attached to a resource.

    `expiry_date` is what makes this table matter: an expired work permit stops
    billing on a live deployment, so the state is derived on every read and the
    beat schedule warns before it lapses.
    """

    __tablename__ = "resource_documents"

    resource_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doc_type: Mapped[DocumentType] = mapped_column(
        StrEnumType(DocumentType), nullable=False, index=True
    )
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    issue_date: Mapped[date | None] = mapped_column(nullable=True)
    expiry_date: Mapped[date | None] = mapped_column(nullable=True, index=True)
    issuing_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    reference_number: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    resource: Mapped[Resource] = relationship(back_populates="documents", lazy="raise")
    document: Mapped[Document] = relationship(lazy="selectin")

    __table_args__ = (
        Index("ix_resource_documents_expiry", "expiry_date", "doc_type"),
        Index("ix_resource_documents_type", "resource_id", "doc_type"),
    )

    @property
    def is_work_authorisation(self) -> bool:
        return self.doc_type in WORK_AUTHORISATION_TYPES

    @property
    def is_personal(self) -> bool:
        return self.doc_type in PERSONAL_DOCUMENT_TYPES


__all__ = [
    "PERSONAL_DOCUMENT_TYPES",
    "WORK_AUTHORISATION_TYPES",
    "AssessmentStatus",
    "AvailabilityStatus",
    "Document",
    "DocumentExpiryState",
    "DocumentType",
    "Proficiency",
    "Resource",
    "ResourceCertification",
    "ResourceDocument",
    "ResourceExperience",
    "ResourceSkill",
    "ResourceType",
    "VisaStatus",
]
