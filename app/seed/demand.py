"""Seed the skill master and a set of demo requirements.

The skill master is seeded from the parser vocabulary so that extracted skills
land on canonical names from the first parse, rather than accumulating
near-duplicates that someone has to merge later (ASSUMPTIONS.md A22).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.vocabulary import SKILL_VOCABULARY
from app.core.logging import get_logger
from app.models.accounts import Account
from app.models.demand import (
    ContractType,
    PrioritySource,
    RateUnit,
    Requirement,
    RequirementSkill,
    RequirementSource,
    RequirementStatus,
    ReviewStatus,
    SkillImportance,
    WorkMode,
)
from app.models.skills import Skill, normalize_skill
from app.repositories.demand import SkillRepository
from app.repositories.identity import UserRepository

logger = get_logger("seed")

#: (title, role, account, priority, contract, months, positions, city, country,
#:  min_years, rate_min, rate_max, unit, deadline_hours, status, mandatory, preferred)
REQUIREMENTS: list[tuple] = [
    (
        "Senior SAP FICO Consultant",
        "SAP Consultant",
        "Milaha",
        PrioritySource.P1_EXISTING_CUSTOMER,
        ContractType.CONTRACT,
        18,
        2,
        "Doha",
        "QA",
        8,
        18000,
        22000,
        RateUnit.MONTHLY,
        None,
        RequirementStatus.QUALIFIED,
        ["SAP FICO", "SAP S/4HANA", "SAP MM"],
        ["Power BI"],
    ),
    (
        "Data Engineer - Fleet Analytics",
        "Data Engineer",
        "Milaha",
        PrioritySource.P1_EXISTING_CUSTOMER,
        ContractType.CONTRACT,
        12,
        1,
        "Doha",
        "QA",
        5,
        14000,
        17000,
        RateUnit.MONTHLY,
        None,
        RequirementStatus.QUALIFIED,
        ["Data Engineering", "Databricks", "Azure"],
        ["Power BI"],
    ),
    (
        "OT/SCADA Security Engineer",
        "Security Engineer",
        "Al Dana Energy",
        PrioritySource.P1_EXISTING_CUSTOMER,
        ContractType.CONTRACT,
        18,
        2,
        "Doha",
        "QA",
        6,
        16000,
        20000,
        RateUnit.MONTHLY,
        30,
        RequirementStatus.UNDER_REVIEW,
        ["OT / SCADA Security", "SIEM", "ISO 27001"],
        ["Penetration Testing"],
    ),
    (
        "Java Microservices Developer",
        "Java Developer",
        "Northgate Financial Group",
        PrioritySource.P4_ENTERPRISE_GOV,
        ContractType.CONTRACT,
        20,
        3,
        "Dubai",
        "AE",
        4,
        95,
        120,
        RateUnit.HOURLY,
        None,
        RequirementStatus.QUALIFIED,
        ["Java", "Spring Boot", "Microservices", "Kubernetes"],
        ["AWS"],
    ),
    (
        "Microsoft Dynamics 365 Consultant",
        "Solution Architect",
        "Qatar Civic Digital Authority",
        PrioritySource.P4_ENTERPRISE_GOV,
        ContractType.OUTSOURCED_SERVICE,
        24,
        2,
        "Doha",
        "QA",
        7,
        None,
        None,
        None,
        None,
        RequirementStatus.NEW,
        ["Microsoft Dynamics 365", "Power Platform"],
        ["QA / Testing"],
    ),
    # Two VMS requirements with live windows: one urgent, one comfortable. These
    # are what make the deadline board demonstrate something real.
    (
        "Cloud Platform Engineer (VMS)",
        "Cloud Engineer",
        "Gulf Talent Exchange",
        PrioritySource.P5_VENDOR_MSP_VMS,
        ContractType.CONTRACT,
        12,
        1,
        "Doha",
        "QA",
        5,
        15000,
        18000,
        RateUnit.MONTHLY,
        5,
        RequirementStatus.QUALIFIED,
        ["Kubernetes", "Terraform", "AWS", "CI/CD"],
        ["Linux"],
    ),
    (
        "Oracle Fusion Functional Consultant (VMS)",
        "Oracle Consultant",
        "Gulf Talent Exchange",
        PrioritySource.P5_VENDOR_MSP_VMS,
        ContractType.CONTRACT,
        15,
        1,
        "Doha",
        "QA",
        6,
        17000,
        21000,
        RateUnit.MONTHLY,
        36,
        RequirementStatus.QUALIFIED,
        ["Oracle Fusion", "Oracle EBS", "PL/SQL"],
        [],
    ),
    (
        "QA Automation Engineer",
        "QA Engineer",
        "Northgate Financial Group",
        PrioritySource.P2_PARTNER_PRIME,
        ContractType.CONTRACT_TO_HIRE,
        9,
        2,
        "Dubai",
        "AE",
        3,
        70,
        90,
        RateUnit.HOURLY,
        20,
        RequirementStatus.QUALIFIED,
        ["Test Automation", "Selenium", "CI/CD"],
        ["Performance Testing"],
    ),
]


async def seed_skills(session: AsyncSession) -> int:
    """Populate the skill master from the parser vocabulary."""
    repo = SkillRepository(session)
    created = 0

    for canonical, (category, aliases) in SKILL_VOCABULARY.items():
        if await repo.get_by_name(canonical):
            continue
        await repo.add(
            Skill(
                name=canonical,
                normalized=normalize_skill(canonical),
                category=category,
                aliases=list(aliases),
                needs_review=False,
            )
        )
        created += 1

    return created


async def _requirement_exists(session: AsyncSession, title: str) -> bool:
    stmt = select(Requirement.id).where(Requirement.title == title)
    return (await session.execute(stmt)).first() is not None


async def seed_requirements(session: AsyncSession) -> dict[str, int]:
    users = UserRepository(session)
    skills = SkillRepository(session)

    sales = await users.get_by_email("sales@glimmora.ai")
    owner_id: uuid.UUID | None = sales.id if sales else None

    accounts = {
        account.name: account
        for account in (await session.execute(select(Account))).scalars().all()
    }

    now = datetime.now(UTC)
    today = date.today()
    stats = {"requirements": 0, "skills_linked": 0}

    for (
        title,
        role,
        account_name,
        priority,
        contract,
        months,
        positions,
        city,
        country,
        min_years,
        rate_min,
        rate_max,
        rate_unit,
        deadline_hours,
        status,
        mandatory,
        preferred,
    ) in REQUIREMENTS:
        if await _requirement_exists(session, title):
            continue

        account = accounts.get(account_name)
        requirement = Requirement(
            title=title,
            role=role,
            account_id=account.id if account else None,
            priority_source=priority,
            source=(
                RequirementSource.EMAIL
                if priority is PrioritySource.P5_VENDOR_MSP_VMS
                else RequirementSource.MANUAL
            ),
            contract_type=contract,
            duration_months=months,
            positions=positions,
            location=f"{city}, {country}",
            country=country,
            work_mode=WorkMode.ONSITE,
            experience_min_years=min_years,
            rate_min=rate_min,
            rate_max=rate_max,
            rate_currency="QAR" if country == "QA" else "AED",
            rate_unit=rate_unit,
            start_by_date=today + timedelta(days=30),
            availability_requirement="Maximum 30 days notice",
            response_deadline_at=(
                now + timedelta(hours=deadline_hours) if deadline_hours else None
            ),
            status=status,
            is_active=True,
            review_status=ReviewStatus.ACCEPTED,
            owner_id=owner_id,
            description_raw=f"{title} for {account_name}. Seeded demo requirement.",
        )
        session.add(requirement)
        await session.flush()

        for names, importance in (
            (mandatory, SkillImportance.MANDATORY),
            (preferred, SkillImportance.PREFERRED),
        ):
            for name in names:
                skill = await skills.get_by_name(name)
                if skill is None:
                    continue
                session.add(
                    RequirementSkill(
                        requirement_id=requirement.id,
                        skill_id=skill.id,
                        importance=importance,
                        min_years=min_years if importance is SkillImportance.MANDATORY else None,
                    )
                )
                stats["skills_linked"] += 1

        stats["requirements"] += 1

    logger.info("seed_requirements_complete", **stats)
    return stats


__all__ = ["REQUIREMENTS", "seed_requirements", "seed_skills"]
