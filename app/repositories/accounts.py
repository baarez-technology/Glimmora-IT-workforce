"""Data access for accounts, routing, contacts, projects and activities."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Integer, Select, and_, cast, func, or_, select
from sqlalchemy.orm import selectinload

from app.core.pagination import PageParams, apply_sort, paginate
from app.models.accounts import (
    Account,
    AccountRelationship,
    AccountType,
    Activity,
    ActivityType,
    Contact,
    Project,
    ProjectStatus,
    ProjectTechnology,
    RelationshipStatus,
    Technology,
)
from app.repositories.base import BaseRepository

ACCOUNT_SORT_FIELDS = {"name", "account_type", "relationship_status", "country", "created_at"}
CONTACT_SORT_FIELDS = {"full_name", "title", "created_at"}
PROJECT_SORT_FIELDS = {"name", "status", "start_date", "end_date", "created_at"}
ACTIVITY_SORT_FIELDS = {"occurred_at", "follow_up_at", "created_at"}


class AccountRepository(BaseRepository[Account]):
    model = Account

    async def list_accounts(
        self,
        params: PageParams,
        *,
        account_type: AccountType | None = None,
        relationship_status: RelationshipStatus | None = None,
        owner_id: uuid.UUID | None = None,
        country: str | None = None,
        is_existing_customer: bool | None = None,
        is_approved_vendor: bool | None = None,
    ) -> tuple[list[Account], int]:
        stmt: Select[Any] = self.select()

        if account_type is not None:
            stmt = stmt.where(Account.account_type == account_type)
        if relationship_status is not None:
            stmt = stmt.where(Account.relationship_status == relationship_status)
        if owner_id is not None:
            stmt = stmt.where(Account.owner_id == owner_id)
        if country is not None:
            stmt = stmt.where(Account.country == country.upper())
        if is_existing_customer is not None:
            stmt = stmt.where(Account.is_existing_customer.is_(is_existing_customer))
        if is_approved_vendor is not None:
            stmt = stmt.where(Account.is_approved_vendor.is_(is_approved_vendor))
        if params.q:
            needle = f"%{params.q.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Account.name).like(needle),
                    func.lower(Account.legal_name).like(needle),
                    func.lower(Account.industry).like(needle),
                )
            )

        stmt = apply_sort(stmt, Account, params, ACCOUNT_SORT_FIELDS)
        if params.sort_spec() is None:
            stmt = stmt.order_by(Account.name.asc())
        return await paginate(self.session, stmt, params)

    async def get_by_name(self, name: str, country: str | None) -> Account | None:
        stmt = self.select().where(func.lower(Account.name) == name.strip().lower())
        if country:
            stmt = stmt.where(Account.country == country.upper())
        return (await self.session.execute(stmt)).scalars().first()

    async def name_taken(
        self, name: str, country: str | None, *, exclude_id: uuid.UUID | None = None
    ) -> bool:
        stmt = self.select().where(func.lower(Account.name) == name.strip().lower())
        stmt = stmt.where(Account.country == (country.upper() if country else None))
        if exclude_id is not None:
            stmt = stmt.where(Account.id != exclude_id)
        return (await self.session.execute(stmt)).scalars().first() is not None

    async def counts_for(self, account_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
        """Related-entity counts for a page of accounts, in three queries, not N+1."""
        if not account_ids:
            return {}

        counts: dict[uuid.UUID, dict[str, int]] = {
            account_id: {"contacts": 0, "decision_makers": 0, "projects": 0, "routes": 0}
            for account_id in account_ids
        }

        contact_rows = await self.session.execute(
            select(
                Contact.account_id,
                func.count(Contact.id),
                func.sum(cast(Contact.is_decision_maker, Integer)),
            )
            .where(Contact.account_id.in_(account_ids), Contact.is_active.is_(True))
            .group_by(Contact.account_id)
        )
        for account_id, total, decision_makers in contact_rows:
            counts[account_id]["contacts"] = int(total or 0)
            counts[account_id]["decision_makers"] = int(decision_makers or 0)

        project_rows = await self.session.execute(
            select(Project.account_id, func.count(Project.id))
            .where(Project.account_id.in_(account_ids), Project.deleted_at.is_(None))
            .group_by(Project.account_id)
        )
        for account_id, total in project_rows:
            counts[account_id]["projects"] = int(total or 0)

        route_rows = await self.session.execute(
            select(AccountRelationship.from_account_id, func.count(AccountRelationship.id))
            .where(AccountRelationship.from_account_id.in_(account_ids))
            .group_by(AccountRelationship.from_account_id)
        )
        for account_id, total in route_rows:
            counts[account_id]["routes"] = int(total or 0)

        return counts

    async def has_decision_maker(self, account_id: uuid.UUID) -> bool:
        stmt = select(Contact.id).where(
            Contact.account_id == account_id,
            Contact.is_decision_maker.is_(True),
            Contact.is_active.is_(True),
        )
        return (await self.session.execute(stmt)).first() is not None


class AccountRelationshipRepository(BaseRepository[AccountRelationship]):
    model = AccountRelationship

    async def routes_from(self, account_id: uuid.UUID) -> list[AccountRelationship]:
        stmt = (
            select(AccountRelationship)
            .where(AccountRelationship.from_account_id == account_id)
            .options(selectinload(AccountRelationship.to_account))
            .order_by(AccountRelationship.is_preferred_route.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def exists_between(
        self, from_id: uuid.UUID, to_id: uuid.UUID, relation_type: Any
    ) -> bool:
        stmt = select(AccountRelationship.id).where(
            AccountRelationship.from_account_id == from_id,
            AccountRelationship.to_account_id == to_id,
            AccountRelationship.relation_type == relation_type,
        )
        return (await self.session.execute(stmt)).first() is not None

    async def clear_preferred(self, account_id: uuid.UUID) -> None:
        """Only one route can be the preferred one at a time."""
        stmt = select(AccountRelationship).where(
            AccountRelationship.from_account_id == account_id,
            AccountRelationship.is_preferred_route.is_(True),
        )
        for route in (await self.session.execute(stmt)).scalars().all():
            route.is_preferred_route = False


class ContactRepository(BaseRepository[Contact]):
    model = Contact

    async def list_contacts(
        self,
        params: PageParams,
        *,
        account_id: uuid.UUID | None = None,
        is_decision_maker: bool | None = None,
        is_active: bool | None = None,
    ) -> tuple[list[Contact], int]:
        stmt: Select[Any] = select(Contact)
        if account_id is not None:
            stmt = stmt.where(Contact.account_id == account_id)
        if is_decision_maker is not None:
            stmt = stmt.where(Contact.is_decision_maker.is_(is_decision_maker))
        if is_active is not None:
            stmt = stmt.where(Contact.is_active.is_(is_active))
        if params.q:
            needle = f"%{params.q.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Contact.full_name).like(needle),
                    func.lower(Contact.email).like(needle),
                    func.lower(Contact.title).like(needle),
                )
            )
        stmt = apply_sort(stmt, Contact, params, CONTACT_SORT_FIELDS)
        if params.sort_spec() is None:
            stmt = stmt.order_by(Contact.is_decision_maker.desc(), Contact.full_name.asc())
        return await paginate(self.session, stmt, params)

    async def clear_primary(self, account_id: uuid.UUID) -> None:
        stmt = select(Contact).where(Contact.account_id == account_id, Contact.is_primary.is_(True))
        for contact in (await self.session.execute(stmt)).scalars().all():
            contact.is_primary = False


class ProjectRepository(BaseRepository[Project]):
    model = Project

    def select(self) -> Select[Any]:
        return super().select().options(selectinload(Project.technologies))

    async def list_projects(
        self,
        params: PageParams,
        *,
        account_id: uuid.UUID | None = None,
        status: ProjectStatus | None = None,
        technology_id: uuid.UUID | None = None,
        ending_before: datetime | None = None,
    ) -> tuple[list[Project], int]:
        stmt: Select[Any] = self.select()
        if account_id is not None:
            stmt = stmt.where(Project.account_id == account_id)
        if status is not None:
            stmt = stmt.where(Project.status == status)
        if technology_id is not None:
            stmt = stmt.where(
                Project.id.in_(
                    select(ProjectTechnology.project_id).where(
                        ProjectTechnology.technology_id == technology_id
                    )
                )
            )
        if ending_before is not None:
            stmt = stmt.where(Project.end_date.is_not(None), Project.end_date <= ending_before)
        if params.q:
            needle = f"%{params.q.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Project.name).like(needle),
                    func.lower(Project.code).like(needle),
                    func.lower(Project.description).like(needle),
                )
            )
        stmt = apply_sort(stmt, Project, params, PROJECT_SORT_FIELDS)
        if params.sort_spec() is None:
            stmt = stmt.order_by(Project.name.asc())
        return await paginate(self.session, stmt, params)

    async def set_technologies(self, project: Project, technology_ids: list[uuid.UUID]) -> None:
        existing = await self.session.execute(
            select(ProjectTechnology).where(ProjectTechnology.project_id == project.id)
        )
        current = {row.technology_id: row for row in existing.scalars().all()}
        wanted = set(technology_ids)

        for technology_id, row in current.items():
            if technology_id not in wanted:
                await self.session.delete(row)

        for technology_id in wanted - set(current):
            self.session.add(ProjectTechnology(project_id=project.id, technology_id=technology_id))
        await self.session.flush()

        # The identity map still holds the collection loaded before this change,
        # so a re-read would return the old technologies. Expire it explicitly.
        self.session.expire(project, ["technologies"])


class TechnologyRepository(BaseRepository[Technology]):
    model = Technology

    async def list_all(self) -> list[Technology]:
        stmt = select(Technology).where(Technology.is_active.is_(True)).order_by(Technology.name)
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_by_name(self, name: str) -> Technology | None:
        stmt = select(Technology).where(func.lower(Technology.name) == name.strip().lower())
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_many(self, ids: list[uuid.UUID]) -> list[Technology]:
        if not ids:
            return []
        stmt = select(Technology).where(Technology.id.in_(ids))
        return list((await self.session.execute(stmt)).scalars().all())


class ActivityRepository(BaseRepository[Activity]):
    model = Activity

    async def list_activities(
        self,
        params: PageParams,
        *,
        account_id: uuid.UUID | None = None,
        contact_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        activity_type: ActivityType | None = None,
        user_id: uuid.UUID | None = None,
        open_follow_ups_only: bool = False,
        overdue_only: bool = False,
        now: datetime | None = None,
    ) -> tuple[list[Activity], int]:
        stmt: Select[Any] = select(Activity)

        if account_id is not None:
            stmt = stmt.where(Activity.account_id == account_id)
        if contact_id is not None:
            stmt = stmt.where(Activity.contact_id == contact_id)
        if project_id is not None:
            stmt = stmt.where(Activity.project_id == project_id)
        if activity_type is not None:
            stmt = stmt.where(Activity.activity_type == activity_type)
        if user_id is not None:
            stmt = stmt.where(Activity.user_id == user_id)
        if open_follow_ups_only or overdue_only:
            stmt = stmt.where(
                and_(Activity.follow_up_at.is_not(None), Activity.completed_at.is_(None))
            )
        if overdue_only and now is not None:
            stmt = stmt.where(Activity.follow_up_at <= now)
        if params.q:
            needle = f"%{params.q.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Activity.subject).like(needle),
                    func.lower(Activity.body).like(needle),
                )
            )

        stmt = apply_sort(stmt, Activity, params, ACTIVITY_SORT_FIELDS)
        if params.sort_spec() is None:
            # A timeline reads newest-first; a follow-up queue reads soonest-first.
            stmt = stmt.order_by(
                Activity.follow_up_at.asc()
                if (open_follow_ups_only or overdue_only)
                else Activity.occurred_at.desc()
            )
        return await paginate(self.session, stmt, params)


__all__ = [
    "ACCOUNT_SORT_FIELDS",
    "ACTIVITY_SORT_FIELDS",
    "CONTACT_SORT_FIELDS",
    "PROJECT_SORT_FIELDS",
    "AccountRelationshipRepository",
    "AccountRepository",
    "ActivityRepository",
    "ContactRepository",
    "ProjectRepository",
    "TechnologyRepository",
]
