"""Seed the technology master, demo accounts, routes, contacts and projects.

Milaha appears because the SOW names it as the reference outsourcing model.
Every other organisation, every person, and every commercial detail is
fictional. No real personal information appears anywhere (master brief section 18).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
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
    RelationType,
    Technology,
)
from app.repositories.accounts import (
    AccountRelationshipRepository,
    AccountRepository,
    TechnologyRepository,
)
from app.repositories.identity import UserRepository

logger = get_logger("seed")

#: SOW section 3 — the Phase 1 IT resource categories.
TECHNOLOGIES: list[tuple[str, str, list[str]]] = [
    ("SAP", "ERP", ["S/4HANA", "SAP ECC", "ABAP", "SAP FICO", "SAP MM", "SAP SD"]),
    ("Oracle", "ERP", ["Oracle EBS", "Oracle Fusion", "PL/SQL", "Oracle DBA"]),
    ("Microsoft", "Enterprise Platform", ["Dynamics 365", "SharePoint", ".NET", "Power Platform"]),
    ("AI / Data", "Data & AI", ["Machine Learning", "Data Engineering", "Power BI", "Databricks"]),
    ("Cloud", "Infrastructure", ["AWS", "Azure", "GCP", "Kubernetes", "K8s", "Terraform"]),
    ("Cybersecurity", "Security", ["SOC", "SIEM", "IAM", "Penetration Testing", "ISO 27001"]),
    (
        "Java / Application Development",
        "Application Development",
        ["Java", "Spring Boot", "Angular", "React"],
    ),
    ("QA / Testing", "Quality", ["Selenium", "Test Automation", "Performance Testing"]),
    ("IT Infrastructure", "Infrastructure", ["Networking", "Windows Server", "Linux", "VMware"]),
]

#: (name, type, country, city, industry, existing_customer, existing_partner,
#:  approved_vendor, has_msa, outsourcing_friendly, status)
ACCOUNTS: list[
    tuple[str, AccountType, str, str, str, bool, bool, bool, bool, bool, RelationshipStatus]
] = [
    # The reference model named in the SOW.
    (
        "Milaha",
        AccountType.CUSTOMER,
        "QA",
        "Doha",
        "Maritime & Logistics",
        True,
        False,
        True,
        True,
        True,
        RelationshipStatus.ACTIVE,
    ),
    # Fictional customers.
    (
        "Al Dana Energy",
        AccountType.CUSTOMER,
        "QA",
        "Doha",
        "Energy",
        True,
        False,
        True,
        False,
        True,
        RelationshipStatus.ACTIVE,
    ),
    (
        "Northgate Financial Group",
        AccountType.CUSTOMER,
        "AE",
        "Dubai",
        "Banking",
        True,
        False,
        False,
        False,
        True,
        RelationshipStatus.ACTIVE,
    ),
    (
        "Qatar Civic Digital Authority",
        AccountType.PROSPECT,
        "QA",
        "Doha",
        "Government",
        False,
        False,
        False,
        False,
        True,
        RelationshipStatus.TARGET,
    ),
    (
        "Levant Retail Holdings",
        AccountType.PROSPECT,
        "SA",
        "Riyadh",
        "Retail",
        False,
        False,
        False,
        False,
        False,
        RelationshipStatus.TARGET,
    ),
    # Fictional partners and primes.
    (
        "Meridian Systems Integration",
        AccountType.PRIME_CONTRACTOR,
        "QA",
        "Doha",
        "IT Services",
        False,
        True,
        True,
        True,
        True,
        RelationshipStatus.ACTIVE,
    ),
    (
        "Cedar Technology Partners",
        AccountType.PARTNER,
        "AE",
        "Abu Dhabi",
        "IT Consulting",
        False,
        True,
        False,
        False,
        True,
        RelationshipStatus.ACTIVE,
    ),
    (
        "Gulf Talent Exchange",
        AccountType.VENDOR_MSP,
        "QA",
        "Doha",
        "Vendor Management",
        False,
        False,
        True,
        True,
        True,
        RelationshipStatus.ACTIVE,
    ),
    (
        "Harbourline Consulting",
        AccountType.PARTNER,
        "QA",
        "Doha",
        "Management Consulting",
        False,
        True,
        False,
        False,
        True,
        RelationshipStatus.DORMANT,
    ),
]

#: (from_account, to_account, relation_type, is_preferred)
ROUTES: list[tuple[str, str, RelationType, bool]] = [
    # Reaching the government authority requires a prime — the classic case the
    # Addressability engine exists to score.
    (
        "Qatar Civic Digital Authority",
        "Meridian Systems Integration",
        RelationType.SUBCONTRACTS_THROUGH,
        True,
    ),
    ("Levant Retail Holdings", "Cedar Technology Partners", RelationType.PARTNER_OF, True),
    ("Al Dana Energy", "Gulf Talent Exchange", RelationType.VENDOR_TO, False),
]

#: (account, full_name, title, is_decision_maker, is_primary)
CONTACTS: list[tuple[str, str, str, bool, bool]] = [
    ("Milaha", "Hessa Al-Marri", "Head of IT Delivery", True, True),
    ("Milaha", "Vikram Suresh", "ERP Programme Manager", False, False),
    ("Al Dana Energy", "Noora Al-Sulaiti", "CIO", True, True),
    ("Northgate Financial Group", "Peter Aldridge", "Director of Technology", True, True),
    ("Northgate Financial Group", "Lina Haddad", "Vendor Manager", False, False),
    ("Meridian Systems Integration", "Sameer Qureshi", "Delivery Director", True, True),
    ("Cedar Technology Partners", "Marc Duval", "Partnerships Lead", True, True),
    ("Gulf Talent Exchange", "Reem Al-Hajri", "Supplier Manager", False, True),
    # Deliberately left without a decision maker so the Addressability preview
    # has something real to flag as missing.
    ("Qatar Civic Digital Authority", "Procurement Desk", "General Enquiries", False, True),
]

#: (account, name, code, status, months_from_now_start, duration_months, location, technologies)
PROJECTS: list[tuple[str, str, str, ProjectStatus, int, int, str, list[str]]] = [
    (
        "Milaha",
        "S/4HANA Finance Rollout",
        "MIL-SAP-01",
        ProjectStatus.ACTIVE,
        -8,
        24,
        "Doha",
        ["SAP"],
    ),
    (
        "Milaha",
        "Fleet Analytics Platform",
        "MIL-DATA-02",
        ProjectStatus.ACTIVE,
        -3,
        12,
        "Doha",
        ["AI / Data", "Cloud"],
    ),
    (
        "Al Dana Energy",
        "SCADA Security Uplift",
        "ADE-SEC-01",
        ProjectStatus.ACTIVE,
        -5,
        18,
        "Doha",
        ["Cybersecurity", "IT Infrastructure"],
    ),
    (
        "Al Dana Energy",
        "Oracle EBS to Fusion Migration",
        "ADE-ORA-02",
        ProjectStatus.PLANNED,
        2,
        15,
        "Doha",
        ["Oracle", "Cloud"],
    ),
    (
        "Northgate Financial Group",
        "Core Banking API Layer",
        "NFG-JAVA-01",
        ProjectStatus.ACTIVE,
        -6,
        20,
        "Dubai",
        ["Java / Application Development", "Cloud"],
    ),
    (
        "Qatar Civic Digital Authority",
        "Citizen Services Portal",
        "QCDA-MS-01",
        ProjectStatus.PLANNED,
        3,
        24,
        "Doha",
        ["Microsoft", "QA / Testing"],
    ),
]

#: (account, type, subject, days_ago, follow_up_in_days)
ACTIVITIES: list[tuple[str, ActivityType, str, int, int | None]] = [
    ("Milaha", ActivityType.MEETING, "Quarterly delivery review — two SAP extensions likely", 4, 7),
    ("Milaha", ActivityType.EMAIL, "Sent updated rate card for FY26", 11, None),
    (
        "Al Dana Energy",
        ActivityType.CALL,
        "CIO flagged an upcoming cybersecurity hiring wave",
        2,
        3,
    ),
    (
        "Northgate Financial Group",
        ActivityType.NOTE,
        "Vendor registration renewal due before Q4 bids",
        9,
        14,
    ),
    (
        "Qatar Civic Digital Authority",
        ActivityType.EMAIL,
        "Introduction request via Meridian",
        6,
        2,
    ),
    (
        "Meridian Systems Integration",
        ActivityType.MEETING,
        "Agreed subcontracting terms for public-sector work",
        15,
        None,
    ),
    ("Cedar Technology Partners", ActivityType.CALL, "Discussed joint bid for Levant Retail", 1, 5),
]


async def _contact_exists(session: AsyncSession, account_id: uuid.UUID, full_name: str) -> bool:
    stmt = select(Contact.id).where(
        Contact.account_id == account_id, Contact.full_name == full_name
    )
    return (await session.execute(stmt)).first() is not None


async def _project_exists(session: AsyncSession, account_id: uuid.UUID, name: str) -> bool:
    stmt = select(Project.id).where(Project.account_id == account_id, Project.name == name)
    return (await session.execute(stmt)).first() is not None


async def _activity_exists(session: AsyncSession, account_id: uuid.UUID, subject: str) -> bool:
    stmt = select(Activity.id).where(Activity.account_id == account_id, Activity.subject == subject)
    return (await session.execute(stmt)).first() is not None


async def seed_technologies(session: AsyncSession) -> int:
    repo = TechnologyRepository(session)
    created = 0
    for name, category, aliases in TECHNOLOGIES:
        if await repo.get_by_name(name):
            continue
        await repo.add(Technology(name=name, category=category, aliases=aliases))
        created += 1
    return created


async def seed_accounts(session: AsyncSession) -> dict[str, int]:
    accounts_repo = AccountRepository(session)
    technologies_repo = TechnologyRepository(session)
    users_repo = UserRepository(session)

    sales_owner = await users_repo.get_by_email("sales@glimmora.ai")
    owner_id = sales_owner.id if sales_owner else None

    stats = {"accounts": 0, "routes": 0, "contacts": 0, "projects": 0, "activities": 0}
    by_name: dict[str, Account] = {}

    for (
        name,
        account_type,
        country,
        city,
        industry,
        existing_customer,
        existing_partner,
        approved_vendor,
        has_msa,
        outsourcing_friendly,
        relationship_status,
    ) in ACCOUNTS:
        account = await accounts_repo.get_by_name(name, country)
        if account is None:
            account = Account(
                name=name,
                account_type=account_type,
                country=country,
                city=city,
                industry=industry,
                relationship_status=relationship_status,
                is_existing_customer=existing_customer,
                is_existing_partner=existing_partner,
                is_approved_vendor=approved_vendor,
                has_msa=has_msa,
                contract_outsourcing_friendly=outsourcing_friendly,
                payment_terms_days=60,
                owner_id=owner_id,
            )
            await accounts_repo.add(account)
            stats["accounts"] += 1
        by_name[name] = account

    routes_repo = AccountRelationshipRepository(session)
    for from_name, to_name, relation_type, is_preferred in ROUTES:
        source, target = by_name.get(from_name), by_name.get(to_name)
        if not source or not target:
            continue
        if await routes_repo.exists_between(source.id, target.id, relation_type):
            continue
        session.add(
            AccountRelationship(
                from_account_id=source.id,
                to_account_id=target.id,
                relation_type=relation_type,
                is_preferred_route=is_preferred,
                notes=f"Reach {source.name} through {target.name}",
            )
        )
        stats["routes"] += 1

    contacts_by_account: dict[str, Contact] = {}
    for account_name, full_name, title, is_decision_maker, is_primary in CONTACTS:
        account = by_name.get(account_name)
        if not account:
            continue
        if await _contact_exists(session, account.id, full_name):
            continue
        local = full_name.lower().replace(" ", ".").replace("'", "")
        domain = account.name.lower().replace(" ", "").replace("-", "")[:20]
        contact = Contact(
            account_id=account.id,
            full_name=full_name,
            title=title,
            email=f"{local}@{domain}.example.com",
            phone="+974 4000 0000",
            is_decision_maker=is_decision_maker,
            is_primary=is_primary,
        )
        session.add(contact)
        contacts_by_account.setdefault(account_name, contact)
        stats["contacts"] += 1

    technology_lookup = {tech.name: tech for tech in await technologies_repo.list_all()}
    today = date.today()

    for (
        account_name,
        project_name,
        code,
        project_status,
        start_offset,
        duration,
        location,
        tech_names,
    ) in PROJECTS:
        account = by_name.get(account_name)
        if not account:
            continue
        if await _project_exists(session, account.id, project_name):
            continue
        start = today + timedelta(days=start_offset * 30)
        project = Project(
            account_id=account.id,
            name=project_name,
            code=code,
            status=project_status,
            start_date=start,
            end_date=start + timedelta(days=duration * 30),
            location=location,
            owner_id=owner_id,
            description=f"{project_name} for {account.name}.",
        )
        session.add(project)
        await session.flush()

        for tech_name in tech_names:
            technology = technology_lookup.get(tech_name)
            if technology:
                session.add(ProjectTechnology(project_id=project.id, technology_id=technology.id))
        stats["projects"] += 1

    now = datetime.now(UTC)
    for account_name, activity_type, subject, days_ago, follow_up_in in ACTIVITIES:
        account = by_name.get(account_name)
        if not account:
            continue
        if await _activity_exists(session, account.id, subject):
            continue
        session.add(
            Activity(
                activity_type=activity_type,
                subject=subject,
                occurred_at=now - timedelta(days=days_ago),
                follow_up_at=(now + timedelta(days=follow_up_in)) if follow_up_in else None,
                account_id=account.id,
                contact_id=(
                    contacts_by_account[account_name].id
                    if account_name in contacts_by_account
                    else None
                ),
                user_id=owner_id,
            )
        )
        stats["activities"] += 1

    logger.info("seed_accounts_complete", **stats)
    return stats


__all__ = ["ACCOUNTS", "PROJECTS", "TECHNOLOGIES", "seed_accounts", "seed_technologies"]
