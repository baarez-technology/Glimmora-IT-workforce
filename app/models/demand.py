"""Requirements: the demand side of the platform (DATABASE.md section 3.3)."""

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


class PrioritySource(StrEnum):
    """SOW section 2 — where the requirement came from, in pursuit priority order."""

    P1_EXISTING_CUSTOMER = "P1_EXISTING_CUSTOMER"
    P2_PARTNER_PRIME = "P2_PARTNER_PRIME"
    P3_PROJECT = "P3_PROJECT"
    P4_ENTERPRISE_GOV = "P4_ENTERPRISE_GOV"
    P5_VENDOR_MSP_VMS = "P5_VENDOR_MSP_VMS"
    P6_EXTERNAL_APPROVED = "P6_EXTERNAL_APPROVED"


class RequirementSource(StrEnum):
    """How the requirement entered the platform (SOW section 5)."""

    MANUAL = "MANUAL"
    JD_PASTE = "JD_PASTE"
    DOCUMENT_UPLOAD = "DOCUMENT_UPLOAD"
    EMAIL = "EMAIL"
    EXCEL_IMPORT = "EXCEL_IMPORT"
    API = "API"


class ContractType(StrEnum):
    CONTRACT = "CONTRACT"
    CONTRACT_TO_HIRE = "CONTRACT_TO_HIRE"
    PERMANENT = "PERMANENT"
    OUTSOURCED_SERVICE = "OUTSOURCED_SERVICE"


class WorkMode(StrEnum):
    ONSITE = "ONSITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"


class RateUnit(StrEnum):
    HOURLY = "HOURLY"
    DAILY = "DAILY"
    MONTHLY = "MONTHLY"
    ANNUAL = "ANNUAL"


class RequirementStatus(StrEnum):
    NEW = "NEW"
    PARSED = "PARSED"
    UNDER_REVIEW = "UNDER_REVIEW"
    QUALIFIED = "QUALIFIED"
    ON_HOLD = "ON_HOLD"
    CLOSED_WON = "CLOSED_WON"
    CLOSED_LOST = "CLOSED_LOST"
    EXPIRED = "EXPIRED"


#: Statuses a requirement can no longer be pursued from.
TERMINAL_STATUSES = frozenset(
    {RequirementStatus.CLOSED_WON, RequirementStatus.CLOSED_LOST, RequirementStatus.EXPIRED}
)


class ReviewStatus(StrEnum):
    """AD-7: AI output is not business data until a human accepts it."""

    PENDING_REVIEW = "PENDING_REVIEW"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class SkillImportance(StrEnum):
    MANDATORY = "MANDATORY"
    PREFERRED = "PREFERRED"
    NICE_TO_HAVE = "NICE_TO_HAVE"


class DeadlineState(StrEnum):
    """Derived from `response_deadline_at`, never stored (ASSUMPTIONS.md A12)."""

    NONE = "NONE"
    SAFE = "SAFE"
    DUE_SOON = "DUE_SOON"
    URGENT = "URGENT"
    EXPIRED = "EXPIRED"


class Requirement(SoftDeleteEntity):
    __tablename__ = "requirements"

    # --- what the client wants ------------------------------------------
    title: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    role: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    description_raw: Mapped[str | None] = mapped_column(Text, nullable=True)

    location: Mapped[str | None] = mapped_column(String(160), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    work_mode: Mapped[WorkMode | None] = mapped_column(StrEnumType(WorkMode), nullable=True)
    contract_type: Mapped[ContractType | None] = mapped_column(
        StrEnumType(ContractType), nullable=True, index=True
    )

    experience_min_years: Mapped[int | None] = mapped_column(nullable=True)
    experience_max_years: Mapped[int | None] = mapped_column(nullable=True)
    duration_months: Mapped[int | None] = mapped_column(nullable=True)
    positions: Mapped[int] = mapped_column(default=1, nullable=False)
    start_by_date: Mapped[date | None] = mapped_column(nullable=True)
    availability_requirement: Mapped[str | None] = mapped_column(String(160), nullable=True)

    # --- commercial ------------------------------------------------------
    rate_min: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    rate_max: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    rate_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    rate_unit: Mapped[RateUnit | None] = mapped_column(StrEnumType(RateUnit), nullable=True)

    # --- who and how we reach them ---------------------------------------
    #: The account that told us about the requirement.
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: Where the consultant actually works, when different from the account.
    end_customer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: The partner or prime we would submit through.
    route_account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # --- provenance and workflow -----------------------------------------
    priority_source: Mapped[PrioritySource] = mapped_column(
        StrEnumType(PrioritySource),
        nullable=False,
        default=PrioritySource.P6_EXTERNAL_APPROVED,
        index=True,
    )
    source: Mapped[RequirementSource] = mapped_column(
        StrEnumType(RequirementSource), nullable=False, default=RequirementSource.MANUAL
    )
    source_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_reference: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)

    status: Mapped[RequirementStatus] = mapped_column(
        StrEnumType(RequirementStatus), nullable=False, default=RequirementStatus.NEW, index=True
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: SOW section 5 NEW. VMS windows are commonly 24-48 hours; missing it loses
    #: the seat regardless of how good the candidate is.
    response_deadline_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, index=True
    )

    # --- AI provenance (AD-7) --------------------------------------------
    review_status: Mapped[ReviewStatus] = mapped_column(
        StrEnumType(ReviewStatus), nullable=False, default=ReviewStatus.ACCEPTED, index=True
    )
    #: Per-field {value, confidence, evidence_span} from the parser.
    parsed_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    parse_confidence: Mapped[float | None] = mapped_column(nullable=True)
    parse_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    parsed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    skills: Mapped[list[RequirementSkill]] = relationship(
        back_populates="requirement",
        cascade="all, delete-orphan",
        lazy="raise",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_requirements_status_active", "status", "is_active"),
        Index("ix_requirements_owner_status", "owner_id", "status"),
        Index("ix_requirements_deadline_open", "response_deadline_at", "is_active"),
    )

    @property
    def is_open(self) -> bool:
        return self.is_active and self.status not in TERMINAL_STATUSES

    @property
    def is_awaiting_review(self) -> bool:
        return self.review_status is ReviewStatus.PENDING_REVIEW


class RequirementSkill(BaseEntity):
    __tablename__ = "requirement_skills"

    requirement_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("skills.id", ondelete="CASCADE"), nullable=False, index=True
    )
    importance: Mapped[SkillImportance] = mapped_column(
        StrEnumType(SkillImportance), nullable=False, default=SkillImportance.MANDATORY, index=True
    )
    min_years: Mapped[int | None] = mapped_column(nullable=True)

    requirement: Mapped[Requirement] = relationship(back_populates="skills", lazy="raise")
    skill: Mapped[Any] = relationship("Skill", lazy="selectin")

    __table_args__ = (
        UniqueConstraint("requirement_id", "skill_id", name="requirement_skill_unique"),
    )


class RequirementStatusHistory(BaseEntity):
    """Append-only stage history. Status changes are events, not silent edits."""

    __tablename__ = "requirement_status_history"

    requirement_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[RequirementStatus | None] = mapped_column(
        StrEnumType(RequirementStatus), nullable=True
    )
    to_status: Mapped[RequirementStatus] = mapped_column(
        StrEnumType(RequirementStatus), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (Index("ix_requirement_history_time", "requirement_id", "created_at"),)


__all__ = [
    "TERMINAL_STATUSES",
    "ContractType",
    "DeadlineState",
    "PrioritySource",
    "RateUnit",
    "Requirement",
    "RequirementSkill",
    "RequirementSource",
    "RequirementStatus",
    "RequirementStatusHistory",
    "ReviewStatus",
    "SkillImportance",
    "WorkMode",
]
