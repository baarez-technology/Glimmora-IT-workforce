"""Requirement lifecycle, JD parsing and the human-review gate.

The rule that shapes this module: **nothing the parser produced is business data
until a human accepts it** (AD-7). A parsed requirement is created in
`PENDING_REVIEW` and is excluded from matching until reviewed, so a confident-
looking but wrong extraction can never quietly drive a submission.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.base import ExtractionResult
from app.ai.registry import extract_requirement as run_extraction
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import log_business_event
from app.core.pagination import PageParams
from app.db.types import utcnow
from app.models.demand import (
    TERMINAL_STATUSES,
    ContractType,
    PrioritySource,
    RateUnit,
    Requirement,
    RequirementSource,
    RequirementStatus,
    RequirementStatusHistory,
    ReviewStatus,
    SkillImportance,
    WorkMode,
)
from app.models.identity import AuditAction, User
from app.models.skills import Skill
from app.repositories.accounts import AccountRepository, ProjectRepository
from app.repositories.demand import RequirementRepository, SkillRepository
from app.schemas.demand import (
    AcceptParseRequest,
    ParsedFieldResponse,
    ParseTextRequest,
    RequirementCreate,
    RequirementSkillInput,
    RequirementUpdate,
)
from app.services.audit import AuditService, build_diff
from app.services.sla import default_deadline_for

#: Above this a field is pre-filled and can be accepted at a glance.
CONFIDENCE_HIGH = 0.85
#: Below this the parser is too unsure to pre-fill at all.
CONFIDENCE_LOW = 0.5

#: Money and dates always require explicit confirmation, whatever the parser
#: claims: a wrong rate corrupts every downstream commercial number, and a wrong
#: deadline loses a seat (AI_ARCHITECTURE.md section 3).
ALWAYS_CONFIRM = frozenset(
    {
        "rate_min",
        "rate_max",
        "rate_currency",
        "rate_unit",
        "response_deadline_at",
        "start_by_date",
    }
)

FIELD_LABELS: dict[str, str] = {
    "title": "Title",
    "role": "Role",
    "mandatory_skills": "Mandatory skills",
    "preferred_skills": "Preferred skills",
    "technologies": "Technologies",
    "experience_min_years": "Minimum experience",
    "experience_max_years": "Maximum experience",
    "duration_months": "Duration (months)",
    "positions": "Positions",
    "location": "Location",
    "country": "Country",
    "work_mode": "Work mode",
    "contract_type": "Contract type",
    "rate_min": "Rate from",
    "rate_max": "Rate to",
    "rate_currency": "Currency",
    "rate_unit": "Rate unit",
    "response_deadline_at": "Submission deadline",
    "start_by_date": "Start by",
    "availability_requirement": "Availability / notice",
    "customer_name": "Client named in the JD",
    "project_name": "Project named in the JD",
}

#: Extracted text fields, with the column length each is truncated to.
_TEXT_FIELDS: dict[str, int] = {
    "title": 240,
    "role": 160,
    "location": 160,
    "availability_requirement": 160,
}

#: Extracted fields that must resolve to an enum member, or be dropped.
_ENUM_FIELDS: dict[str, type[StrEnum]] = {
    "work_mode": WorkMode,
    "contract_type": ContractType,
    "rate_unit": RateUnit,
}

#: Extracted integers, with the inclusive bounds the schema accepts.
_INT_FIELDS: dict[str, tuple[int, int]] = {
    "experience_min_years": (0, 45),
    "experience_max_years": (0, 45),
    "duration_months": (1, 120),
    "positions": (1, 50),
}

AUDITED_REQUIREMENT_FIELDS = {
    "title",
    "role",
    "status",
    "priority_source",
    "response_deadline_at",
    "rate_min",
    "rate_max",
    "rate_currency",
    "rate_unit",
    "account_id",
    "route_account_id",
    "owner_id",
    "is_active",
}

#: Status transitions that make sense. A requirement cannot jump from NEW to won.
ALLOWED_TRANSITIONS: dict[RequirementStatus, set[RequirementStatus]] = {
    RequirementStatus.NEW: {
        RequirementStatus.PARSED,
        RequirementStatus.UNDER_REVIEW,
        RequirementStatus.QUALIFIED,
        RequirementStatus.ON_HOLD,
        RequirementStatus.CLOSED_LOST,
        RequirementStatus.EXPIRED,
    },
    RequirementStatus.PARSED: {
        RequirementStatus.UNDER_REVIEW,
        RequirementStatus.QUALIFIED,
        RequirementStatus.ON_HOLD,
        RequirementStatus.CLOSED_LOST,
        RequirementStatus.EXPIRED,
    },
    RequirementStatus.UNDER_REVIEW: {
        RequirementStatus.QUALIFIED,
        RequirementStatus.ON_HOLD,
        RequirementStatus.CLOSED_LOST,
        RequirementStatus.EXPIRED,
    },
    RequirementStatus.QUALIFIED: {
        RequirementStatus.ON_HOLD,
        RequirementStatus.CLOSED_WON,
        RequirementStatus.CLOSED_LOST,
        RequirementStatus.EXPIRED,
    },
    RequirementStatus.ON_HOLD: {
        RequirementStatus.QUALIFIED,
        RequirementStatus.UNDER_REVIEW,
        RequirementStatus.CLOSED_LOST,
        RequirementStatus.EXPIRED,
    },
    RequirementStatus.CLOSED_WON: set(),
    RequirementStatus.CLOSED_LOST: {RequirementStatus.QUALIFIED},
    RequirementStatus.EXPIRED: {RequirementStatus.QUALIFIED, RequirementStatus.CLOSED_LOST},
}


def confidence_level(confidence: float) -> str:
    if confidence >= CONFIDENCE_HIGH:
        return "HIGH"
    return "MEDIUM" if confidence >= CONFIDENCE_LOW else "LOW"


class RequirementService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.requirements = RequirementRepository(session)
        self.skills = SkillRepository(session)
        self.accounts = AccountRepository(session)
        self.projects = ProjectRepository(session)
        self.audit = AuditService(session)

    # ------------------------------------------------------------- reading
    async def list_requirements(self, params: PageParams, **filters: Any):
        return await self.requirements.list_requirements(params, **filters)

    async def get_requirement(self, requirement_id: uuid.UUID) -> Requirement:
        requirement = await self.requirements.get(requirement_id)
        if requirement is None:
            raise NotFoundError("requirement", requirement_id)
        return requirement

    async def history(self, requirement_id: uuid.UUID) -> list[RequirementStatusHistory]:
        await self.get_requirement(requirement_id)
        return await self.requirements.history(requirement_id)

    # ------------------------------------------------------------- writing
    async def create_requirement(self, payload: RequirementCreate, *, actor: User) -> Requirement:
        await self._validate_links(
            payload.account_id,
            payload.end_customer_id,
            payload.route_account_id,
            payload.project_id,
        )

        data = payload.model_dump(exclude={"skills"})
        data["owner_id"] = data.get("owner_id") or actor.id

        # Only a VMS/MSP source implies a deadline nobody stated (A11).
        if data.get("response_deadline_at") is None:
            data["response_deadline_at"] = default_deadline_for(payload.priority_source)

        requirement = Requirement(**data, status=RequirementStatus.NEW)
        # Typed by hand, so it is business data immediately.
        requirement.review_status = ReviewStatus.ACCEPTED
        await self.requirements.add(requirement)

        if payload.skills:
            await self._apply_skills(requirement, payload.skills)

        await self._record_status(requirement, None, RequirementStatus.NEW, actor, "Created")
        await self.audit.record(
            AuditAction.REQUIREMENT_CREATED,
            summary=f"Created requirement {requirement.title}",
            actor=actor,
            entity_type="requirement",
            entity_id=requirement.id,
        )
        log_business_event(
            "requirement_created",
            requirement_id=str(requirement.id),
            source=requirement.source.value,
        )
        return await self.get_requirement(requirement.id)

    async def update_requirement(
        self, requirement_id: uuid.UUID, payload: RequirementUpdate, *, actor: User
    ) -> Requirement:
        requirement = await self.get_requirement(requirement_id)
        before = requirement.to_dict()

        updates = payload.model_dump(exclude_unset=True)
        skills = updates.pop("skills", None)

        await self._validate_links(
            updates.get("account_id", requirement.account_id),
            updates.get("end_customer_id", requirement.end_customer_id),
            updates.get("route_account_id", requirement.route_account_id),
            updates.get("project_id", requirement.project_id),
        )

        for field, value in updates.items():
            setattr(requirement, field, value)

        self._assert_ranges(requirement)

        if skills is not None:
            await self._apply_skills(
                requirement, [RequirementSkillInput.model_validate(entry) for entry in skills]
            )

        changes = build_diff(before, requirement.to_dict(), fields=AUDITED_REQUIREMENT_FIELDS)
        await self.audit.record(
            AuditAction.REQUIREMENT_UPDATED,
            summary=f"Updated requirement {requirement.title}",
            actor=actor,
            entity_type="requirement",
            entity_id=requirement.id,
            changes=changes,
        )
        return await self.get_requirement(requirement.id)

    async def change_status(
        self,
        requirement_id: uuid.UUID,
        new_status: RequirementStatus,
        *,
        actor: User,
        reason: str | None = None,
    ) -> Requirement:
        requirement = await self.get_requirement(requirement_id)
        current = requirement.status

        if new_status == current:
            return requirement

        allowed = ALLOWED_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise ConflictError(
                f"A requirement cannot move from {current.value.replace('_', ' ').lower()} "
                f"to {new_status.value.replace('_', ' ').lower()}.",
                details=[
                    {
                        "field": "status",
                        "message": "Allowed: "
                        + (", ".join(sorted(s.value for s in allowed)) or "none"),
                    }
                ],
            )

        if new_status is RequirementStatus.QUALIFIED and requirement.is_awaiting_review:
            raise ConflictError(
                "Review the extracted fields before qualifying this requirement.",
                details=[{"field": "review_status", "message": "Still pending review"}],
            )

        requirement.status = new_status
        if new_status in TERMINAL_STATUSES:
            requirement.is_active = False

        await self._record_status(requirement, current, new_status, actor, reason)
        await self.audit.record(
            AuditAction.REQUIREMENT_STATUS_CHANGED,
            summary=f"{requirement.title}: {current.value} -> {new_status.value}",
            actor=actor,
            entity_type="requirement",
            entity_id=requirement.id,
            changes={"status": {"from": current.value, "to": new_status.value}},
        )
        return await self.get_requirement(requirement.id)

    async def archive_requirement(self, requirement_id: uuid.UUID, *, actor: User) -> Requirement:
        requirement = await self.get_requirement(requirement_id)
        requirement.deleted_at = utcnow()
        requirement.is_active = False
        await self.audit.record(
            AuditAction.REQUIREMENT_UPDATED,
            summary=f"Archived requirement {requirement.title}",
            actor=actor,
            entity_type="requirement",
            entity_id=requirement.id,
        )
        return requirement

    # ------------------------------------------------------------- parsing
    async def parse_text(self, payload: ParseTextRequest, *, actor: User) -> Requirement:
        """Create a draft requirement from raw JD text.

        The draft is deliberately not usable yet: it lands in PENDING_REVIEW and
        stays out of matching until a human accepts the extracted fields.
        """
        text = payload.text.strip()
        result = await run_extraction(text)

        priority_source = payload.priority_source or self._infer_priority_source(payload.source)

        requirement = Requirement(
            title=(result.value("title") or "Untitled requirement")[:240],
            description_raw=text,
            source=payload.source,
            source_detail=payload.source_detail,
            priority_source=priority_source,
            account_id=payload.account_id,
            project_id=payload.project_id,
            owner_id=actor.id,
            status=RequirementStatus.NEW,
            review_status=ReviewStatus.PENDING_REVIEW,
            parsed_payload=result.model_dump(mode="json"),
            parse_confidence=result.overall_confidence,
            parse_model=f"{result.provider}:{result.model_id}",
            parsed_at=utcnow(),
        )

        self._apply_extraction(requirement, result)
        await self.requirements.add(requirement)

        skill_inputs = self._skills_from_extraction(result)
        if skill_inputs:
            await self._apply_skills(requirement, skill_inputs)

        requirement.status = RequirementStatus.PARSED
        await self._record_status(
            requirement, RequirementStatus.NEW, RequirementStatus.PARSED, actor, "Parsed from JD"
        )

        await self.audit.record(
            AuditAction.JD_PARSED,
            summary=(
                f"Parsed a job description into '{requirement.title}' "
                f"({result.provider}, confidence {result.overall_confidence:.2f})"
            ),
            actor=actor,
            entity_type="requirement",
            entity_id=requirement.id,
        )
        log_business_event(
            "jd_parsed",
            requirement_id=str(requirement.id),
            provider=result.provider,
            used_fallback=result.used_fallback,
            confidence=result.overall_confidence,
        )
        return await self.get_requirement(requirement.id)

    def parse_fields_for_review(self, requirement: Requirement) -> list[ParsedFieldResponse]:
        """The parse result, shaped for the side-by-side review screen."""
        payload = requirement.parsed_payload or {}
        raw_fields: dict[str, Any] = payload.get("fields", {})

        fields: list[ParsedFieldResponse] = []
        for name, label in FIELD_LABELS.items():
            entry = raw_fields.get(name) or {}
            value = entry.get("value")
            confidence = float(entry.get("confidence") or 0.0)
            present = value is not None and value != [] and value != ""

            fields.append(
                ParsedFieldResponse(
                    field=name,
                    label=label,
                    value=value if present else None,
                    confidence=round(confidence, 3),
                    level=confidence_level(confidence) if present else "LOW",
                    # Money and dates always need a human eye, however confident
                    # the parser was.
                    requires_confirmation=(
                        present and (name in ALWAYS_CONFIRM or confidence < CONFIDENCE_HIGH)
                    ),
                    evidence=entry.get("evidence"),
                    evidence_start=entry.get("evidence_start"),
                    evidence_end=entry.get("evidence_end"),
                )
            )
        return fields

    async def accept_parse(
        self, requirement_id: uuid.UUID, payload: AcceptParseRequest, *, actor: User
    ) -> Requirement:
        requirement = await self.get_requirement(requirement_id)

        if requirement.review_status is not ReviewStatus.PENDING_REVIEW:
            raise ConflictError("This requirement has already been reviewed.")

        outstanding = self._outstanding_confirmations(requirement, set(payload.confirmed_fields))
        if outstanding:
            raise ValidationError(
                "Confirm the highlighted fields before accepting.",
                details=[
                    {"field": name, "message": f"{FIELD_LABELS.get(name, name)} needs confirming"}
                    for name in outstanding
                ],
            )

        updates = payload.updates.model_dump(exclude_unset=True)
        updates.pop("skills", None)
        for field, value in updates.items():
            setattr(requirement, field, value)

        self._assert_ranges(requirement)

        if payload.skills is not None:
            await self._apply_skills(requirement, payload.skills)

        if not requirement.title or requirement.title == "Untitled requirement":
            raise ValidationError(
                "Give the requirement a title before accepting it.",
                details=[{"field": "title", "message": "Required"}],
            )

        requirement.review_status = ReviewStatus.ACCEPTED
        previous = requirement.status
        requirement.status = RequirementStatus.UNDER_REVIEW
        await self._record_status(
            requirement, previous, RequirementStatus.UNDER_REVIEW, actor, "Parse accepted"
        )

        await self.audit.record(
            AuditAction.REQUIREMENT_UPDATED,
            summary=f"Accepted the parsed fields for {requirement.title}",
            actor=actor,
            entity_type="requirement",
            entity_id=requirement.id,
            changes={"review_status": {"from": "PENDING_REVIEW", "to": "ACCEPTED"}},
        )
        return await self.get_requirement(requirement.id)

    async def reject_parse(
        self, requirement_id: uuid.UUID, *, actor: User, reason: str | None = None
    ) -> Requirement:
        requirement = await self.get_requirement(requirement_id)
        if requirement.review_status is not ReviewStatus.PENDING_REVIEW:
            raise ConflictError("This requirement has already been reviewed.")

        requirement.review_status = ReviewStatus.REJECTED
        requirement.is_active = False
        previous = requirement.status
        requirement.status = RequirementStatus.CLOSED_LOST
        await self._record_status(
            requirement, previous, RequirementStatus.CLOSED_LOST, actor, reason or "Parse rejected"
        )
        await self.audit.record(
            AuditAction.REQUIREMENT_UPDATED,
            summary=f"Rejected the parsed requirement {requirement.title}",
            actor=actor,
            entity_type="requirement",
            entity_id=requirement.id,
        )
        return requirement

    # ------------------------------------------------------------- helpers
    def _outstanding_confirmations(
        self, requirement: Requirement, confirmed: set[str]
    ) -> list[str]:
        return [
            field.field
            for field in self.parse_fields_for_review(requirement)
            if field.requires_confirmation and field.field not in confirmed
        ]

    @staticmethod
    def _infer_priority_source(source: RequirementSource) -> PrioritySource:
        if source is RequirementSource.EMAIL:
            return PrioritySource.P2_PARTNER_PRIME
        return PrioritySource.P6_EXTERNAL_APPROVED

    def _apply_extraction(self, requirement: Requirement, result: ExtractionResult) -> None:
        """Copy extracted values onto the draft, dropping anything that is not valid.

        A real language model can return "FLEXIBLE" for a work mode or "Qatar"
        for a two-letter country code. Writing those straight onto the model
        fails at flush with a 500, which would turn a slightly-wrong extraction
        into a lost job description. Every value is therefore coerced here, and
        one that does not fit is left absent for the reviewer to fill in —
        consistent with the rule that the parser never guesses.
        """
        for name, limit in _TEXT_FIELDS.items():
            value = result.value(name)
            if isinstance(value, str) and value.strip():
                setattr(requirement, name, value.strip()[:limit])

        country = _to_country(result.value("country"))
        if country is not None:
            requirement.country = country

        for name, enum_type in _ENUM_FIELDS.items():
            member = _to_enum(result.value(name), enum_type)
            if member is not None:
                setattr(requirement, name, member)

        for name, (low, high) in _INT_FIELDS.items():
            number = _to_int(result.value(name), minimum=low, maximum=high)
            if number is not None:
                setattr(requirement, name, number)

        for name in ("rate_min", "rate_max"):
            amount = _to_decimal(result.value(name))
            if amount is not None:
                setattr(requirement, name, amount)

        currency = _to_currency(result.value("rate_currency"))
        if currency is not None:
            requirement.rate_currency = currency

        deadline = _to_datetime(result.value("response_deadline_at"))
        if deadline is not None:
            requirement.response_deadline_at = deadline

        start_by = _to_date(result.value("start_by_date"))
        if start_by is not None:
            requirement.start_by_date = start_by

        # An inverted range is a misread, not a fact. Drop the weaker half rather
        # than persist something the schema would reject on the next update.
        if (
            requirement.experience_min_years is not None
            and requirement.experience_max_years is not None
            and requirement.experience_max_years < requirement.experience_min_years
        ):
            requirement.experience_max_years = None
        if (
            requirement.rate_min is not None
            and requirement.rate_max is not None
            and requirement.rate_max < requirement.rate_min
        ):
            requirement.rate_min, requirement.rate_max = (
                requirement.rate_max,
                requirement.rate_min,
            )

    @staticmethod
    def _skills_from_extraction(result: ExtractionResult) -> list[RequirementSkillInput]:
        entries: list[RequirementSkillInput] = []
        for name in result.value("mandatory_skills") or []:
            entries.append(
                RequirementSkillInput(name=str(name), importance=SkillImportance.MANDATORY)
            )
        for name in result.value("preferred_skills") or []:
            entries.append(
                RequirementSkillInput(name=str(name), importance=SkillImportance.PREFERRED)
            )
        return entries

    async def _apply_skills(
        self, requirement: Requirement, entries: list[RequirementSkillInput]
    ) -> None:
        resolved: list[tuple[Skill, SkillImportance, int | None]] = []
        seen: set[uuid.UUID] = set()

        for entry in entries:
            skill = await self.skills.resolve(entry.name)
            if skill is None or skill.id in seen:
                continue
            seen.add(skill.id)
            resolved.append((skill, entry.importance, entry.min_years))

        await self.requirements.set_skills(requirement, resolved)

    @staticmethod
    def _assert_ranges(requirement: Requirement) -> None:
        if (
            requirement.experience_min_years is not None
            and requirement.experience_max_years is not None
            and requirement.experience_max_years < requirement.experience_min_years
        ):
            raise ValidationError(
                "Maximum experience cannot be below minimum experience.",
                details=[
                    {"field": "experience_max_years", "message": "Must be at least the minimum"}
                ],
            )
        if (
            requirement.rate_min is not None
            and requirement.rate_max is not None
            and requirement.rate_max < requirement.rate_min
        ):
            raise ValidationError(
                "The upper rate cannot be below the lower rate.",
                details=[{"field": "rate_max", "message": "Must be at least the lower rate"}],
            )

    async def _validate_links(
        self,
        account_id: uuid.UUID | None,
        end_customer_id: uuid.UUID | None,
        route_account_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
    ) -> None:
        for label, account_id_value in (
            ("account", account_id),
            ("end customer", end_customer_id),
            ("route account", route_account_id),
        ):
            if account_id_value and await self.accounts.get(account_id_value) is None:
                raise NotFoundError(label, account_id_value)
        if project_id and await self.projects.get(project_id) is None:
            raise NotFoundError("project", project_id)

    async def _record_status(
        self,
        requirement: Requirement,
        from_status: RequirementStatus | None,
        to_status: RequirementStatus,
        actor: User,
        reason: str | None,
    ) -> None:
        self.session.add(
            RequirementStatusHistory(
                requirement_id=requirement.id,
                from_status=from_status,
                to_status=to_status,
                user_id=actor.id,
                reason=reason,
            )
        )
        await self.session.flush()


# --------------------------------------------------------------- coercion


def _to_enum(value: Any, enum_type: type[StrEnum]) -> StrEnum | None:
    """Resolve a value onto an enum member, or return None.

    Matching is case- and separator-insensitive so "per month" and "Per-Month"
    both reach MONTHLY, but an unknown member is dropped rather than forced.
    """
    if value is None:
        return None
    if isinstance(value, enum_type):
        return value

    candidate = str(value).strip().upper().replace(" ", "_").replace("-", "_")
    for member in enum_type:
        if member.value.upper() == candidate:
            return member
    return None


def _to_int(value: Any, *, minimum: int, maximum: int) -> int | None:
    """Coerce to an int inside the schema's bounds, or drop it."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if minimum <= number <= maximum else None


def _to_country(value: Any) -> str | None:
    """Only a genuine ISO 3166-1 alpha-2 code; "Qatar" is not one."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if len(code) == 2 and code.isalpha() else None


def _to_currency(value: Any) -> str | None:
    """Only a genuine ISO 4217 code."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if len(code) == 3 and code.isalpha() else None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount >= 0 else None


def _to_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _to_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


__all__ = [
    "ALLOWED_TRANSITIONS",
    "ALWAYS_CONFIRM",
    "AUDITED_REQUIREMENT_FIELDS",
    "CONFIDENCE_HIGH",
    "CONFIDENCE_LOW",
    "FIELD_LABELS",
    "RequirementService",
    "confidence_level",
]
