"""Accounts, routing, contacts, projects and the activity timeline.

DATABASE.md section 3.2. The design decision that matters here is AD-3: customer,
partner, prime contractor and vendor/MSP are one table with a type, plus a
self-referential relationship graph — because the same organisation is often a
customer on one project and a prime on another, and because the SOW's central
question ("through which customer, partner or prime should we approach?") is a
relationship, not a table.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseEntity, SoftDeleteEntity
from app.db.types import GUID, JSONType, StrEnumType, UTCDateTime


class AccountType(StrEnum):
    CUSTOMER = "CUSTOMER"
    PARTNER = "PARTNER"
    PRIME_CONTRACTOR = "PRIME_CONTRACTOR"
    VENDOR_MSP = "VENDOR_MSP"
    PROSPECT = "PROSPECT"


class RelationshipStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DORMANT = "DORMANT"
    TARGET = "TARGET"
    BLOCKED = "BLOCKED"


class RelationType(StrEnum):
    """How one account gives us reach into another."""

    SUBCONTRACTS_THROUGH = "SUBCONTRACTS_THROUGH"
    PRIME_FOR = "PRIME_FOR"
    PARTNER_OF = "PARTNER_OF"
    VENDOR_TO = "VENDOR_TO"


class ProjectStatus(StrEnum):
    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    ON_HOLD = "ON_HOLD"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class ActivityType(StrEnum):
    CALL = "CALL"
    EMAIL = "EMAIL"
    NOTE = "NOTE"
    MEETING = "MEETING"
    TASK = "TASK"
    STATUS_CHANGE = "STATUS_CHANGE"
    SYSTEM = "SYSTEM"


class Account(SoftDeleteEntity):
    """A customer, partner, prime contractor, vendor/MSP or prospect.

    The boolean relationship flags are stored rather than derived: they are
    commercial facts a human confirms, and they are exactly the inputs the
    Addressability engine consumes (SCORING.md section 2).
    """

    __tablename__ = "accounts"

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    legal_name: Mapped[str | None] = mapped_column(String(240), nullable=True)
    account_type: Mapped[AccountType] = mapped_column(
        StrEnumType(AccountType), nullable=False, index=True
    )
    relationship_status: Mapped[RelationshipStatus] = mapped_column(
        StrEnumType(RelationshipStatus),
        nullable=False,
        default=RelationshipStatus.TARGET,
        index=True,
    )

    country: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    industry: Mapped[str | None] = mapped_column(String(120), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # --- Addressability inputs (SCORING.md section 2, factors 1-4) ---------
    is_existing_customer: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)
    is_existing_partner: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_approved_vendor: Mapped[bool] = mapped_column(default=False, nullable=False)
    has_msa: Mapped[bool] = mapped_column(default=False, nullable=False)
    contract_outsourcing_friendly: Mapped[bool] = mapped_column(default=False, nullable=False)

    payment_terms_days: Mapped[int | None] = mapped_column(nullable=True)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    tags: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)

    contacts: Mapped[list[Contact]] = relationship(
        back_populates="account", cascade="all, delete-orphan", lazy="raise", passive_deletes=True
    )
    projects: Mapped[list[Project]] = relationship(
        back_populates="account",
        cascade="all, delete-orphan",
        lazy="raise",
        passive_deletes=True,
        foreign_keys="Project.account_id",
    )

    __table_args__ = (
        Index("ix_accounts_type_status", "account_type", "relationship_status"),
        UniqueConstraint("name", "country", name="account_name_country"),
    )

    @property
    def is_addressable_route(self) -> bool:
        """Whether this account can be sold to directly, without a middleman."""
        return self.is_existing_customer or self.is_approved_vendor or self.has_msa


class AccountRelationship(BaseEntity):
    """The routing graph: how we reach one account through another.

    Answers the SOW question "through which customer, partner or prime contractor
    should we approach the opportunity?". `is_preferred_route` marks the route
    Sales should try first, and feeds Addressability factor 3.
    """

    __tablename__ = "account_relationships"

    from_account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    to_account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation_type: Mapped[RelationType] = mapped_column(StrEnumType(RelationType), nullable=False)
    is_preferred_route: Mapped[bool] = mapped_column(default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    from_account: Mapped[Account] = relationship(foreign_keys=[from_account_id], lazy="raise")
    to_account: Mapped[Account] = relationship(foreign_keys=[to_account_id], lazy="raise")

    __table_args__ = (
        UniqueConstraint(
            "from_account_id", "to_account_id", "relation_type", name="account_relationship_unique"
        ),
        CheckConstraint("from_account_id <> to_account_id", name="no_self_relationship"),
    )


class Contact(BaseEntity):
    __tablename__ = "contacts"

    account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    full_name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    title: Mapped[str | None] = mapped_column(String(140), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(48), nullable=True)
    linkedin_url: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Addressability factor 5: knowing who decides is worth points.
    is_decision_maker: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)
    is_primary: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    account: Mapped[Account] = relationship(back_populates="contacts", lazy="raise")

    __table_args__ = (Index("ix_contacts_account_decision", "account_id", "is_decision_maker"),)


class Technology(BaseEntity):
    """Master list of technology families (SOW section 3)."""

    __tablename__ = "technologies"

    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    aliases: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)


class Project(SoftDeleteEntity):
    __tablename__ = "projects"

    account_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[ProjectStatus] = mapped_column(
        StrEnumType(ProjectStatus), nullable=False, default=ProjectStatus.PLANNED, index=True
    )
    start_date: Mapped[date | None] = mapped_column(nullable=True)
    end_date: Mapped[date | None] = mapped_column(nullable=True, index=True)
    location: Mapped[str | None] = mapped_column(String(160), nullable=True)

    # The route into this specific project, which can differ from the account's
    # general route.
    prime_contractor_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    account: Mapped[Account] = relationship(
        back_populates="projects", foreign_keys=[account_id], lazy="raise"
    )
    prime_contractor: Mapped[Account | None] = relationship(
        foreign_keys=[prime_contractor_id], lazy="raise"
    )
    technologies: Mapped[list[ProjectTechnology]] = relationship(
        back_populates="project", cascade="all, delete-orphan", lazy="raise", passive_deletes=True
    )

    __table_args__ = (Index("ix_projects_account_status", "account_id", "status"),)


class ProjectTechnology(BaseEntity):
    __tablename__ = "project_technologies"

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    technology_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("technologies.id", ondelete="CASCADE"), nullable=False, index=True
    )

    project: Mapped[Project] = relationship(back_populates="technologies", lazy="raise")
    technology: Mapped[Technology] = relationship(lazy="selectin")

    __table_args__ = (
        UniqueConstraint("project_id", "technology_id", name="project_technology_unique"),
    )


class Activity(BaseEntity):
    """The lightweight Contact & Activity Log (SOW section 4 NEW).

    One table with nullable foreign keys to each attachable entity, so any record
    can render the same timeline component. Later phases add their own foreign
    key columns (requirement, opportunity, resource, submission) rather than
    degrading this into an untyped `entity_type`/`entity_id` pair — referential
    integrity is the reason to use a relational database.

    Deliberately not a CRM: a timeline of entries with follow-up dates, nothing more.
    """

    __tablename__ = "activities"

    activity_type: Mapped[ActivityType] = mapped_column(
        StrEnumType(ActivityType), nullable=False, index=True
    )
    subject: Mapped[str] = mapped_column(String(240), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(240), nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    follow_up_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("contacts.id", ondelete="CASCADE"), nullable=True, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )

    __table_args__ = (
        CheckConstraint(
            "account_id IS NOT NULL OR contact_id IS NOT NULL OR project_id IS NOT NULL",
            name="activity_has_a_subject",
        ),
        Index("ix_activities_account_time", "account_id", "occurred_at"),
        Index("ix_activities_followup_open", "follow_up_at", "completed_at"),
    )

    @property
    def is_follow_up_open(self) -> bool:
        return self.follow_up_at is not None and self.completed_at is None

    def attachment(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "contact_id": self.contact_id,
            "project_id": self.project_id,
        }


__all__ = [
    "Account",
    "AccountRelationship",
    "AccountType",
    "Activity",
    "ActivityType",
    "Contact",
    "Project",
    "ProjectStatus",
    "ProjectTechnology",
    "RelationType",
    "RelationshipStatus",
    "Technology",
]
