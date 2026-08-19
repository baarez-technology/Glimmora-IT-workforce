"""Account, routing, contact, project and activity services."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.pagination import PageParams
from app.db.types import utcnow
from app.models.accounts import (
    Account,
    AccountRelationship,
    AccountType,
    Activity,
    Contact,
    Project,
)
from app.models.identity import AuditAction, User
from app.repositories.accounts import (
    AccountRelationshipRepository,
    AccountRepository,
    ActivityRepository,
    ContactRepository,
    ProjectRepository,
    TechnologyRepository,
)
from app.schemas.accounts import (
    AccountCreate,
    AccountRouteCreate,
    AccountUpdate,
    ActivityCreate,
    ActivityUpdate,
    AddressabilitySignals,
    ContactCreate,
    ContactUpdate,
    ProjectCreate,
    ProjectUpdate,
)
from app.services.audit import AuditService, build_diff

AUDITED_ACCOUNT_FIELDS = {
    "name",
    "account_type",
    "relationship_status",
    "country",
    "is_existing_customer",
    "is_existing_partner",
    "is_approved_vendor",
    "has_msa",
    "contract_outsourcing_friendly",
    "owner_id",
}

#: Human-readable labels for the addressability facts a user still has to confirm.
MISSING_SIGNAL_LABELS = {
    "contract_outsourcing_friendly": (
        "Confirm whether this account buys contract or outsourced resources"
    ),
    "existing_customer": "Not recorded as an existing customer",
    "partner_or_prime_route": "No partner or prime route recorded",
    "approved_vendor": "Not recorded as an approved vendor and no MSA",
    "decision_maker_known": "No decision maker identified",
}


class AccountService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.accounts = AccountRepository(session)
        self.routes = AccountRelationshipRepository(session)
        self.contacts = ContactRepository(session)
        self.audit = AuditService(session)

    # ------------------------------------------------------------- reading
    async def list_accounts(self, params: PageParams, **filters: object):
        return await self.accounts.list_accounts(params, **filters)  # type: ignore[arg-type]

    async def get_account(self, account_id: uuid.UUID) -> Account:
        account = await self.accounts.get(account_id)
        if account is None:
            raise NotFoundError("account", account_id)
        return account

    async def addressability_signals(self, account: Account) -> AddressabilitySignals:
        """Preview which Addressability inputs this account currently satisfies.

        Not a score — Phase 9 owns that. The value here is showing a user *now*
        which commercial facts are still unrecorded, because an account with
        blank flags will score badly for the wrong reason (SCORING.md section 1).
        """
        routes = await self.routes.routes_from(account.id)
        has_route = any(route.is_preferred_route for route in routes) or account.is_existing_partner
        has_decision_maker = await self.accounts.has_decision_maker(account.id)

        signals = {
            "contract_outsourcing_friendly": account.contract_outsourcing_friendly,
            "existing_customer": account.is_existing_customer,
            "partner_or_prime_route": has_route,
            "approved_vendor": account.is_approved_vendor or account.has_msa,
            "decision_maker_known": has_decision_maker,
        }

        return AddressabilitySignals(
            **signals,
            signals_met=sum(1 for value in signals.values() if value),
            signals_total=len(signals),
            missing=[MISSING_SIGNAL_LABELS[key] for key, value in signals.items() if not value],
        )

    # ------------------------------------------------------------- writing
    async def create_account(self, payload: AccountCreate, *, actor: User) -> Account:
        if await self.accounts.name_taken(payload.name, payload.country):
            raise ConflictError(
                "An account with that name already exists in this country.",
                details=[{"field": "name", "message": "Already in use"}],
            )

        account = Account(**payload.model_dump())
        # An unowned account is an account nobody follows up.
        account.owner_id = account.owner_id or actor.id
        await self.accounts.add(account)

        await self.audit.record(
            AuditAction.ACCOUNT_CREATED,
            summary=f"Created {account.account_type.value.lower()} account {account.name}",
            actor=actor,
            entity_type="account",
            entity_id=account.id,
        )
        return account

    async def update_account(
        self, account_id: uuid.UUID, payload: AccountUpdate, *, actor: User
    ) -> Account:
        account = await self.get_account(account_id)
        before = account.to_dict()

        updates = payload.model_dump(exclude_unset=True)
        new_name = updates.get("name", account.name)
        new_country = updates.get("country", account.country)
        if ("name" in updates or "country" in updates) and await self.accounts.name_taken(
            new_name, new_country, exclude_id=account.id
        ):
            raise ConflictError(
                "Another account already uses that name in this country.",
                details=[{"field": "name", "message": "Already in use"}],
            )

        for field, value in updates.items():
            setattr(account, field, value)

        changes = build_diff(before, account.to_dict(), fields=AUDITED_ACCOUNT_FIELDS)
        await self.audit.record(
            AuditAction.ACCOUNT_UPDATED,
            summary=f"Updated account {account.name}",
            actor=actor,
            entity_type="account",
            entity_id=account.id,
            changes=changes,
        )
        return account

    async def archive_account(self, account_id: uuid.UUID, *, actor: User) -> Account:
        account = await self.get_account(account_id)
        account.deleted_at = utcnow()

        await self.audit.record(
            AuditAction.ACCOUNT_UPDATED,
            summary=f"Archived account {account.name}",
            actor=actor,
            entity_type="account",
            entity_id=account.id,
            changes={"deleted_at": {"from": None, "to": account.deleted_at.isoformat()}},
        )
        return account

    # -------------------------------------------------------------- routes
    async def list_routes(self, account_id: uuid.UUID) -> list[AccountRelationship]:
        await self.get_account(account_id)
        return await self.routes.routes_from(account_id)

    async def add_route(
        self, account_id: uuid.UUID, payload: AccountRouteCreate, *, actor: User
    ) -> AccountRelationship:
        account = await self.get_account(account_id)

        if payload.to_account_id == account_id:
            raise ValidationError(
                "An account cannot route through itself.",
                details=[{"field": "to_account_id", "message": "Choose a different account"}],
            )

        target = await self.accounts.get(payload.to_account_id)
        if target is None:
            raise NotFoundError("account", payload.to_account_id)

        if await self.routes.exists_between(
            account_id, payload.to_account_id, payload.relation_type
        ):
            raise ConflictError("That route already exists for this account.")

        if payload.is_preferred_route:
            await self.routes.clear_preferred(account_id)

        route = AccountRelationship(from_account_id=account_id, **payload.model_dump())
        await self.routes.add(route)

        await self.audit.record(
            AuditAction.ACCOUNT_UPDATED,
            summary=(
                f"Added route: reach {account.name} via {target.name} "
                f"({payload.relation_type.value.replace('_', ' ').lower()})"
            ),
            actor=actor,
            entity_type="account",
            entity_id=account_id,
        )
        return route

    async def remove_route(
        self, account_id: uuid.UUID, route_id: uuid.UUID, *, actor: User
    ) -> None:
        route = await self.routes.get(route_id)
        if route is None or route.from_account_id != account_id:
            raise NotFoundError("route", route_id)

        await self.routes.delete(route)
        await self.audit.record(
            AuditAction.ACCOUNT_UPDATED,
            summary="Removed an account route",
            actor=actor,
            entity_type="account",
            entity_id=account_id,
        )


class ContactService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.contacts = ContactRepository(session)
        self.accounts = AccountRepository(session)
        self.audit = AuditService(session)

    async def list_contacts(self, params: PageParams, **filters: object):
        return await self.contacts.list_contacts(params, **filters)  # type: ignore[arg-type]

    async def get_contact(self, contact_id: uuid.UUID) -> Contact:
        contact = await self.contacts.get(contact_id)
        if contact is None:
            raise NotFoundError("contact", contact_id)
        return contact

    async def create_contact(self, payload: ContactCreate, *, actor: User) -> Contact:
        if await self.accounts.get(payload.account_id) is None:
            raise NotFoundError("account", payload.account_id)

        if payload.is_primary:
            await self.contacts.clear_primary(payload.account_id)

        data = payload.model_dump()
        data["email"] = str(data["email"]).lower() if data.get("email") else None
        contact = Contact(**data)
        await self.contacts.add(contact)

        await self.audit.record(
            AuditAction.ACCOUNT_UPDATED,
            summary=(
                f"Added contact {contact.full_name}"
                + (" (decision maker)" if contact.is_decision_maker else "")
            ),
            actor=actor,
            entity_type="contact",
            entity_id=contact.id,
        )
        return contact

    async def update_contact(
        self, contact_id: uuid.UUID, payload: ContactUpdate, *, actor: User
    ) -> Contact:
        contact = await self.get_contact(contact_id)
        before = contact.to_dict()
        updates = payload.model_dump(exclude_unset=True)

        if updates.get("is_primary"):
            await self.contacts.clear_primary(contact.account_id)
        if updates.get("email"):
            updates["email"] = str(updates["email"]).lower()

        for field, value in updates.items():
            setattr(contact, field, value)

        changes = build_diff(
            before,
            contact.to_dict(),
            fields={"full_name", "title", "email", "is_decision_maker", "is_primary", "is_active"},
        )
        await self.audit.record(
            AuditAction.ACCOUNT_UPDATED,
            summary=f"Updated contact {contact.full_name}",
            actor=actor,
            entity_type="contact",
            entity_id=contact.id,
            changes=changes,
        )
        return contact

    async def delete_contact(self, contact_id: uuid.UUID, *, actor: User) -> None:
        contact = await self.get_contact(contact_id)
        name = contact.full_name
        await self.contacts.delete(contact)
        await self.audit.record(
            AuditAction.ACCOUNT_UPDATED,
            summary=f"Removed contact {name}",
            actor=actor,
            entity_type="contact",
            entity_id=contact_id,
        )


class ProjectService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.projects = ProjectRepository(session)
        self.accounts = AccountRepository(session)
        self.technologies = TechnologyRepository(session)
        self.audit = AuditService(session)

    async def list_projects(self, params: PageParams, **filters: object):
        return await self.projects.list_projects(params, **filters)  # type: ignore[arg-type]

    async def get_project(self, project_id: uuid.UUID) -> Project:
        project = await self.projects.get(project_id)
        if project is None:
            raise NotFoundError("project", project_id)
        return project

    async def create_project(self, payload: ProjectCreate, *, actor: User) -> Project:
        if await self.accounts.get(payload.account_id) is None:
            raise NotFoundError("account", payload.account_id)

        if payload.prime_contractor_id:
            prime = await self.accounts.get(payload.prime_contractor_id)
            if prime is None:
                raise NotFoundError("prime contractor", payload.prime_contractor_id)
            if prime.account_type not in {AccountType.PRIME_CONTRACTOR, AccountType.PARTNER}:
                raise ValidationError(
                    "The prime contractor must be an account of type prime contractor or partner.",
                    details=[{"field": "prime_contractor_id", "message": "Wrong account type"}],
                )

        data = payload.model_dump(exclude={"technology_ids"})
        # An unowned project is a project nobody chases for extensions.
        data["owner_id"] = data.get("owner_id") or actor.id
        project = Project(**data)
        await self.projects.add(project)

        if payload.technology_ids:
            await self._assert_technologies_exist(payload.technology_ids)
            await self.projects.set_technologies(project, payload.technology_ids)

        await self.audit.record(
            AuditAction.ACCOUNT_UPDATED,
            summary=f"Created project {project.name}",
            actor=actor,
            entity_type="project",
            entity_id=project.id,
        )
        return await self.get_project(project.id)

    async def update_project(
        self, project_id: uuid.UUID, payload: ProjectUpdate, *, actor: User
    ) -> Project:
        project = await self.get_project(project_id)
        before = project.to_dict()
        updates = payload.model_dump(exclude_unset=True)
        technology_ids = updates.pop("technology_ids", None)

        start = updates.get("start_date", project.start_date)
        end = updates.get("end_date", project.end_date)
        if start and end and end < start:
            raise ValidationError(
                "The end date cannot be before the start date.",
                details=[{"field": "end_date", "message": "Must be on or after the start date"}],
            )

        for field, value in updates.items():
            setattr(project, field, value)

        if technology_ids is not None:
            await self._assert_technologies_exist(technology_ids)
            await self.projects.set_technologies(project, technology_ids)

        changes = build_diff(
            before,
            project.to_dict(),
            fields={"name", "status", "start_date", "end_date", "prime_contractor_id", "owner_id"},
        )
        await self.audit.record(
            AuditAction.ACCOUNT_UPDATED,
            summary=f"Updated project {project.name}",
            actor=actor,
            entity_type="project",
            entity_id=project.id,
            changes=changes,
        )
        return await self.get_project(project.id)

    async def archive_project(self, project_id: uuid.UUID, *, actor: User) -> Project:
        project = await self.get_project(project_id)
        project.deleted_at = utcnow()
        await self.audit.record(
            AuditAction.ACCOUNT_UPDATED,
            summary=f"Archived project {project.name}",
            actor=actor,
            entity_type="project",
            entity_id=project.id,
        )
        return project

    async def _assert_technologies_exist(self, technology_ids: list[uuid.UUID]) -> None:
        found = await self.technologies.get_many(technology_ids)
        missing = set(technology_ids) - {technology.id for technology in found}
        if missing:
            raise ValidationError(
                "One or more technologies do not exist.",
                details=[
                    {
                        "field": "technology_ids",
                        "message": f"Unknown: {sorted(str(m) for m in missing)}",
                    }
                ],
            )


class ActivityService:
    """The lightweight Contact & Activity Log. Deliberately not a CRM."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.activities = ActivityRepository(session)
        self.accounts = AccountRepository(session)
        self.contacts = ContactRepository(session)
        self.projects = ProjectRepository(session)

    async def list_activities(self, params: PageParams, **filters: object):
        return await self.activities.list_activities(params, **filters)  # type: ignore[arg-type]

    async def get_activity(self, activity_id: uuid.UUID) -> Activity:
        activity = await self.activities.get(activity_id)
        if activity is None:
            raise NotFoundError("activity", activity_id)
        return activity

    async def create_activity(self, payload: ActivityCreate, *, actor: User) -> Activity:
        await self._assert_targets_exist(payload)

        occurred_at = payload.occurred_at or utcnow()
        if occurred_at > utcnow():
            raise ValidationError(
                "An activity cannot be recorded in the future.",
                details=[
                    {
                        "field": "occurred_at",
                        "message": "Use the follow-up date for future actions",
                    }
                ],
            )

        activity = Activity(
            **payload.model_dump(exclude={"occurred_at"}),
            occurred_at=occurred_at,
            user_id=actor.id,
        )
        await self.activities.add(activity)
        return activity

    async def update_activity(
        self, activity_id: uuid.UUID, payload: ActivityUpdate, *, actor: User
    ) -> Activity:
        activity = await self.get_activity(activity_id)
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(activity, field, value)
        return activity

    async def complete_follow_up(self, activity_id: uuid.UUID, *, actor: User) -> Activity:
        activity = await self.get_activity(activity_id)
        if activity.follow_up_at is None:
            raise ValidationError("That activity has no follow-up to complete.")
        activity.completed_at = utcnow()
        return activity

    async def delete_activity(self, activity_id: uuid.UUID, *, actor: User) -> None:
        activity = await self.get_activity(activity_id)
        # Only the author may remove their own note; everything else is history.
        if activity.user_id != actor.id:
            raise ValidationError("You can only delete activities you recorded.")
        await self.activities.delete(activity)

    async def _assert_targets_exist(self, payload: ActivityCreate) -> None:
        if payload.account_id and await self.accounts.get(payload.account_id) is None:
            raise NotFoundError("account", payload.account_id)
        if payload.contact_id and await self.contacts.get(payload.contact_id) is None:
            raise NotFoundError("contact", payload.contact_id)
        if payload.project_id and await self.projects.get(payload.project_id) is None:
            raise NotFoundError("project", payload.project_id)


def is_follow_up_overdue(activity: Activity, *, now: datetime | None = None) -> bool:
    reference = now or datetime.now(UTC)
    return (
        activity.is_follow_up_open
        and activity.follow_up_at is not None
        and (activity.follow_up_at <= reference)
    )


__all__ = [
    "AUDITED_ACCOUNT_FIELDS",
    "MISSING_SIGNAL_LABELS",
    "AccountService",
    "ActivityService",
    "ContactService",
    "ProjectService",
    "is_follow_up_overdue",
]
