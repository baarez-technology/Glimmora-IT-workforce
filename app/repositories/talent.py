"""Data access for resources, documents and CV parsing."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import selectinload

from app.core.pagination import PageParams, apply_sort, paginate
from app.models.skills import Skill, normalize_skill
from app.models.talent import (
    WORK_AUTHORISATION_TYPES,
    AvailabilityStatus,
    Document,
    DocumentType,
    Resource,
    ResourceCertification,
    ResourceDocument,
    ResourceExperience,
    ResourceSkill,
    ResourceType,
    VisaStatus,
)
from app.repositories.base import BaseRepository

RESOURCE_SORT_FIELDS = {
    "full_name",
    "resource_type",
    "availability_status",
    "available_from",
    "total_experience_years",
    "created_at",
    "updated_at",
}


class ResourceRepository(BaseRepository[Resource]):
    model = Resource

    def select(self) -> Select[Any]:
        return (
            super()
            .select()
            .options(
                selectinload(Resource.skills),
                selectinload(Resource.experience),
                selectinload(Resource.certifications),
                selectinload(Resource.documents),
            )
        )

    async def list_resources(
        self,
        params: PageParams,
        *,
        resource_type: ResourceType | None = None,
        availability_status: AvailabilityStatus | None = None,
        visa_status: VisaStatus | None = None,
        country: str | None = None,
        owner_id: uuid.UUID | None = None,
        partner_account_id: uuid.UUID | None = None,
        skill_id: uuid.UUID | None = None,
        review_status: str | None = None,
        max_notice_days: int | None = None,
        available_by: date | None = None,
        bench_only: bool = False,
    ) -> tuple[list[Resource], int]:
        stmt: Select[Any] = self.select()

        if resource_type is not None:
            stmt = stmt.where(Resource.resource_type == resource_type)
        if availability_status is not None:
            stmt = stmt.where(Resource.availability_status == availability_status)
        if visa_status is not None:
            stmt = stmt.where(Resource.visa_status == visa_status)
        if country is not None:
            stmt = stmt.where(Resource.current_location_country == country.upper())
        if owner_id is not None:
            stmt = stmt.where(Resource.owner_id == owner_id)
        if partner_account_id is not None:
            stmt = stmt.where(Resource.partner_account_id == partner_account_id)
        if review_status is not None:
            stmt = stmt.where(Resource.review_status == review_status)
        if max_notice_days is not None:
            stmt = stmt.where(Resource.notice_period_days <= max_notice_days)
        if available_by is not None:
            stmt = stmt.where(
                or_(Resource.available_from.is_(None), Resource.available_from <= available_by)
            )
        if bench_only:
            stmt = stmt.where(
                or_(
                    Resource.resource_type == ResourceType.BENCH,
                    Resource.availability_status == AvailabilityStatus.AVAILABLE,
                )
            )
        if skill_id is not None:
            stmt = stmt.where(
                Resource.id.in_(
                    select(ResourceSkill.resource_id).where(ResourceSkill.skill_id == skill_id)
                )
            )
        if params.q:
            needle = f"%{params.q.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(Resource.full_name).like(needle),
                    func.lower(Resource.headline).like(needle),
                    func.lower(Resource.summary).like(needle),
                    func.lower(Resource.email).like(needle),
                    func.lower(Resource.code).like(needle),
                )
            )

        stmt = apply_sort(stmt, Resource, params, RESOURCE_SORT_FIELDS)
        if params.sort_spec() is None:
            stmt = stmt.order_by(Resource.full_name.asc())
        return await paginate(self.session, stmt, params)

    async def find_duplicates(
        self, *, email: str | None, phone: str | None, full_name: str | None
    ) -> list[tuple[Resource, str, float]]:
        """Candidates who look like the same person.

        Exact contact details are near-certain; a name match alone is a prompt
        to look, not a merge — so it is returned with lower confidence rather
        than acted on.
        """
        matches: list[tuple[Resource, str, float]] = []
        seen: set[uuid.UUID] = set()

        if email:
            stmt = self.select().where(func.lower(Resource.email) == email.strip().lower())
            for resource in (await self.session.execute(stmt)).scalars().unique().all():
                matches.append((resource, "Same email address", 0.98))
                seen.add(resource.id)

        if phone:
            digits = "".join(char for char in phone if char.isdigit())[-9:]
            if len(digits) >= 8:
                stmt = self.select().where(Resource.phone.is_not(None))
                for resource in (await self.session.execute(stmt)).scalars().unique().all():
                    if resource.id in seen or not resource.phone:
                        continue
                    other = "".join(char for char in resource.phone if char.isdigit())[-9:]
                    if other == digits:
                        matches.append((resource, "Same phone number", 0.9))
                        seen.add(resource.id)

        if full_name:
            stmt = self.select().where(func.lower(Resource.full_name) == full_name.strip().lower())
            for resource in (await self.session.execute(stmt)).scalars().unique().all():
                if resource.id not in seen:
                    matches.append((resource, "Same full name", 0.6))
                    seen.add(resource.id)

        return matches

    async def next_code(self) -> str:
        count = int(
            (await self.session.execute(select(func.count()).select_from(Resource))).scalar_one()
        )
        return f"GLM-{count + 1:05d}"

    async def set_skills(
        self, resource: Resource, entries: list[tuple[Skill, dict[str, Any]]]
    ) -> None:
        existing = await self.session.execute(
            select(ResourceSkill).where(ResourceSkill.resource_id == resource.id)
        )
        current = {row.skill_id: row for row in existing.scalars().all()}
        wanted = {skill.id: attributes for skill, attributes in entries}

        for skill_id, row in current.items():
            if skill_id not in wanted:
                await self.session.delete(row)
            else:
                for key, value in wanted[skill_id].items():
                    setattr(row, key, value)

        for skill_id, attributes in wanted.items():
            if skill_id not in current:
                self.session.add(
                    ResourceSkill(resource_id=resource.id, skill_id=skill_id, **attributes)
                )

        await self.session.flush()
        self.session.expire(resource, ["skills"])

    async def replace_experience(self, resource: Resource, entries: list[dict[str, Any]]) -> None:
        existing = await self.session.execute(
            select(ResourceExperience).where(ResourceExperience.resource_id == resource.id)
        )
        for row in existing.scalars().all():
            await self.session.delete(row)
        for entry in entries:
            self.session.add(ResourceExperience(resource_id=resource.id, **entry))
        await self.session.flush()
        self.session.expire(resource, ["experience"])

    async def replace_certifications(
        self, resource: Resource, entries: list[dict[str, Any]]
    ) -> None:
        existing = await self.session.execute(
            select(ResourceCertification).where(ResourceCertification.resource_id == resource.id)
        )
        for row in existing.scalars().all():
            await self.session.delete(row)
        for entry in entries:
            self.session.add(ResourceCertification(resource_id=resource.id, **entry))
        await self.session.flush()
        self.session.expire(resource, ["certifications"])

    async def counts_by_availability(self) -> dict[str, int]:
        rows = await self.session.execute(
            select(Resource.availability_status, func.count(Resource.id))
            .where(Resource.deleted_at.is_(None))
            .group_by(Resource.availability_status)
        )
        return {str(status): int(count) for status, count in rows}


class SkillLookup(BaseRepository[Skill]):
    model = Skill

    async def resolve(self, name: str) -> Skill | None:
        cleaned = name.strip()
        if not cleaned:
            return None
        normalized = normalize_skill(cleaned)

        stmt = select(Skill).where(Skill.normalized == normalized)
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            return existing

        aliased = await self.session.execute(select(Skill).where(Skill.aliases.is_not(None)))
        for skill in aliased.scalars().all():
            if skill.aliases and any(normalize_skill(a) == normalized for a in skill.aliases):
                return skill

        return await self.add(
            Skill(name=cleaned[:120], normalized=normalized[:120], needs_review=True)
        )


class DocumentRepository(BaseRepository[ResourceDocument]):
    model = ResourceDocument

    def select(self) -> Select[Any]:
        return super().select().options(selectinload(ResourceDocument.document))

    async def for_resource(self, resource_id: uuid.UUID) -> list[ResourceDocument]:
        stmt = (
            self.select()
            .where(ResourceDocument.resource_id == resource_id)
            .order_by(ResourceDocument.doc_type, ResourceDocument.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def expiring(
        self, *, before: date, work_authorisation_only: bool = False
    ) -> list[ResourceDocument]:
        stmt = self.select().where(
            ResourceDocument.expiry_date.is_not(None),
            ResourceDocument.expiry_date <= before,
        )
        if work_authorisation_only:
            stmt = stmt.where(ResourceDocument.doc_type.in_(tuple(WORK_AUTHORISATION_TYPES)))
        stmt = stmt.order_by(ResourceDocument.expiry_date.asc())
        return list((await self.session.execute(stmt)).scalars().unique().all())

    async def latest_cv(self, resource_id: uuid.UUID) -> ResourceDocument | None:
        stmt = (
            self.select()
            .where(
                ResourceDocument.resource_id == resource_id,
                ResourceDocument.doc_type == DocumentType.CV,
            )
            .order_by(ResourceDocument.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def add_file(self, document: Document) -> Document:
        self.session.add(document)
        await self.session.flush()
        return document

    async def get_file(self, document_id: uuid.UUID) -> Document | None:
        stmt = select(Document).where(Document.id == document_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()


__all__ = [
    "RESOURCE_SORT_FIELDS",
    "DocumentRepository",
    "ResourceRepository",
    "SkillLookup",
]
