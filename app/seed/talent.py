"""Seed the talent cloud with demo consultants and their documents.

All people, contact details and documents are fictional. One consultant is
seeded with an expiring QID and another with an expired work permit, so the
document-expiry screens demonstrate the case they exist for rather than an
empty board.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.talent import (
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
from app.repositories.demand import SkillRepository
from app.repositories.identity import UserRepository

logger = get_logger("seed")

#: (name, headline, type, availability, days_until_free, notice, years, city,
#:  country, cost, billing, skills, certifications)
RESOURCES: list[tuple] = [
    (
        "Rahul Menon",
        "Senior SAP FICO Consultant",
        ResourceType.CONSULTANT,
        AvailabilityStatus.AVAILABLE_SOON,
        30,
        30,
        11.0,
        "Doha",
        "QA",
        14000,
        20000,
        [("SAP FICO", 9), ("SAP S/4HANA", 6), ("SAP MM", 5)],
        ["PMP"],
    ),
    (
        "Priya Nair",
        "Data Engineer",
        ResourceType.BENCH,
        AvailabilityStatus.AVAILABLE,
        0,
        0,
        6.0,
        "Doha",
        "QA",
        11000,
        16000,
        [("Data Engineering", 6), ("Databricks", 4), ("Azure", 5), ("Power BI", 3)],
        [],
    ),
    (
        "Omar Haddad",
        "OT Security Engineer",
        ResourceType.EMPLOYEE,
        AvailabilityStatus.DEPLOYED,
        90,
        60,
        8.0,
        "Doha",
        "QA",
        15000,
        21000,
        [("OT / SCADA Security", 7), ("SIEM", 6), ("ISO 27001", 4)],
        ["CISSP"],
    ),
    (
        "Elena Vasquez",
        "Java Microservices Developer",
        ResourceType.CONSULTANT,
        AvailabilityStatus.AVAILABLE,
        0,
        15,
        7.0,
        "Dubai",
        "AE",
        90,
        130,
        [("Java", 7), ("Spring Boot", 6), ("Microservices", 5), ("Kubernetes", 4)],
        [],
    ),
    (
        "Kwame Boateng",
        "Cloud Platform Engineer",
        ResourceType.BENCH,
        AvailabilityStatus.AVAILABLE,
        0,
        0,
        5.0,
        "Doha",
        "QA",
        12500,
        18000,
        [("Kubernetes", 5), ("Terraform", 4), ("AWS", 5), ("CI/CD", 5)],
        [],
    ),
    (
        "Meera Krishnan",
        "QA Automation Engineer",
        ResourceType.PRE_VETTED_CANDIDATE,
        AvailabilityStatus.AVAILABLE_SOON,
        45,
        45,
        4.0,
        "Bengaluru",
        "IN",
        40,
        75,
        [("Test Automation", 4), ("Selenium", 4), ("CI/CD", 3)],
        [],
    ),
    (
        "Yusuf Al-Rashid",
        "Oracle Fusion Consultant",
        ResourceType.PARTNER_RESOURCE,
        AvailabilityStatus.AVAILABLE_SOON,
        20,
        30,
        9.0,
        "Doha",
        "QA",
        15500,
        22000,
        [("Oracle Fusion", 6), ("Oracle EBS", 8), ("PL/SQL", 9)],
        [],
    ),
    (
        "Sofia Almeida",
        "Full Stack Developer",
        ResourceType.FREELANCER,
        AvailabilityStatus.AVAILABLE,
        0,
        7,
        6.0,
        "Dubai",
        "AE",
        75,
        115,
        [("TypeScript", 6), ("React", 6), ("Node.js", 5), ("REST APIs", 6)],
        [],
    ),
]

#: (resource, doc_type, days_from_today) — negative means already expired.
DOCUMENTS: list[tuple[str, DocumentType, int]] = [
    ("Rahul Menon", DocumentType.PASSPORT, 1200),
    ("Rahul Menon", DocumentType.QID, 25),  # expiring soon — the alert case
    ("Priya Nair", DocumentType.QID, 400),
    ("Omar Haddad", DocumentType.WORK_PERMIT, -12),  # expired — blocks deployment
    ("Omar Haddad", DocumentType.PASSPORT, 900),
    ("Kwame Boateng", DocumentType.QID, 55),  # expiring soon
    ("Yusuf Al-Rashid", DocumentType.VISA, 700),
]

_PLACEHOLDER = b"%PDF-1.4\n% Glimmora seed placeholder document\n"


async def _resource_exists(session: AsyncSession, full_name: str) -> bool:
    stmt = select(Resource.id).where(Resource.full_name == full_name)
    return (await session.execute(stmt)).first() is not None


async def seed_resources(session: AsyncSession) -> dict[str, int]:
    from app.storage.service import store_upload

    users = UserRepository(session)
    skills_repo = SkillRepository(session)

    resourcing = await users.get_by_email("resourcing@glimmora.ai")
    owner_id: uuid.UUID | None = resourcing.id if resourcing else None

    today = date.today()
    stats = {"resources": 0, "skills": 0, "documents": 0}
    by_name: dict[str, Resource] = {}

    existing_count = len(list((await session.execute(select(Resource.id))).scalars().all()))

    for index, (
        full_name,
        headline,
        resource_type,
        availability,
        free_in,
        notice,
        years,
        city,
        country,
        cost,
        billing,
        skill_entries,
        certifications,
    ) in enumerate(RESOURCES):
        if await _resource_exists(session, full_name):
            stmt = select(Resource).where(Resource.full_name == full_name)
            by_name[full_name] = (await session.execute(stmt)).scalar_one()
            continue

        local = full_name.lower().replace(" ", ".").replace("'", "")
        resource = Resource(
            code=f"GLM-{existing_count + index + 1:05d}",
            full_name=full_name,
            email=f"{local}@example.com",
            phone="+974 5555 0000",
            resource_type=resource_type,
            headline=headline,
            summary=f"{headline} with {years:.0f} years of enterprise IT delivery experience.",
            total_experience_years=years,
            relevant_experience_years=years,
            current_location_city=city,
            current_location_country=country,
            willing_to_relocate=country != "QA",
            availability_status=availability,
            available_from=today + timedelta(days=free_in) if free_in else today,
            notice_period_days=notice,
            expected_cost_amount=cost,
            expected_cost_currency="QAR"
            if country == "QA"
            else "AED"
            if country == "AE"
            else "USD",
            expected_cost_unit="MONTHLY" if cost > 500 else "HOURLY",
            target_billing_amount=billing,
            target_billing_currency="QAR"
            if country == "QA"
            else "AED"
            if country == "AE"
            else "USD",
            target_billing_unit="MONTHLY" if billing > 500 else "HOURLY",
            visa_status=VisaStatus.UNKNOWN,
            owner_id=owner_id,
            source="MANUAL",
            review_status="ACCEPTED",
        )
        session.add(resource)
        await session.flush()
        by_name[full_name] = resource
        stats["resources"] += 1

        for skill_name, skill_years in skill_entries:
            skill = await skills_repo.get_by_name(skill_name)
            if skill is None:
                continue
            session.add(
                ResourceSkill(
                    resource_id=resource.id,
                    skill_id=skill.id,
                    years=float(skill_years),
                    is_primary=skill_name == skill_entries[0][0],
                )
            )
            stats["skills"] += 1

        for certification in certifications:
            session.add(ResourceCertification(resource_id=resource.id, name=certification))

        session.add(
            ResourceExperience(
                resource_id=resource.id,
                company="Previous employer",
                role=headline,
                start_date=today - timedelta(days=int(years * 365)),
                is_current=availability is AvailabilityStatus.DEPLOYED,
            )
        )

    for resource_name, doc_type, offset in DOCUMENTS:
        target = by_name.get(resource_name)
        if target is None:
            continue

        existing = await session.execute(
            select(ResourceDocument.id).where(
                ResourceDocument.resource_id == target.id,
                ResourceDocument.doc_type == doc_type,
            )
        )
        if existing.first() is not None:
            continue

        stored = store_upload(f"{doc_type.value.lower()}.pdf", _PLACEHOLDER)
        document = Document(
            storage_key=stored.storage_key,
            storage_backend=stored.backend,
            original_filename=stored.original_filename,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            checksum_sha256=stored.checksum_sha256 or hashlib.sha256(_PLACEHOLDER).hexdigest(),
            uploaded_by=owner_id,
        )
        session.add(document)
        await session.flush()

        session.add(
            ResourceDocument(
                resource_id=target.id,
                document_id=document.id,
                doc_type=doc_type,
                title=f"{doc_type.value.title()} (demo)",
                issue_date=today - timedelta(days=365),
                expiry_date=today + timedelta(days=offset),
                issuing_country=target.current_location_country,
                reference_number=f"DEMO-{uuid.uuid4().hex[:8].upper()}",
            )
        )
        stats["documents"] += 1

    # Visa status is derived from the documents, never typed by hand.
    from app.repositories.talent import DocumentRepository
    from app.services.documents import derive_visa_status

    documents_repo = DocumentRepository(session)
    await session.flush()
    for seeded in by_name.values():
        links = await documents_repo.for_resource(seeded.id)
        seeded.visa_status = derive_visa_status(links)

    logger.info("seed_resources_complete", **stats)
    return stats


__all__ = ["DOCUMENTS", "RESOURCES", "seed_resources"]
