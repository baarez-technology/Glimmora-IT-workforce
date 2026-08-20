"""Deployments and billing — where the engine's value becomes measurable.

Two decisions shape this module:

* **A deployment carries its own rate snapshot**, not a reference to the
  requirement's or the consultant's current rate. Rates get renegotiated; a
  billing record for March must stay correct after an April rate change.
* **Projected and confirmed revenue are different things** (ASSUMPTIONS.md A15).
  A projection presented as billed revenue would make the platform's single
  headline metric a lie, so the two never merge into one number.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseEntity
from app.db.types import GUID, MoneyType, StrEnumType


class DeploymentStatus(StrEnum):
    PENDING_START = "PENDING_START"
    ACTIVE = "ACTIVE"
    ON_HOLD = "ON_HOLD"
    ENDED = "ENDED"


class BillingStatus(StrEnum):
    #: Generated from the deployment's dates and rates. Never counted as revenue.
    PROJECTED = "PROJECTED"
    #: A human has checked it against reality.
    CONFIRMED = "CONFIRMED"
    INVOICED = "INVOICED"
    CANCELLED = "CANCELLED"


#: Statuses that count towards billed revenue. PROJECTED is deliberately absent.
REALISED_BILLING_STATUSES: frozenset[BillingStatus] = frozenset(
    {BillingStatus.CONFIRMED, BillingStatus.INVOICED}
)


class Deployment(BaseEntity):
    """One consultant, on one engagement, at one set of rates."""

    __tablename__ = "deployments"

    resource_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True, index=True
    )
    end_customer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    requirement_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True, index=True
    )
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("submissions.id", ondelete="SET NULL"), nullable=True, index=True
    )

    role_title: Mapped[str] = mapped_column(String(160), nullable=False)
    location: Mapped[str | None] = mapped_column(String(160), nullable=True)

    start_date: Mapped[date] = mapped_column(nullable=False, index=True)
    #: Planned end. `actual_end_date` records what really happened, so an early
    #: exit is visible rather than silently rewriting the plan.
    end_date: Mapped[date | None] = mapped_column(nullable=True, index=True)
    actual_end_date: Mapped[date | None] = mapped_column(nullable=True)

    status: Mapped[DeploymentStatus] = mapped_column(
        StrEnumType(DeploymentStatus),
        nullable=False,
        default=DeploymentStatus.PENDING_START,
        index=True,
    )

    # --- the rate snapshot ---------------------------------------------
    bill_rate: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    bill_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="QAR")
    bill_unit: Mapped[str] = mapped_column(String(16), nullable=False, default="MONTHLY")
    cost_rate: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    cost_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="QAR")
    cost_unit: Mapped[str] = mapped_column(String(16), nullable=False, default="MONTHLY")

    visa_cost: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    insurance_cost: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    other_cost: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)

    #: Per deployment, because a support contract and a project engagement do
    #: not share a working calendar.
    working_days_per_month: Mapped[int] = mapped_column(nullable=False, default=22)
    hours_per_day: Mapped[int] = mapped_column(nullable=False, default=8)

    #: An extension is a new deployment linked to its predecessor, not an edit
    #: of the old one — the original's billing history has to stay intact.
    extension_of_deployment_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("deployments.id", ondelete="SET NULL"), nullable=True, index=True
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    end_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    billing_records: Mapped[list[BillingRecord]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
        lazy="raise",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_deployments_status_end", "status", "end_date"),
        Index("ix_deployments_account_status", "account_id", "status"),
    )

    @property
    def effective_end(self) -> date | None:
        """What actually happened, falling back to the plan."""
        return self.actual_end_date or self.end_date

    @property
    def is_billable(self) -> bool:
        return self.status in {DeploymentStatus.ACTIVE, DeploymentStatus.PENDING_START}


class BillingRecord(BaseEntity):
    """One deployment, one calendar month."""

    __tablename__ = "billing_records"

    deployment_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("deployments.id", ondelete="CASCADE"), nullable=False, index=True
    )

    period_year: Mapped[int] = mapped_column(nullable=False, index=True)
    period_month: Mapped[int] = mapped_column(nullable=False, index=True)

    revenue_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    cost_amount: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    gross_profit: Mapped[Decimal] = mapped_column(MoneyType, nullable=False)
    margin_percent: Mapped[float | None] = mapped_column(nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="QAR")

    status: Mapped[BillingStatus] = mapped_column(
        StrEnumType(BillingStatus), nullable=False, default=BillingStatus.PROJECTED, index=True
    )
    #: True when the month was pro-rated for a partial period, so nobody reads a
    #: half-month figure as a full one.
    is_estimated: Mapped[bool] = mapped_column(default=True, nullable=False)
    #: Working days actually covered, kept so a pro-rated figure can be audited.
    billable_days: Mapped[int | None] = mapped_column(nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    deployment: Mapped[Deployment] = relationship(back_populates="billing_records", lazy="raise")

    __table_args__ = (
        UniqueConstraint(
            "deployment_id", "period_year", "period_month", name="billing_period_unique"
        ),
        Index("ix_billing_period", "period_year", "period_month", "status"),
    )

    @property
    def period_label(self) -> str:
        return f"{self.period_year:04d}-{self.period_month:02d}"


__all__ = [
    "REALISED_BILLING_STATUSES",
    "BillingRecord",
    "BillingStatus",
    "Deployment",
    "DeploymentStatus",
]
