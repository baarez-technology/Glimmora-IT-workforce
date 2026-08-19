"""Data access for requirements, skills and status history."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import selectinload

from app.core.pagination import PageParams, apply_sort, paginate
from app.models.demand import (
    TERMINAL_STATUSES,
    ContractType,
    PrioritySource,
    Requirement,
    RequirementSkill,
    RequirementStatus,
    RequirementStatusHistory,
    ReviewStatus,
    SkillImportance,
)
from app.models.skills import Skill, normalize_skill
from app.repositories.base import BaseRepository

REQUIREMENT_SORT_FIELDS = {
    "title",
    "status",
    "priority_source",
    "response_deadline_at",
    "start_by_date",
    "created_at",
    "updated_at",
}


class SkillRepository(BaseRepository[Skill]):
    model = Skill

    async def get_by_name(self, name: str) -> Skill | None:
        stmt = select(Skill).where(Skill.normalized == normalize_skill(name))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def search(self, query: str | None, limit: int = 50) -> list[Skill]:
        stmt = select(Skill).where(Skill.is_active.is_(True))
        if query:
            needle = f"%{normalize_skill(query)}%"
            stmt = stmt.where(Skill.normalized.like(needle))
        stmt = stmt.order_by(Skill.name).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def resolve(self, name: str, *, create_missing: bool = True) -> Skill | None:
        """Map a free-text skill onto the master, creating it if it is new.

        A newly-created skill is flagged `needs_review` so an admin can merge it
        into an existing entry rather than letting near-duplicates accumulate
        (ASSUMPTIONS.md A22).
        """
        cleaned = name.strip()
        if not cleaned:
            return None

        existing = await self.get_by_name(cleaned)
        if existing is not None:
            return existing

        # An alias of a known skill resolves to that skill, not to a new row.
        normalized = normalize_skill(cleaned)
        alias_match = await self.session.execute(select(Skill).where(Skill.aliases.is_not(None)))
        for skill in alias_match.scalars().all():
            if skill.aliases and any(normalize_skill(a) == normalized for a in skill.aliases):
                return skill

        if not create_missing:
            return None

        skill = Skill(
            name=cleaned[:120],
            normalized=normalized[:120],
            needs_review=True,
        )
        return await self.add(skill)


class RequirementRepository(BaseRepository[Requirement]):
    model = Requirement

    def select(self) -> Select[Any]:
        return super().select().options(selectinload(Requirement.skills))

    async def list_requirements(
        self,
        params: PageParams,
        *,
        status: RequirementStatus | None = None,
        priority_source: PrioritySource | None = None,
        contract_type: ContractType | None = None,
        account_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        owner_id: uuid.UUID | None = None,
        review_status: ReviewStatus | None = None,
        country: str | None = None,
        open_only: bool = False,
        has_deadline: bool | None = None,
        deadline_before: datetime | None = None,
        skill_id: uuid.UUID | None = None,
    ) -> tuple[list[Requirement], int]:
        stmt: Select[Any] = self.select()

        if status is not None:
            stmt = stmt.where(Requirement.status == status)
        if priority_source is not None:
            stmt = stmt.where(Requirement.priority_source == priority_source)
        if contract_type is not None:
            stmt = stmt.where(Requirement.contract_type == contract_type)
        if account_id is not None:
            stmt = stmt.where(
                or_(
                    Requirement.account_id == account_id,
                    Requirement.end_customer_id == account_id,
                    Requirement.route_account_id == account_id,
                )
            )
        if project_id is not None:
            stmt = stmt.where(Requirement.project_id == project_id)
        if owner_id is not None:
            stmt = stmt.where(Requirement.owner_id == owner_id)
        if review_status is not None:
            stmt = stmt.where(Requirement.review_status == review_status)
        if country is not None:
            stmt = stmt.where(Requirement.country == country.upper())
        if open_only:
            stmt = stmt.where(
                Requirement.is_active.is_(True),
                Requirement.status.not_in(tuple(TERMINAL_STATUSES)),
            )
        if has_deadline is True:
            stmt = stmt.where(Requirement.response_deadline_at.is_not(None))
        elif has_deadline is False:
            stmt = stmt.where(Requirement.response_deadline_at.is_(None))
        if deadline_before is not None:
            stmt = stmt.where(
                Requirement.response_deadline_at.is_not(None),
                Requirement.response_deadline_at <= deadline_before,
            )
        if skill_id is not None:
            stmt = stmt.where(
                Requirement.id.in_(
                    select(RequirementSkill.requirement_id).where(
                        RequirementSkill.skill_id == skill_id
                    )
                )
            )
        if params.q:
            needle = f"%{params.q.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Requirement.title).like(needle),
                    func.lower(Requirement.role).like(needle),
                    func.lower(Requirement.description_raw).like(needle),
                    func.lower(Requirement.external_reference).like(needle),
                )
            )

        stmt = apply_sort(stmt, Requirement, params, REQUIREMENT_SORT_FIELDS)
        if params.sort_spec() is None:
            # Newest first is the useful default for a demand queue.
            stmt = stmt.order_by(Requirement.created_at.desc())
        return await paginate(self.session, stmt, params)

    async def open_with_deadlines(self, limit: int = 500) -> list[Requirement]:
        """Every open requirement that carries a submission deadline."""
        stmt = (
            self.select()
            .where(
                Requirement.is_active.is_(True),
                Requirement.status.not_in(tuple(TERMINAL_STATUSES)),
                Requirement.response_deadline_at.is_not(None),
            )
            .order_by(Requirement.response_deadline_at.asc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def set_skills(
        self,
        requirement: Requirement,
        entries: list[tuple[Skill, SkillImportance, int | None]],
    ) -> None:
        existing = await self.session.execute(
            select(RequirementSkill).where(RequirementSkill.requirement_id == requirement.id)
        )
        current = {row.skill_id: row for row in existing.scalars().all()}
        wanted = {skill.id: (importance, years) for skill, importance, years in entries}

        for skill_id, row in current.items():
            if skill_id not in wanted:
                await self.session.delete(row)
            else:
                row.importance, row.min_years = wanted[skill_id]

        for skill_id, (importance, years) in wanted.items():
            if skill_id not in current:
                self.session.add(
                    RequirementSkill(
                        requirement_id=requirement.id,
                        skill_id=skill_id,
                        importance=importance,
                        min_years=years,
                    )
                )

        await self.session.flush()
        # The identity map still holds the collection loaded before this change.
        self.session.expire(requirement, ["skills"])

    async def history(self, requirement_id: uuid.UUID) -> list[RequirementStatusHistory]:
        stmt = (
            select(RequirementStatusHistory)
            .where(RequirementStatusHistory.requirement_id == requirement_id)
            .order_by(RequirementStatusHistory.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def counts_by_status(self) -> dict[str, int]:
        rows = await self.session.execute(
            select(Requirement.status, func.count(Requirement.id))
            .where(Requirement.deleted_at.is_(None))
            .group_by(Requirement.status)
        )
        return {str(status): int(count) for status, count in rows}


__all__ = ["REQUIREMENT_SORT_FIELDS", "RequirementRepository", "SkillRepository"]
