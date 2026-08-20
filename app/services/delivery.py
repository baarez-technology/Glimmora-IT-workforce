"""Deployments and billing.

This is where the platform's headline claim — *monthly billable revenue
generated through the engine* — becomes a number somebody can check. Two rules
protect it:

* **Projections are never counted as revenue.** They are generated, labelled
  PROJECTED, and shown alongside confirmed figures rather than added to them
  (ASSUMPTIONS.md A15).
* **Regeneration never overwrites a human's work.** A confirmed or invoiced
  month is left exactly as it is; only projections are refreshed.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import log_business_event
from app.db.types import utcnow
from app.engines.billing.periods import (
    Period,
    amounts_for,
    coverage,
    periods_between,
)
from app.engines.scoring.commercial import to_monthly
from app.models.delivery import (
    REALISED_BILLING_STATUSES,
    BillingRecord,
    BillingStatus,
    Deployment,
    DeploymentStatus,
)
from app.models.demand import Requirement
from app.models.identity import AuditAction, User
from app.models.pipeline import Submission, SubmissionStatus
from app.models.talent import AvailabilityStatus, Resource
from app.services.audit import AuditService

#: How far ahead an open-ended deployment is projected. Projecting to infinity
#: would produce a revenue figure nobody could defend.
OPEN_ENDED_HORIZON_MONTHS = 12


class DeploymentService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)

    async def get(self, deployment_id: uuid.UUID) -> Deployment:
        deployment = (
            await self.session.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one_or_none()
        if deployment is None:
            raise NotFoundError("deployment", deployment_id)
        return deployment

    async def create_from_submission(
        self,
        submission_id: uuid.UUID,
        *,
        actor: User,
        start_date: date,
        end_date: date | None = None,
        role_title: str | None = None,
        bill_rate: Decimal | None = None,
        cost_rate: Decimal | None = None,
        **overrides: Any,
    ) -> Deployment:
        """Turn a selected candidate into a deployment.

        The rates default from the submission and the consultant, but are
        **copied**, not referenced: a rate renegotiated in June must not silently
        rewrite March's billing.
        """
        submission = (
            await self.session.execute(select(Submission).where(Submission.id == submission_id))
        ).scalar_one_or_none()
        if submission is None:
            raise NotFoundError("submission", submission_id)

        if submission.status is not SubmissionStatus.SELECTED:
            raise ValidationError(
                "Only a selected candidate can be deployed. This submission is "
                f"{submission.status.value.lower()}.",
                details=[{"field": "submission_id", "message": "Not selected"}],
            )

        existing = (
            (
                await self.session.execute(
                    select(Deployment).where(Deployment.submission_id == submission_id)
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            raise ConflictError(
                "This submission has already been deployed.",
                details=[{"field": "submission_id", "message": str(existing.id)}],
            )

        requirement = (
            await self.session.execute(
                select(Requirement).where(Requirement.id == submission.requirement_id)
            )
        ).scalar_one_or_none()
        resource = (
            await self.session.execute(
                select(Resource).where(Resource.id == submission.resource_id)
            )
        ).scalar_one_or_none()
        if resource is None:
            raise NotFoundError("resource", submission.resource_id)

        deployment = Deployment(
            resource_id=resource.id,
            account_id=requirement.account_id if requirement else None,
            end_customer_id=requirement.end_customer_id if requirement else None,
            project_id=requirement.project_id if requirement else None,
            requirement_id=submission.requirement_id,
            submission_id=submission_id,
            role_title=role_title or (requirement.title if requirement else resource.full_name),
            location=requirement.location if requirement else None,
            start_date=start_date,
            end_date=end_date
            or (
                start_date + timedelta(days=30 * requirement.duration_months)
                if requirement and requirement.duration_months
                else None
            ),
            status=(
                DeploymentStatus.ACTIVE
                if start_date <= utcnow().date()
                else DeploymentStatus.PENDING_START
            ),
            bill_rate=bill_rate or submission.proposed_bill_rate,
            bill_currency=submission.proposed_bill_currency
            or (requirement.rate_currency if requirement else None)
            or "QAR",
            bill_unit=submission.proposed_bill_unit
            or (requirement.rate_unit.value if requirement and requirement.rate_unit else None)
            or "MONTHLY",
            cost_rate=cost_rate or resource.expected_cost_amount,
            cost_currency=resource.expected_cost_currency or "QAR",
            cost_unit=resource.expected_cost_unit or "MONTHLY",
            created_by=actor.id,
            **overrides,
        )
        self.session.add(deployment)
        await self.session.flush()

        await self._sync_resource_availability(deployment)

        await self.audit.record(
            AuditAction.DEPLOYMENT_CREATED,
            summary=f"Deployed {resource.full_name} as {deployment.role_title}",
            actor=actor,
            entity_type="deployment",
            entity_id=deployment.id,
        )
        log_business_event("deployment_created", deployment_id=str(deployment.id))
        return deployment

    async def _sync_resource_availability(self, deployment: Deployment) -> None:
        """Keep the talent cloud honest about who is actually billing.

        Without this the bench radar would keep offering a consultant who is
        already placed, which is exactly the mistake Phase 8 exists to prevent.
        """
        resource = (
            await self.session.execute(
                select(Resource).where(Resource.id == deployment.resource_id)
            )
        ).scalar_one_or_none()
        if resource is None:
            return

        if deployment.status in {DeploymentStatus.ACTIVE, DeploymentStatus.PENDING_START}:
            resource.availability_status = (
                AvailabilityStatus.DEPLOYED
                if deployment.status is DeploymentStatus.ACTIVE
                else AvailabilityStatus.AVAILABLE_SOON
            )
            # The end date is what the zero-bench sweep reads.
            resource.available_from = deployment.effective_end
        elif deployment.status is DeploymentStatus.ENDED:
            ended = deployment.actual_end_date or deployment.end_date
            today = utcnow().date()
            resource.availability_status = (
                AvailabilityStatus.AVAILABLE
                if ended is None or ended <= today
                else AvailabilityStatus.AVAILABLE_SOON
            )
            resource.available_from = ended

    async def end(
        self,
        deployment: Deployment,
        *,
        actor: User,
        actual_end_date: date,
        reason: str | None = None,
    ) -> Deployment:
        if actual_end_date < deployment.start_date:
            raise ValidationError(
                "A deployment cannot end before it started.",
                details=[{"field": "actual_end_date", "message": "Before the start date"}],
            )

        deployment.actual_end_date = actual_end_date
        deployment.status = DeploymentStatus.ENDED
        deployment.end_reason = reason
        await self._sync_resource_availability(deployment)

        # Months after the real end never happened. Cancel the projections
        # rather than deleting them, so the change is visible.
        cancelled = await self._cancel_projections_after(deployment, actual_end_date)

        await self.audit.record(
            AuditAction.DEPLOYMENT_ENDED,
            summary=f"Ended on {actual_end_date:%d %b %Y}" + (f" — {reason}" if reason else ""),
            actor=actor,
            entity_type="deployment",
            entity_id=deployment.id,
        )
        log_business_event(
            "deployment_created",
            deployment_id=str(deployment.id),
            cancelled_projections=cancelled,
        )
        await self.session.flush()
        return deployment

    async def _cancel_projections_after(self, deployment: Deployment, ended: date) -> int:
        rows = await self.session.execute(
            select(BillingRecord).where(
                BillingRecord.deployment_id == deployment.id,
                BillingRecord.status == BillingStatus.PROJECTED,
            )
        )
        cancelled = 0
        for record in rows.scalars().all():
            period_start = date(record.period_year, record.period_month, 1)
            if period_start > ended:
                record.status = BillingStatus.CANCELLED
                record.notes = "Cancelled — deployment ended before this period"
                cancelled += 1
        return cancelled

    async def extend(
        self,
        deployment: Deployment,
        *,
        actor: User,
        start_date: date,
        end_date: date | None,
        bill_rate: Decimal | None = None,
        cost_rate: Decimal | None = None,
    ) -> Deployment:
        """A linked successor, not an edit.

        Extending by moving the original's end date would silently rewrite its
        billing history and lose the fact that a renegotiation happened.
        """
        if deployment.effective_end and start_date <= deployment.effective_end:
            raise ValidationError(
                "An extension must start after the current deployment ends "
                f"({deployment.effective_end:%d %b %Y}).",
                details=[{"field": "start_date", "message": "Overlaps the current deployment"}],
            )

        successor = Deployment(
            resource_id=deployment.resource_id,
            account_id=deployment.account_id,
            end_customer_id=deployment.end_customer_id,
            project_id=deployment.project_id,
            requirement_id=deployment.requirement_id,
            submission_id=None,
            role_title=deployment.role_title,
            location=deployment.location,
            start_date=start_date,
            end_date=end_date,
            status=(
                DeploymentStatus.ACTIVE
                if start_date <= utcnow().date()
                else DeploymentStatus.PENDING_START
            ),
            bill_rate=bill_rate if bill_rate is not None else deployment.bill_rate,
            bill_currency=deployment.bill_currency,
            bill_unit=deployment.bill_unit,
            cost_rate=cost_rate if cost_rate is not None else deployment.cost_rate,
            cost_currency=deployment.cost_currency,
            cost_unit=deployment.cost_unit,
            working_days_per_month=deployment.working_days_per_month,
            hours_per_day=deployment.hours_per_day,
            extension_of_deployment_id=deployment.id,
            created_by=actor.id,
        )
        self.session.add(successor)
        await self.session.flush()
        await self._sync_resource_availability(successor)

        await self.audit.record(
            AuditAction.DEPLOYMENT_CREATED,
            summary=f"Extension of {deployment.role_title} from {start_date:%d %b %Y}",
            actor=actor,
            entity_type="deployment",
            entity_id=successor.id,
        )
        return successor

    async def list_deployments(
        self,
        *,
        status: DeploymentStatus | None = None,
        account_id: uuid.UUID | None = None,
        resource_id: uuid.UUID | None = None,
        ending_before: date | None = None,
    ) -> list[Deployment]:
        stmt = select(Deployment)
        if status is not None:
            stmt = stmt.where(Deployment.status == status)
        if account_id is not None:
            stmt = stmt.where(Deployment.account_id == account_id)
        if resource_id is not None:
            stmt = stmt.where(Deployment.resource_id == resource_id)
        if ending_before is not None:
            stmt = stmt.where(
                Deployment.end_date.is_not(None), Deployment.end_date <= ending_before
            )
        stmt = stmt.order_by(Deployment.start_date.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def ending_soon(self, *, days_ahead: int = 90) -> list[tuple[Deployment, int]]:
        """Active deployments approaching their end, soonest first."""
        today = utcnow().date()
        horizon = today + timedelta(days=days_ahead)
        rows = await self.session.execute(
            select(Deployment).where(
                Deployment.status == DeploymentStatus.ACTIVE,
                Deployment.end_date.is_not(None),
                Deployment.end_date <= horizon,
            )
        )
        board = [
            (deployment, (deployment.end_date - today).days)
            for deployment in rows.scalars().all()
            if deployment.end_date is not None
        ]
        board.sort(key=lambda row: row[1])
        return board


class BillingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)

    # ---------------------------------------------------------- projections
    def _monthly_rates(self, deployment: Deployment) -> tuple[Decimal | None, Decimal | None]:
        """Normalise the deployment's own rate snapshot onto a monthly basis."""
        revenue = to_monthly(
            deployment.bill_rate,
            deployment.bill_unit,
            working_days=deployment.working_days_per_month,
            hours_per_day=deployment.hours_per_day,
        )
        base_cost = to_monthly(
            deployment.cost_rate,
            deployment.cost_unit,
            working_days=deployment.working_days_per_month,
            hours_per_day=deployment.hours_per_day,
        )
        if base_cost is None:
            return revenue, None

        one_off = (
            (deployment.visa_cost or Decimal("0"))
            + (deployment.insurance_cost or Decimal("0"))
            + (deployment.other_cost or Decimal("0"))
        )
        months = len(self._projection_periods(deployment)) or 1
        return revenue, base_cost + (one_off / months)

    def _projection_periods(self, deployment: Deployment) -> list[Period]:
        end = deployment.effective_end
        if end is None:
            # Open-ended: project a bounded horizon rather than to infinity.
            end = deployment.start_date + timedelta(days=30 * OPEN_ENDED_HORIZON_MONTHS)
        return periods_between(deployment.start_date, end)

    async def generate_projections(self, deployment: Deployment, *, actor: User) -> dict[str, int]:
        """Create or refresh PROJECTED rows for every month the deployment spans.

        Idempotent, and it never touches a row a human has confirmed.
        """
        revenue, cost = self._monthly_rates(deployment)
        if revenue is None:
            raise ValidationError(
                "This deployment has no bill rate, so revenue cannot be projected.",
                details=[{"field": "bill_rate", "message": "Required to project billing"}],
            )

        existing = {
            (record.period_year, record.period_month): record
            for record in (
                await self.session.execute(
                    select(BillingRecord).where(BillingRecord.deployment_id == deployment.id)
                )
            )
            .scalars()
            .all()
        }

        created = updated = protected = 0
        for period in self._projection_periods(deployment):
            cover = coverage(period, start=deployment.start_date, end=deployment.effective_end)
            if cover is None:
                continue

            amounts = amounts_for(cover, monthly_revenue=revenue, monthly_cost=cost or Decimal("0"))
            record = existing.get((period.year, period.month))

            if record is not None:
                if record.status in REALISED_BILLING_STATUSES:
                    # A human has checked this month against reality. Leave it.
                    protected += 1
                    continue
                record.revenue_amount = amounts.revenue
                record.cost_amount = amounts.cost
                record.gross_profit = amounts.gross_profit
                record.margin_percent = amounts.margin_percent
                record.billable_days = amounts.billable_days
                record.is_estimated = True
                record.status = BillingStatus.PROJECTED
                updated += 1
                continue

            self.session.add(
                BillingRecord(
                    deployment_id=deployment.id,
                    period_year=period.year,
                    period_month=period.month,
                    revenue_amount=amounts.revenue,
                    cost_amount=amounts.cost,
                    gross_profit=amounts.gross_profit,
                    margin_percent=amounts.margin_percent,
                    currency=deployment.bill_currency,
                    status=BillingStatus.PROJECTED,
                    is_estimated=True,
                    billable_days=amounts.billable_days,
                    created_by=actor.id,
                )
            )
            created += 1

        await self.session.flush()
        log_business_event(
            "billing_created",
            deployment_id=str(deployment.id),
            created=created,
            updated=updated,
        )
        return {"created": created, "updated": updated, "protected": protected}

    async def generate_for_all(self, *, actor: User) -> dict[str, int]:
        rows = await self.session.execute(
            select(Deployment).where(
                Deployment.status.in_([DeploymentStatus.ACTIVE, DeploymentStatus.PENDING_START]),
                Deployment.bill_rate.is_not(None),
            )
        )
        totals = {"deployments": 0, "created": 0, "updated": 0, "protected": 0}
        for deployment in rows.scalars().all():
            result = await self.generate_projections(deployment, actor=actor)
            totals["deployments"] += 1
            for key in ("created", "updated", "protected"):
                totals[key] += result[key]
        return totals

    # -------------------------------------------------------------- records
    async def get(self, record_id: uuid.UUID) -> BillingRecord:
        record = (
            await self.session.execute(select(BillingRecord).where(BillingRecord.id == record_id))
        ).scalar_one_or_none()
        if record is None:
            raise NotFoundError("billing record", record_id)
        return record

    async def create_record(
        self,
        *,
        deployment_id: uuid.UUID,
        period_year: int,
        period_month: int,
        revenue_amount: Decimal,
        cost_amount: Decimal,
        actor: User,
        status: BillingStatus = BillingStatus.CONFIRMED,
        notes: str | None = None,
    ) -> BillingRecord:
        """Record a month by hand.

        The SOW asks for billing to be "lightweight — manual entry or Excel
        import" (ASSUMPTIONS.md A15). Projections cover the common case, but a
        month that predates the system, an ad-hoc invoice, or a deployment
        nobody projected still has to be recordable — otherwise the headline
        revenue figure is only ever as complete as the projection generator.

        Entered by hand it is **not** an estimate, so `is_estimated` is false
        and the default status is CONFIRMED: a human typed what actually
        happened.
        """
        deployment = (
            await self.session.execute(select(Deployment).where(Deployment.id == deployment_id))
        ).scalar_one_or_none()
        if deployment is None:
            raise NotFoundError("deployment", deployment_id)

        existing = (
            (
                await self.session.execute(
                    select(BillingRecord).where(
                        BillingRecord.deployment_id == deployment_id,
                        BillingRecord.period_year == period_year,
                        BillingRecord.period_month == period_month,
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            raise ConflictError(
                f"{period_year:04d}-{period_month:02d} already has a billing row for "
                "this deployment. Edit it rather than adding a second.",
                details=[{"field": "period", "message": str(existing.id)}],
            )

        profit = revenue_amount - cost_amount
        record = BillingRecord(
            deployment_id=deployment_id,
            period_year=period_year,
            period_month=period_month,
            revenue_amount=revenue_amount,
            cost_amount=cost_amount,
            gross_profit=profit,
            margin_percent=(float(profit / revenue_amount * 100) if revenue_amount > 0 else None),
            currency=deployment.bill_currency,
            status=status,
            is_estimated=False,
            notes=notes,
            created_by=actor.id,
            confirmed_by=actor.id if status in REALISED_BILLING_STATUSES else None,
        )
        self.session.add(record)
        await self.session.flush()

        await self.audit.record(
            AuditAction.BILLING_CREATED,
            summary=(f"Entered {record.period_label} by hand: {revenue_amount} {record.currency}"),
            actor=actor,
            entity_type="billing_record",
            entity_id=record.id,
        )
        log_business_event("billing_created", record_id=str(record.id), manual=True)
        return record

    async def update_record(
        self,
        record: BillingRecord,
        *,
        actor: User,
        revenue_amount: Decimal | None = None,
        cost_amount: Decimal | None = None,
        status: BillingStatus | None = None,
        notes: str | None = None,
    ) -> BillingRecord:
        """Correct a month. Profit and margin are always re-derived."""
        if revenue_amount is not None:
            record.revenue_amount = revenue_amount
        if cost_amount is not None:
            record.cost_amount = cost_amount
        if notes is not None:
            record.notes = notes
        if status is not None:
            record.status = status
            if status in REALISED_BILLING_STATUSES:
                record.confirmed_by = actor.id
                record.is_estimated = False

        # Never trust a supplied profit: it is arithmetic, and letting a caller
        # set it independently is how a total stops reconciling.
        record.gross_profit = record.revenue_amount - record.cost_amount
        record.margin_percent = (
            float(record.gross_profit / record.revenue_amount * 100)
            if record.revenue_amount > 0
            else None
        )

        await self.audit.record(
            AuditAction.BILLING_UPDATED,
            summary=f"Adjusted {record.period_label}: {record.revenue_amount} {record.currency}",
            actor=actor,
            entity_type="billing_record",
            entity_id=record.id,
        )
        await self.session.flush()
        return record

    async def confirm(
        self,
        record: BillingRecord,
        *,
        actor: User,
        revenue_amount: Decimal | None = None,
        cost_amount: Decimal | None = None,
        notes: str | None = None,
    ) -> BillingRecord:
        """Turn a projection into a real number.

        The user may correct the figures while confirming — the projection was
        arithmetic, the confirmation is what actually happened.
        """
        if record.status is BillingStatus.CANCELLED:
            raise ValidationError(
                "A cancelled period cannot be confirmed.",
                details=[{"field": "status", "message": "Cancelled"}],
            )

        if revenue_amount is not None:
            record.revenue_amount = revenue_amount
        if cost_amount is not None:
            record.cost_amount = cost_amount

        record.gross_profit = record.revenue_amount - record.cost_amount
        record.margin_percent = (
            float(record.gross_profit / record.revenue_amount * 100)
            if record.revenue_amount > 0
            else None
        )
        record.status = BillingStatus.CONFIRMED
        record.is_estimated = False
        record.confirmed_by = actor.id
        if notes:
            record.notes = notes

        await self.audit.record(
            AuditAction.BILLING_CONFIRMED,
            summary=f"Confirmed {record.period_label}: {record.revenue_amount} {record.currency}",
            actor=actor,
            entity_type="billing_record",
            entity_id=record.id,
        )
        await self.session.flush()
        return record

    async def list_records(
        self,
        *,
        year: int | None = None,
        month: int | None = None,
        status: BillingStatus | None = None,
        deployment_id: uuid.UUID | None = None,
        limit: int = 200,
    ) -> list[BillingRecord]:
        stmt = select(BillingRecord)
        if year is not None:
            stmt = stmt.where(BillingRecord.period_year == year)
        if month is not None:
            stmt = stmt.where(BillingRecord.period_month == month)
        if status is not None:
            stmt = stmt.where(BillingRecord.status == status)
        if deployment_id is not None:
            stmt = stmt.where(BillingRecord.deployment_id == deployment_id)
        stmt = stmt.order_by(
            BillingRecord.period_year.desc(), BillingRecord.period_month.desc()
        ).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    # -------------------------------------------------------------- summary
    async def monthly_summary(self, *, months: int = 12) -> list[dict[str, Any]]:
        """Revenue by month, with confirmed and projected kept apart.

        Merging them would be the single most misleading thing this platform
        could do (ASSUMPTIONS.md A15).
        """
        rows = await self.session.execute(
            select(
                BillingRecord.period_year,
                BillingRecord.period_month,
                BillingRecord.status,
                func.sum(BillingRecord.revenue_amount),
                func.sum(BillingRecord.cost_amount),
                func.sum(BillingRecord.gross_profit),
                func.count(),
            ).group_by(BillingRecord.period_year, BillingRecord.period_month, BillingRecord.status)
        )

        buckets: dict[tuple[int, int], dict[str, Any]] = {}
        for year, month, status, revenue, cost, profit, count in rows:
            key = (year, month)
            bucket = buckets.setdefault(
                key,
                {
                    "period": f"{year:04d}-{month:02d}",
                    "year": year,
                    "month": month,
                    "confirmed_revenue": Decimal("0"),
                    "confirmed_cost": Decimal("0"),
                    "confirmed_profit": Decimal("0"),
                    "projected_revenue": Decimal("0"),
                    "projected_cost": Decimal("0"),
                    "projected_profit": Decimal("0"),
                    "records": 0,
                },
            )
            bucket["records"] += count
            if status in REALISED_BILLING_STATUSES:
                bucket["confirmed_revenue"] += revenue or Decimal("0")
                bucket["confirmed_cost"] += cost or Decimal("0")
                bucket["confirmed_profit"] += profit or Decimal("0")
            elif status is BillingStatus.PROJECTED:
                bucket["projected_revenue"] += revenue or Decimal("0")
                bucket["projected_cost"] += cost or Decimal("0")
                bucket["projected_profit"] += profit or Decimal("0")

        ordered = sorted(buckets.values(), key=lambda item: (item["year"], item["month"]))
        for bucket in ordered:
            revenue = bucket["confirmed_revenue"]
            bucket["confirmed_margin_percent"] = (
                float(bucket["confirmed_profit"] / revenue * 100) if revenue > 0 else None
            )
        return ordered[-months:]

    async def headline(self) -> dict[str, Any]:
        """The number the platform is judged on, and its honest caveats.

        Anchored to the **current calendar month**, not to the newest period
        that happens to hold data. Projections run months ahead, so "the latest
        row" would routinely present a future forecast as this month's revenue.
        """
        today = utcnow().date()
        summary = await self.monthly_summary(months=600)
        current = next(
            (row for row in summary if row["year"] == today.year and row["month"] == today.month),
            None,
        )

        totals = (
            await self.session.execute(
                select(
                    func.sum(BillingRecord.revenue_amount),
                    func.sum(BillingRecord.gross_profit),
                ).where(BillingRecord.status.in_(list(REALISED_BILLING_STATUSES)))
            )
        ).one()

        unconfirmed = (
            await self.session.execute(
                select(func.count())
                .select_from(BillingRecord)
                .where(BillingRecord.status == BillingStatus.PROJECTED)
            )
        ).scalar() or 0

        return {
            # Always name the month, even with nothing billed in it — a blank
            # period reads as a broken dashboard rather than a quiet month.
            "period": f"{today.year:04d}-{today.month:02d}",
            "confirmed_revenue": current["confirmed_revenue"] if current else Decimal("0"),
            "projected_revenue": current["projected_revenue"] if current else Decimal("0"),
            "confirmed_margin_percent": current["confirmed_margin_percent"] if current else None,
            "lifetime_confirmed_revenue": totals[0] or Decimal("0"),
            "lifetime_gross_profit": totals[1] or Decimal("0"),
            "unconfirmed_periods": unconfirmed,
        }


__all__ = ["OPEN_ENDED_HORIZON_MONTHS", "BillingService", "DeploymentService"]
