"""Loading requirements and resources into the shapes the engine scores.

Extracted so forward and reverse matching build their views the same way. Two
directions that loaded data differently would eventually score the same pair
differently, which is the one thing an explainable engine cannot afford.

Every function here is **batched**. Reverse matching scores one resource against
every open requirement, so a per-row skills query would turn one screen into a
few hundred round trips (MATCHING.md section 6).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai.vocabulary import SKILL_TO_TECHNOLOGY, SKILL_VOCABULARY
from app.engines.matching.engine import RequirementView, ResourceView
from app.engines.matching.reverse import AccountView
from app.models.accounts import Account, AccountRelationship
from app.models.demand import Requirement, RequirementSkill, SkillImportance
from app.models.talent import Resource, ResourceSkill
from app.services.documents import work_authorisation_state
from app.services.resources import ResourceService


def _technology_for(skill_name: str) -> str | None:
    family = SKILL_VOCABULARY.get(skill_name, (None, []))[0]
    return SKILL_TO_TECHNOLOGY.get(family) if family else None


async def build_requirement_views(
    session: AsyncSession, requirements: list[Requirement]
) -> dict[uuid.UUID, RequirementView]:
    """One query for every requirement's skills, however many requirements."""
    if not requirements:
        return {}

    ids = [requirement.id for requirement in requirements]
    rows = await session.execute(
        select(RequirementSkill)
        .where(RequirementSkill.requirement_id.in_(ids))
        .options(selectinload(RequirementSkill.skill))
    )

    by_requirement: dict[uuid.UUID, list[RequirementSkill]] = {}
    for link in rows.scalars().all():
        by_requirement.setdefault(link.requirement_id, []).append(link)

    views: dict[uuid.UUID, RequirementView] = {}
    for requirement in requirements:
        mandatory: list[str] = []
        preferred: list[str] = []
        required_years: dict[str, int | None] = {}
        technologies: set[str] = set()

        for link in by_requirement.get(requirement.id, []):
            name = link.skill.name
            required_years[name] = link.min_years
            if link.importance is SkillImportance.MANDATORY:
                mandatory.append(name)
            else:
                preferred.append(name)
            technology = _technology_for(name)
            if technology:
                technologies.add(technology)

        views[requirement.id] = RequirementView(
            id=requirement.id,
            title=requirement.title,
            mandatory_skills=mandatory,
            preferred_skills=preferred,
            required_years=required_years,
            technologies=technologies,
            experience_min_years=requirement.experience_min_years,
            country=requirement.country,
            location=requirement.location,
            work_mode=requirement.work_mode.value if requirement.work_mode else None,
            start_by_date=requirement.start_by_date,
            rate_max=requirement.rate_max or requirement.rate_min,
            rate_unit=requirement.rate_unit.value if requirement.rate_unit else None,
            positions=requirement.positions,
        )
    return views


async def build_resource_views(
    session: AsyncSession, resources: list[Resource], *, today: date
) -> list[ResourceView]:
    """One query for every candidate's skills; documents come from eager load."""
    if not resources:
        return []

    ids = [resource.id for resource in resources]
    rows = await session.execute(
        select(ResourceSkill)
        .where(ResourceSkill.resource_id.in_(ids))
        .options(selectinload(ResourceSkill.skill))
    )
    by_resource: dict[uuid.UUID, list[ResourceSkill]] = {}
    for link in rows.scalars().all():
        by_resource.setdefault(link.resource_id, []).append(link)

    views: list[ResourceView] = []
    for resource in resources:
        links = by_resource.get(resource.id, [])

        technologies: set[str] = set()
        primary: set[str] = set()
        for link in links:
            technology = _technology_for(link.skill.name)
            if technology:
                technologies.add(technology)
                if link.is_primary:
                    primary.add(technology)

        authorisation = work_authorisation_state(list(resource.documents), today=today)

        views.append(
            ResourceView(
                id=resource.id,
                full_name=resource.full_name,
                skills={link.skill.name: link.years for link in links},
                skill_last_used={link.skill.name: link.last_used_year for link in links},
                primary_technologies=primary,
                technologies=technologies,
                total_experience_years=resource.total_experience_years,
                country=resource.current_location_country,
                city=resource.current_location_city,
                willing_to_relocate=resource.willing_to_relocate,
                ready_from=ResourceService.ready_from(resource, today=today),
                notice_period_days=resource.notice_period_days,
                available_from=resource.available_from,
                expected_cost=resource.expected_cost_amount,
                expected_cost_unit=resource.expected_cost_unit,
                work_authorisation_state=authorisation.state.value,
                work_authorisation_days=authorisation.days_remaining,
                needs_review=resource.is_awaiting_review,
                availability_status=resource.availability_status.value,
            )
        )
    return views


async def build_account_views(
    session: AsyncSession, account_ids: set[uuid.UUID]
) -> dict[uuid.UUID, AccountView]:
    if not account_ids:
        return {}

    rows = await session.execute(select(Account).where(Account.id.in_(account_ids)))
    return {
        account.id: AccountView(
            id=account.id,
            name=account.name,
            account_type=account.account_type.value,
            relationship_status=account.relationship_status.value,
            is_existing_customer=account.is_existing_customer,
            is_existing_partner=account.is_existing_partner,
            is_approved_vendor=account.is_approved_vendor,
            has_msa=account.has_msa,
            contract_outsourcing_friendly=account.contract_outsourcing_friendly,
        )
        for account in rows.scalars().all()
    }


async def build_preferred_routes(
    session: AsyncSession, account_ids: set[uuid.UUID]
) -> dict[uuid.UUID, tuple[uuid.UUID, bool]]:
    """Best recorded intermediary per account: (via_account_id, is_preferred).

    Where several routes exist, a preferred one wins; otherwise the first
    recorded route is used. Choosing between two equally-unmarked routes is a
    judgement for Sales, not something the engine should invent a rule for.
    """
    if not account_ids:
        return {}

    rows = await session.execute(
        select(AccountRelationship).where(AccountRelationship.to_account_id.in_(account_ids))
    )

    routes: dict[uuid.UUID, tuple[uuid.UUID, bool]] = {}
    for link in rows.scalars().all():
        existing = routes.get(link.to_account_id)
        if existing is None or (link.is_preferred_route and not existing[1]):
            routes[link.to_account_id] = (link.from_account_id, link.is_preferred_route)
    return routes


__all__ = [
    "build_account_views",
    "build_preferred_routes",
    "build_requirement_views",
    "build_resource_views",
]
