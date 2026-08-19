"""Resource lifecycle, CV parsing and the document store."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.extraction.text import extract_text
from app.ai.providers.cv_parser import parse_cv_text
from app.core.config import settings
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import log_business_event
from app.core.pagination import PageParams
from app.db.types import utcnow
from app.models.identity import AuditAction, User
from app.models.talent import (
    AvailabilityStatus,
    Document,
    DocumentType,
    Resource,
    ResourceDocument,
    ResourceType,
)
from app.repositories.accounts import AccountRepository
from app.repositories.talent import DocumentRepository, ResourceRepository, SkillLookup
from app.schemas.talent import (
    AcceptCVRequest,
    DuplicateMatch,
    ParsedCVField,
    ResourceCreate,
    ResourceDocumentCreate,
    ResourceDocumentUpdate,
    ResourceSkillInput,
    ResourceUpdate,
)
from app.services.audit import AuditService, build_diff
from app.storage.service import delete_object, store_upload

CONFIDENCE_HIGH = 0.85
CONFIDENCE_LOW = 0.5

#: Contact details and computed experience always need a human eye: a wrong
#: email sends a CV to the wrong client, and experience years feed every match.
ALWAYS_CONFIRM_CV = frozenset({"email", "phone", "total_experience_years", "full_name"})

CV_FIELD_LABELS: dict[str, str] = {
    "full_name": "Full name",
    "email": "Email",
    "phone": "Phone",
    "headline": "Headline / role",
    "summary": "Summary",
    "current_location_city": "City",
    "current_location_country": "Country",
    "total_experience_years": "Total experience (computed from dates)",
    "experience_entries": "Experience history",
    "skills": "Skills",
    "technologies": "Technologies",
    "certifications": "Certifications",
    "notice_period_days": "Notice period (days)",
}

AUDITED_RESOURCE_FIELDS = {
    "full_name",
    "email",
    "resource_type",
    "availability_status",
    "available_from",
    "notice_period_days",
    "visa_status",
    "expected_cost_amount",
    "target_billing_amount",
    "owner_id",
}


def confidence_level(confidence: float) -> str:
    if confidence >= CONFIDENCE_HIGH:
        return "HIGH"
    return "MEDIUM" if confidence >= CONFIDENCE_LOW else "LOW"


class ResourceService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.resources = ResourceRepository(session)
        self.documents = DocumentRepository(session)
        self.skills = SkillLookup(session)
        self.accounts = AccountRepository(session)
        self.audit = AuditService(session)

    # ------------------------------------------------------------- reading
    async def list_resources(self, params: PageParams, **filters: Any):
        return await self.resources.list_resources(params, **filters)

    async def get_resource(self, resource_id: uuid.UUID) -> Resource:
        resource = await self.resources.get(resource_id)
        if resource is None:
            raise NotFoundError("resource", resource_id)
        return resource

    @staticmethod
    def ready_from(resource: Resource, *, today: date | None = None) -> date:
        """The earliest date this person can actually start.

        Availability alone is not enough — a consultant available in 30 days
        with a 60-day notice period is available in 60 (MATCHING.md section 1).
        """
        from datetime import timedelta

        reference = today or datetime.now(UTC).date()
        notice_ready = reference + timedelta(days=resource.notice_period_days or 0)
        if resource.available_from is None:
            return notice_ready
        return max(resource.available_from, notice_ready)

    # ------------------------------------------------------------- writing
    async def create_resource(self, payload: ResourceCreate, *, actor: User) -> Resource:
        if (
            payload.partner_account_id
            and await self.accounts.get(payload.partner_account_id) is None
        ):
            raise NotFoundError("partner account", payload.partner_account_id)

        duplicates = await self.find_duplicates(
            email=str(payload.email) if payload.email else None,
            phone=payload.phone,
            full_name=payload.full_name,
        )
        exact = [match for match in duplicates if match.confidence >= 0.9]
        if exact:
            raise ConflictError(
                "A resource with those contact details already exists.",
                details=[
                    {"field": "email", "message": f"{match.full_name} — {match.reason}"}
                    for match in exact
                ],
            )

        data = payload.model_dump(exclude={"skills", "experience", "certifications"})
        data["email"] = str(data["email"]).lower() if data.get("email") else None
        data["owner_id"] = data.get("owner_id") or actor.id

        resource = Resource(**data, code=await self.resources.next_code())
        resource.review_status = "ACCEPTED"
        resource.source = "MANUAL"
        await self.resources.add(resource)

        if payload.skills:
            await self._apply_skills(resource, payload.skills)
        if payload.experience:
            await self.resources.replace_experience(
                resource, [entry.model_dump() for entry in payload.experience]
            )
        if payload.certifications:
            await self.resources.replace_certifications(
                resource, [entry.model_dump() for entry in payload.certifications]
            )

        await self.audit.record(
            AuditAction.RESOURCE_CREATED,
            summary=f"Added resource {resource.full_name} ({resource.resource_type.value})",
            actor=actor,
            entity_type="resource",
            entity_id=resource.id,
        )
        return await self.get_resource(resource.id)

    async def update_resource(
        self, resource_id: uuid.UUID, payload: ResourceUpdate, *, actor: User
    ) -> Resource:
        resource = await self.get_resource(resource_id)
        before = resource.to_dict()

        updates = payload.model_dump(exclude_unset=True)
        skills = updates.pop("skills", None)
        experience = updates.pop("experience", None)
        certifications = updates.pop("certifications", None)

        if updates.get("email"):
            updates["email"] = str(updates["email"]).lower()

        for field, value in updates.items():
            setattr(resource, field, value)

        if skills is not None:
            await self._apply_skills(
                resource, [ResourceSkillInput.model_validate(entry) for entry in skills]
            )
        if experience is not None:
            await self.resources.replace_experience(resource, experience)
        if certifications is not None:
            await self.resources.replace_certifications(resource, certifications)

        changes = build_diff(before, resource.to_dict(), fields=AUDITED_RESOURCE_FIELDS)
        await self.audit.record(
            AuditAction.RESOURCE_UPDATED,
            summary=f"Updated resource {resource.full_name}",
            actor=actor,
            entity_type="resource",
            entity_id=resource.id,
            changes=changes,
        )
        return await self.get_resource(resource.id)

    async def archive_resource(self, resource_id: uuid.UUID, *, actor: User) -> Resource:
        resource = await self.get_resource(resource_id)
        resource.deleted_at = utcnow()
        resource.availability_status = AvailabilityStatus.NOT_AVAILABLE
        await self.audit.record(
            AuditAction.RESOURCE_UPDATED,
            summary=f"Archived resource {resource.full_name}",
            actor=actor,
            entity_type="resource",
            entity_id=resource.id,
        )
        return resource

    # ---------------------------------------------------------- duplicates
    async def find_duplicates(
        self, *, email: str | None, phone: str | None, full_name: str | None
    ) -> list[DuplicateMatch]:
        matches = await self.resources.find_duplicates(
            email=email, phone=phone, full_name=full_name
        )
        return [
            DuplicateMatch(
                resource_id=resource.id,
                full_name=resource.full_name,
                email=resource.email,
                reason=reason,
                confidence=confidence,
            )
            for resource, reason, confidence in matches
        ]

    # ------------------------------------------------------------ CV parse
    async def parse_cv(
        self, *, filename: str, content: bytes, actor: User
    ) -> tuple[Resource, list[DuplicateMatch]]:
        """Create a draft resource from an uploaded CV.

        The CV file is stored and attached before parsing, so a parse failure
        never loses the document the recruiter just uploaded.
        """
        text = extract_text(filename, content, max_bytes=settings.MAX_UPLOAD_BYTES)
        result = parse_cv_text(text)

        email = result.value("email")
        phone = result.value("phone")
        full_name = result.value("full_name") or "Unnamed candidate"

        duplicates = await self.find_duplicates(email=email, phone=phone, full_name=full_name)

        resource = Resource(
            code=await self.resources.next_code(),
            full_name=str(full_name)[:160],
            email=str(email).lower()[:255] if email else None,
            phone=str(phone)[:48] if phone else None,
            headline=str(result.value("headline"))[:240] if result.value("headline") else None,
            summary=str(result.value("summary")) if result.value("summary") else None,
            resource_type=ResourceType.PREVIOUS_CANDIDATE,
            availability_status=AvailabilityStatus.NOT_AVAILABLE,
            owner_id=actor.id,
            source="CV_UPLOAD",
            review_status="PENDING_REVIEW",
            parsed_payload=result.model_dump(mode="json"),
            parse_confidence=result.overall_confidence,
            parse_model=f"{result.provider}:{result.model_id}",
            parsed_at=utcnow(),
        )

        country = result.value("current_location_country")
        if isinstance(country, str) and len(country) == 2:
            resource.current_location_country = country.upper()
        city = result.value("current_location_city")
        if isinstance(city, str):
            resource.current_location_city = city[:120]

        years = result.value("total_experience_years")
        if isinstance(years, (int, float)) and 0 <= years <= 60:
            resource.total_experience_years = float(years)

        notice = result.value("notice_period_days")
        if isinstance(notice, int) and 0 <= notice <= 365:
            resource.notice_period_days = notice

        await self.resources.add(resource)

        skill_names = result.value("skills") or []
        if skill_names:
            await self._apply_skills(
                resource, [ResourceSkillInput(name=str(name)) for name in skill_names]
            )

        entries = result.value("experience_entries") or []
        if entries:
            await self.resources.replace_experience(
                resource,
                [
                    {
                        "role": entry.get("role"),
                        "start_date": _to_date(entry.get("start_date")),
                        "end_date": _to_date(entry.get("end_date")),
                        "is_current": bool(entry.get("is_current")),
                    }
                    for entry in entries
                ],
            )

        certifications = result.value("certifications") or []
        if certifications:
            await self.resources.replace_certifications(
                resource, [{"name": str(name)[:200]} for name in certifications]
            )

        # Store the CV itself so the review screen and any future submission
        # have the original document, whatever the parse produced.
        stored = store_upload(filename, content)
        document = Document(
            storage_key=stored.storage_key,
            storage_backend=stored.backend,
            original_filename=stored.original_filename,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            checksum_sha256=stored.checksum_sha256,
            uploaded_by=actor.id,
        )
        await self.documents.add_file(document)
        self.session.add(
            ResourceDocument(
                resource_id=resource.id,
                document_id=document.id,
                doc_type=DocumentType.CV,
                title="CV (uploaded)",
            )
        )
        await self.session.flush()

        await self.audit.record(
            AuditAction.CV_PARSED,
            summary=(
                f"Parsed a CV into '{resource.full_name}' "
                f"({result.provider}, confidence {result.overall_confidence:.2f})"
            ),
            actor=actor,
            entity_type="resource",
            entity_id=resource.id,
        )
        log_business_event(
            "cv_parsed",
            resource_id=str(resource.id),
            provider=result.provider,
            confidence=result.overall_confidence,
            duplicates=len(duplicates),
        )
        return await self.get_resource(resource.id), duplicates

    def cv_fields_for_review(self, resource: Resource) -> list[ParsedCVField]:
        payload = resource.parsed_payload or {}
        raw = payload.get("fields", {})

        fields: list[ParsedCVField] = []
        for name, label in CV_FIELD_LABELS.items():
            entry = raw.get(name) or {}
            value = entry.get("value")
            confidence = float(entry.get("confidence") or 0.0)
            present = value is not None and value != [] and value != ""

            fields.append(
                ParsedCVField(
                    field=name,
                    label=label,
                    value=value if present else None,
                    confidence=round(confidence, 3),
                    level=confidence_level(confidence) if present else "LOW",
                    requires_confirmation=(
                        present and (name in ALWAYS_CONFIRM_CV or confidence < CONFIDENCE_HIGH)
                    ),
                    evidence=entry.get("evidence"),
                )
            )
        return fields

    async def accept_cv(
        self, resource_id: uuid.UUID, payload: AcceptCVRequest, *, actor: User
    ) -> Resource:
        resource = await self.get_resource(resource_id)
        if resource.review_status != "PENDING_REVIEW":
            raise ConflictError("This resource has already been reviewed.")

        confirmed = set(payload.confirmed_fields)
        outstanding = [
            field.field
            for field in self.cv_fields_for_review(resource)
            if field.requires_confirmation and field.field not in confirmed
        ]
        if outstanding:
            raise ValidationError(
                "Confirm the highlighted fields before accepting.",
                details=[
                    {
                        "field": name,
                        "message": f"{CV_FIELD_LABELS.get(name, name)} needs confirming",
                    }
                    for name in outstanding
                ],
            )

        updates = payload.updates.model_dump(exclude_unset=True)
        for key in ("skills", "experience", "certifications"):
            updates.pop(key, None)
        if updates.get("email"):
            updates["email"] = str(updates["email"]).lower()
        for field, value in updates.items():
            setattr(resource, field, value)

        if payload.skills is not None:
            await self._apply_skills(resource, payload.skills)

        if not resource.full_name or resource.full_name == "Unnamed candidate":
            raise ValidationError(
                "Give the candidate a name before accepting.",
                details=[{"field": "full_name", "message": "Required"}],
            )

        resource.review_status = "ACCEPTED"
        await self.audit.record(
            AuditAction.RESOURCE_UPDATED,
            summary=f"Accepted the parsed CV for {resource.full_name}",
            actor=actor,
            entity_type="resource",
            entity_id=resource.id,
            changes={"review_status": {"from": "PENDING_REVIEW", "to": "ACCEPTED"}},
        )
        return await self.get_resource(resource.id)

    # ----------------------------------------------------------- documents
    async def add_document(
        self,
        resource_id: uuid.UUID,
        payload: ResourceDocumentCreate,
        *,
        filename: str,
        content: bytes,
        actor: User,
    ) -> ResourceDocument:
        resource = await self.get_resource(resource_id)

        if payload.doc_type in {DocumentType.VISA, DocumentType.WORK_PERMIT, DocumentType.QID}:
            if payload.expiry_date is None:
                raise ValidationError(
                    "A work-authorisation document must carry an expiry date.",
                    details=[
                        {
                            "field": "expiry_date",
                            "message": "Required — an expired permit stops billing",
                        }
                    ],
                )
            if payload.issue_date and payload.expiry_date < payload.issue_date:
                raise ValidationError(
                    "The expiry date cannot be before the issue date.",
                    details=[{"field": "expiry_date", "message": "Must follow the issue date"}],
                )

        stored = store_upload(filename, content)
        document = Document(
            storage_key=stored.storage_key,
            storage_backend=stored.backend,
            original_filename=stored.original_filename,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            checksum_sha256=stored.checksum_sha256,
            uploaded_by=actor.id,
        )
        await self.documents.add_file(document)

        link = ResourceDocument(
            resource_id=resource.id,
            document_id=document.id,
            **payload.model_dump(),
        )
        await self.documents.add(link)

        await self._refresh_visa_status(resource)

        await self.audit.record(
            AuditAction.DOCUMENT_UPLOADED,
            summary=f"Uploaded {payload.doc_type.value} for {resource.full_name}",
            actor=actor,
            entity_type="resource_document",
            entity_id=link.id,
        )
        return link

    async def update_document(
        self, document_id: uuid.UUID, payload: ResourceDocumentUpdate, *, actor: User
    ) -> ResourceDocument:
        link = await self.documents.get(document_id)
        if link is None:
            raise NotFoundError("document", document_id)

        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(link, field, value)

        resource = await self.get_resource(link.resource_id)
        await self._refresh_visa_status(resource)

        await self.audit.record(
            AuditAction.DOCUMENT_UPDATED,
            summary=f"Updated a {link.doc_type.value} document",
            actor=actor,
            entity_type="resource_document",
            entity_id=link.id,
        )
        return link

    async def delete_document(self, document_id: uuid.UUID, *, actor: User) -> None:
        link = await self.documents.get(document_id)
        if link is None:
            raise NotFoundError("document", document_id)

        file_record = await self.documents.get_file(link.document_id)
        resource_id = link.resource_id
        doc_type = link.doc_type

        await self.documents.delete(link)
        if file_record is not None:
            delete_object(file_record.storage_key)
            await self.session.delete(file_record)

        resource = await self.get_resource(resource_id)
        await self._refresh_visa_status(resource)

        await self.audit.record(
            AuditAction.DOCUMENT_DELETED,
            summary=f"Deleted a {doc_type.value} document",
            actor=actor,
            entity_type="resource_document",
            entity_id=document_id,
        )

    async def _refresh_visa_status(self, resource: Resource) -> None:
        """Keep the resource's visa status in step with its documents.

        Derived rather than typed, so it cannot drift out of date the way a
        manually-maintained field does.
        """
        from app.services.documents import derive_visa_status

        documents = await self.documents.for_resource(resource.id)
        resource.visa_status = derive_visa_status(documents)

    async def _apply_skills(self, resource: Resource, entries: list[ResourceSkillInput]) -> None:
        resolved: list[tuple[Any, dict[str, Any]]] = []
        seen: set[uuid.UUID] = set()

        for entry in entries:
            skill = await self.skills.resolve(entry.name)
            if skill is None or skill.id in seen:
                continue
            seen.add(skill.id)
            resolved.append(
                (
                    skill,
                    {
                        "years": entry.years,
                        "proficiency": entry.proficiency,
                        "last_used_year": entry.last_used_year,
                        "is_primary": entry.is_primary,
                    },
                )
            )

        await self.resources.set_skills(resource, resolved)


def _to_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


__all__ = [
    "ALWAYS_CONFIRM_CV",
    "AUDITED_RESOURCE_FIELDS",
    "CV_FIELD_LABELS",
    "ResourceService",
    "confidence_level",
]
