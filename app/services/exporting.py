"""Excel export.

Exports respect the same field permissions the API does. A spreadsheet is the
easiest way to move data out of a system, so an export that ignored role
boundaries would make the whole RBAC layer decorative — a salesperson would
simply download the consultant cost rates the UI refuses to show them.

Restricted columns are **omitted entirely**, not blanked, so nobody has to guess
whether an empty cell means "no value" or "not allowed".
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.core.logging import log_business_event
from app.core.permissions import Permission, permissions_for
from app.engines.importing.workbook import write_sheet
from app.models.accounts import Account, Contact, Project
from app.models.delivery import BillingRecord, Deployment
from app.models.demand import Requirement
from app.models.identity import AuditAction, User
from app.models.platform import ImportEntity
from app.models.talent import Resource
from app.services.audit import AuditService


class ExportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)

    async def export(self, entity: ImportEntity, *, actor: User) -> tuple[bytes, str]:
        granted = permissions_for(actor.role)
        builders = {
            ImportEntity.CUSTOMERS: self._customers,
            ImportEntity.CONTACTS: self._contacts,
            ImportEntity.PROJECTS: self._projects,
            ImportEntity.REQUIREMENTS: self._requirements,
            ImportEntity.RESOURCES: self._resources,
            ImportEntity.DEPLOYMENTS: self._deployments,
            ImportEntity.BILLING: self._billing,
        }
        builder = builders.get(entity)
        if builder is None:
            raise ValidationError(
                f"Export is not available for {entity.value}.",
                details=[{"field": "entity", "message": "Unsupported"}],
            )

        headers, rows = await builder(granted)
        payload = write_sheet(title=entity.value, headers=headers, rows=rows)

        await self.audit.record(
            AuditAction.EXPORT_GENERATED,
            summary=f"Exported {len(rows)} {entity.value} rows",
            actor=actor,
            entity_type="export",
            entity_id=None,
        )
        log_business_event("export_generated", entity=entity.value, rows=len(rows))

        stamp = date.today().isoformat()
        return payload, f"glimmora-{entity.value}-{stamp}.xlsx"

    # ------------------------------------------------------------ builders
    async def _customers(self, granted: frozenset[Permission]) -> tuple[list[str], list[list[Any]]]:
        rows = await self.session.execute(
            select(Account).where(Account.deleted_at.is_(None)).order_by(Account.name)
        )
        headers = [
            "Name",
            "Account type",
            "Country",
            "City",
            "Industry",
            "Relationship status",
            "Existing customer",
            "Existing partner",
            "Approved vendor",
            "Has MSA",
            "Outsourcing friendly",
            "Payment terms (days)",
        ]
        return headers, [
            [
                a.name,
                a.account_type.value,
                a.country,
                a.city,
                a.industry,
                a.relationship_status.value,
                a.is_existing_customer,
                a.is_existing_partner,
                a.is_approved_vendor,
                a.has_msa,
                a.contract_outsourcing_friendly,
                a.payment_terms_days,
            ]
            for a in rows.scalars().all()
        ]

    async def _contacts(self, granted: frozenset[Permission]) -> tuple[list[str], list[list[Any]]]:
        rows = await self.session.execute(
            select(Contact, Account.name)
            .join(Account, Account.id == Contact.account_id)
            .order_by(Account.name, Contact.full_name)
        )
        headers = ["Account name", "Full name", "Job title", "Email", "Phone", "Decision maker"]
        return headers, [
            [account_name, c.full_name, c.job_title, c.email, c.phone, c.is_decision_maker]
            for c, account_name in rows
        ]

    async def _projects(self, granted: frozenset[Permission]) -> tuple[list[str], list[list[Any]]]:
        rows = await self.session.execute(
            select(Project, Account.name)
            .join(Account, Account.id == Project.account_id)
            .order_by(Account.name, Project.name)
        )
        headers = ["Account name", "Project name", "Status", "Start date", "End date"]
        return headers, [
            [account_name, p.name, p.status.value, p.start_date, p.end_date]
            for p, account_name in rows
        ]

    async def _requirements(
        self, granted: frozenset[Permission]
    ) -> tuple[list[str], list[list[Any]]]:
        can_see_rate = Permission.FIELD_BILLING_RATE in granted
        rows = await self.session.execute(
            select(Requirement)
            .where(Requirement.deleted_at.is_(None))
            .order_by(Requirement.created_at.desc())
        )

        headers = [
            "Title",
            "Role",
            "Status",
            "Priority source",
            "Country",
            "Location",
            "Positions",
            "Minimum experience",
            "Duration (months)",
        ]
        if can_see_rate:
            headers += ["Rate from", "Rate to", "Rate currency", "Rate unit"]

        body = []
        for r in rows.scalars().all():
            record: list[Any] = [
                r.title,
                r.role,
                r.status.value,
                r.priority_source.value,
                r.country,
                r.location,
                r.positions,
                r.experience_min_years,
                r.duration_months,
            ]
            if can_see_rate:
                record += [
                    r.rate_min,
                    r.rate_max,
                    r.rate_currency,
                    r.rate_unit.value if r.rate_unit else None,
                ]
            body.append(record)
        return headers, body

    async def _resources(self, granted: frozenset[Permission]) -> tuple[list[str], list[list[Any]]]:
        # The consultant's cost rate is the field Sales must never see; an
        # export that included it would be the easiest RBAC bypass in the system.
        can_see_cost = Permission.FIELD_RESOURCE_COST in granted
        rows = await self.session.execute(
            select(Resource).where(Resource.deleted_at.is_(None)).order_by(Resource.full_name)
        )

        headers = [
            "Full name",
            "Resource type",
            "Headline",
            "Email",
            "Phone",
            "Country",
            "City",
            "Availability",
            "Available from",
            "Notice period (days)",
            "Total experience (years)",
        ]
        if can_see_cost:
            headers += ["Expected cost", "Cost currency", "Cost unit"]

        body = []
        for r in rows.scalars().all():
            record: list[Any] = [
                r.full_name,
                r.resource_type.value,
                r.headline,
                r.email,
                r.phone,
                r.current_location_country,
                r.current_location_city,
                r.availability_status.value,
                r.available_from,
                r.notice_period_days,
                r.total_experience_years,
            ]
            if can_see_cost:
                record += [r.expected_cost_amount, r.expected_cost_currency, r.expected_cost_unit]
            body.append(record)
        return headers, body

    async def _deployments(
        self, granted: frozenset[Permission]
    ) -> tuple[list[str], list[list[Any]]]:
        can_see_cost = Permission.FIELD_RESOURCE_COST in granted
        can_see_bill = Permission.FIELD_BILLING_RATE in granted

        rows = await self.session.execute(
            select(Deployment, Resource.full_name)
            .join(Resource, Resource.id == Deployment.resource_id)
            .order_by(Deployment.start_date.desc())
        )

        headers = ["Consultant", "Role", "Status", "Start date", "End date", "Actual end date"]
        if can_see_bill:
            headers += ["Bill rate", "Bill currency", "Bill unit"]
        if can_see_cost:
            headers += ["Cost rate", "Cost currency", "Cost unit"]

        body = []
        for d, name in rows:
            record: list[Any] = [
                name,
                d.role_title,
                d.status.value,
                d.start_date,
                d.end_date,
                d.actual_end_date,
            ]
            if can_see_bill:
                record += [d.bill_rate, d.bill_currency, d.bill_unit]
            if can_see_cost:
                record += [d.cost_rate, d.cost_currency, d.cost_unit]
            body.append(record)
        return headers, body

    async def _billing(self, granted: frozenset[Permission]) -> tuple[list[str], list[list[Any]]]:
        rows = await self.session.execute(
            select(BillingRecord, Resource.full_name, Deployment.role_title)
            .join(Deployment, Deployment.id == BillingRecord.deployment_id)
            .join(Resource, Resource.id == Deployment.resource_id)
            .order_by(BillingRecord.period_year.desc(), BillingRecord.period_month.desc())
        )
        headers = [
            "Period",
            "Consultant",
            "Role",
            "Status",
            "Revenue",
            "Cost",
            "Gross profit",
            "Margin %",
            "Currency",
            "Billable days",
            "Estimated",
        ]
        return headers, [
            [
                b.period_label,
                name,
                role,
                # The status column is not decoration: a projected row in a
                # spreadsheet must not be mistaken for earned revenue.
                b.status.value,
                b.revenue_amount,
                b.cost_amount,
                b.gross_profit,
                b.margin_percent,
                b.currency,
                b.billable_days,
                b.is_estimated,
            ]
            for b, name, role in rows
        ]


__all__ = ["ExportService"]
