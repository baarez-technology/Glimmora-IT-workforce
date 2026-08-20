"""Excel import: stage, preview, commit.

The guarantee: **an invalid row is never written to a business table.** Upload
parses and validates into staging only; commit writes the rows a human has seen
classified as valid. Nothing in between touches real data.

Duplicates are skipped rather than merged. A re-import of last month's
spreadsheet is a common accident, and creating a second Milaha is much harder to
undo than not creating it.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger, log_business_event
from app.db.types import utcnow
from app.engines.importing.schema import (
    CoercionError,
    EntitySchema,
    coerce,
    schema_for,
)
from app.engines.importing.workbook import ParsedSheet, WorkbookError, parse
from app.models.accounts import Account, AccountType, Contact, Project, RelationshipStatus
from app.models.demand import (
    PrioritySource,
    RateUnit,
    Requirement,
    RequirementSkill,
    SkillImportance,
)
from app.models.identity import AuditAction, User
from app.models.platform import (
    COMMITTABLE_STATES,
    ImportBatch,
    ImportEntity,
    ImportRow,
    ImportStatus,
    RowState,
)
from app.models.talent import AvailabilityStatus, Resource, ResourceSkill, ResourceType
from app.repositories.demand import SkillRepository
from app.services.audit import AuditService

logger = get_logger("importing")


class ImportService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.audit = AuditService(session)

    # ------------------------------------------------------------- staging
    async def stage(
        self,
        *,
        entity: ImportEntity,
        payload: bytes,
        filename: str,
        actor: User,
    ) -> ImportBatch:
        """Parse, validate and store. Writes nothing to business tables."""
        schema = schema_for(entity)

        try:
            sheet = parse(payload, filename=filename, schema=schema)
        except (WorkbookError, CoercionError) as exc:
            raise ValidationError(
                str(exc), details=[{"field": "file", "message": str(exc)}]
            ) from exc

        batch = ImportBatch(
            entity_type=entity,
            filename=filename,
            status=ImportStatus.STAGED,
            created_by=actor.id,
            file_errors=[f"Required column missing: {label}" for label in sheet.missing_required],
        )
        self.session.add(batch)
        await self.session.flush()

        # A missing required column makes every row invalid, so say it once at
        # the file level rather than repeating it on 400 rows.
        if sheet.missing_required:
            batch.total_rows = len(sheet.rows)
            batch.invalid_rows = len(sheet.rows)
            await self.session.flush()
            return batch

        await self._validate_rows(batch, sheet, schema)

        await self.audit.record(
            AuditAction.IMPORT_STAGED,
            summary=(
                f"Staged {batch.total_rows} {entity.value} rows from {filename}: "
                f"{batch.valid_rows} valid, {batch.invalid_rows} invalid, "
                f"{batch.duplicate_rows} duplicate"
            ),
            actor=actor,
            entity_type="import_batch",
            entity_id=batch.id,
        )
        log_business_event(
            "import_staged",
            batch_id=str(batch.id),
            entity=entity.value,
            total=batch.total_rows,
            invalid=batch.invalid_rows,
        )
        return batch

    async def _validate_rows(
        self, batch: ImportBatch, sheet: ParsedSheet, schema: EntitySchema
    ) -> None:
        seen_in_file: dict[tuple[Any, ...], int] = {}

        for row_number, raw in sheet.rows:
            errors: list[str] = []
            warnings: list[str] = []
            normalized: dict[str, Any] = {}

            for column in schema.columns:
                try:
                    normalized[column.key] = coerce(raw.get(column.key), column)
                except CoercionError as exc:
                    errors.append(str(exc))

            state = RowState.INVALID if errors else RowState.VALID
            duplicate_of: uuid.UUID | None = None

            if not errors:
                # Within-file duplicates first: two rows for the same person in
                # one upload should not both be created.
                key = self._identity_key(normalized, schema)
                if key is not None and key in seen_in_file:
                    state = RowState.DUPLICATE
                    warnings.append(f"Duplicates row {seen_in_file[key]} in this file")
                elif key is not None:
                    seen_in_file[key] = row_number
                    existing = await self._find_existing(normalized, schema)
                    if existing is not None:
                        state = RowState.DUPLICATE
                        duplicate_of = existing
                        warnings.append("Already exists — will be skipped")

                if state is not RowState.DUPLICATE:
                    warnings.extend(self._advisories(normalized, schema))
                    if warnings:
                        state = RowState.WARNING

            self.session.add(
                ImportRow(
                    batch_id=batch.id,
                    row_number=row_number,
                    raw={key: _jsonable(value) for key, value in raw.items()},
                    normalized={key: _jsonable(value) for key, value in normalized.items()},
                    validation_state=state,
                    errors=errors or None,
                    warnings=warnings or None,
                    duplicate_of_id=duplicate_of,
                )
            )

            batch.total_rows += 1
            if state is RowState.VALID:
                batch.valid_rows += 1
            elif state is RowState.INVALID:
                batch.invalid_rows += 1
            elif state is RowState.DUPLICATE:
                batch.duplicate_rows += 1
            else:
                batch.warning_rows += 1

        if sheet.unknown_headers:
            batch.file_errors = (batch.file_errors or []) + [
                f"Ignored unrecognised column: {header}" for header in sheet.unknown_headers
            ]
        await self.session.flush()

    def _identity_key(
        self, normalized: dict[str, Any], schema: EntitySchema
    ) -> tuple[Any, ...] | None:
        for fields in schema.identity_fields:
            values = tuple(str(normalized.get(field) or "").strip().lower() for field in fields)
            if all(values):
                return (fields, *values)
        return None

    def _advisories(self, normalized: dict[str, Any], schema: EntitySchema) -> list[str]:
        """Importable, but worth knowing. Never blocks a row."""
        notes: list[str] = []

        if schema.entity is ImportEntity.REQUIREMENTS:
            if not normalized.get("rate_max") and not normalized.get("rate_min"):
                notes.append("No rate — the commercial score cannot be computed")
            if not normalized.get("account_name"):
                notes.append("No account — addressability cannot be assessed")

        if schema.entity is ImportEntity.RESOURCES:
            if not normalized.get("skills"):
                notes.append("No skills — this consultant will not match anything")
            if not normalized.get("expected_cost_amount"):
                notes.append("No cost rate — margin cannot be calculated")

        if schema.entity is ImportEntity.CUSTOMERS and not normalized.get("country"):
            notes.append("No country — duplicate detection is weaker without one")

        return notes

    async def _find_existing(
        self, normalized: dict[str, Any], schema: EntitySchema
    ) -> uuid.UUID | None:
        entity = schema.entity

        if entity is ImportEntity.CUSTOMERS:
            stmt = select(Account.id).where(
                func.lower(Account.name) == str(normalized["name"]).lower()
            )
            if normalized.get("country"):
                stmt = stmt.where(Account.country == normalized["country"])
            return (await self.session.execute(stmt)).scalar()

        if entity is ImportEntity.CONTACTS and normalized.get("email"):
            return (
                await self.session.execute(
                    select(Contact.id).where(
                        func.lower(Contact.email) == str(normalized["email"]).lower()
                    )
                )
            ).scalar()

        if entity is ImportEntity.RESOURCES:
            if normalized.get("email"):
                return (
                    await self.session.execute(
                        select(Resource.id).where(
                            func.lower(Resource.email) == str(normalized["email"]).lower(),
                            Resource.deleted_at.is_(None),
                        )
                    )
                ).scalar()
            return (
                await self.session.execute(
                    select(Resource.id).where(
                        func.lower(Resource.full_name) == str(normalized["full_name"]).lower(),
                        Resource.deleted_at.is_(None),
                    )
                )
            ).scalar()

        if entity is ImportEntity.PROJECTS:
            return (
                await self.session.execute(
                    select(Project.id).where(
                        func.lower(Project.name) == str(normalized["name"]).lower()
                    )
                )
            ).scalar()

        if entity is ImportEntity.REQUIREMENTS:
            return (
                await self.session.execute(
                    select(Requirement.id).where(
                        func.lower(Requirement.title) == str(normalized["title"]).lower(),
                        Requirement.deleted_at.is_(None),
                    )
                )
            ).scalar()

        return None

    # ------------------------------------------------------------ reading
    async def get(self, batch_id: uuid.UUID) -> ImportBatch:
        batch = (
            await self.session.execute(select(ImportBatch).where(ImportBatch.id == batch_id))
        ).scalar_one_or_none()
        if batch is None:
            raise NotFoundError("import batch", batch_id)
        return batch

    async def rows(
        self, batch_id: uuid.UUID, *, state: RowState | None = None, limit: int = 500
    ) -> list[ImportRow]:
        stmt = select(ImportRow).where(ImportRow.batch_id == batch_id)
        if state is not None:
            stmt = stmt.where(ImportRow.validation_state == state)
        stmt = stmt.order_by(ImportRow.row_number).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_batches(self, *, limit: int = 50) -> list[ImportBatch]:
        rows = await self.session.execute(
            select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(limit)
        )
        return list(rows.scalars().all())

    # ----------------------------------------------------------- committing
    async def commit(self, batch: ImportBatch, *, actor: User) -> dict[str, int]:
        if batch.status is ImportStatus.COMMITTED:
            raise ConflictError(
                "This batch has already been committed.",
                details=[{"field": "batch_id", "message": "Already committed"}],
            )
        if batch.status is ImportStatus.DISCARDED:
            raise ConflictError(
                "This batch was discarded.",
                details=[{"field": "batch_id", "message": "Discarded"}],
            )

        rows = await self.rows(batch.id, limit=10_000)
        committable = [row for row in rows if row.validation_state in COMMITTABLE_STATES]

        if not committable:
            raise ValidationError(
                "There is nothing to import — every row is invalid or a duplicate.",
                details=[{"field": "batch_id", "message": "No importable rows"}],
            )

        created = 0
        skipped = 0
        for row in committable:
            if row.created_entity_id is not None:
                skipped += 1
                continue
            entity_id = await self._create(batch.entity_type, row.normalized or {}, actor=actor)
            if entity_id is None:
                skipped += 1
                continue
            row.created_entity_id = entity_id
            created += 1

        batch.status = ImportStatus.COMMITTED
        batch.committed_rows = created
        batch.committed_at = utcnow()
        batch.committed_by = actor.id
        await self.session.flush()

        await self.audit.record(
            AuditAction.IMPORT_COMMITTED,
            summary=(
                f"Imported {created} {batch.entity_type.value} from {batch.filename} "
                f"({batch.invalid_rows} invalid rows were never written)"
            ),
            actor=actor,
            entity_type="import_batch",
            entity_id=batch.id,
        )
        log_business_event(
            "import_committed", batch_id=str(batch.id), created=created, skipped=skipped
        )
        return {"created": created, "skipped": skipped, "never_written": batch.invalid_rows}

    async def discard(self, batch: ImportBatch, *, actor: User) -> ImportBatch:
        if batch.status is ImportStatus.COMMITTED:
            raise ConflictError(
                "A committed batch cannot be discarded.",
                details=[{"field": "batch_id", "message": "Already committed"}],
            )
        batch.status = ImportStatus.DISCARDED
        await self.session.flush()
        return batch

    # -------------------------------------------------------------- writers
    async def _create(
        self, entity: ImportEntity, data: dict[str, Any], *, actor: User
    ) -> uuid.UUID | None:
        if entity is ImportEntity.CUSTOMERS:
            return await self._create_account(data, actor=actor)
        if entity is ImportEntity.CONTACTS:
            return await self._create_contact(data)
        if entity is ImportEntity.PROJECTS:
            return await self._create_project(data)
        if entity is ImportEntity.REQUIREMENTS:
            return await self._create_requirement(data, actor=actor)
        if entity is ImportEntity.RESOURCES:
            return await self._create_resource(data, actor=actor)
        return None

    async def _create_account(self, data: dict[str, Any], *, actor: User) -> uuid.UUID:
        account = Account(
            name=data["name"],
            account_type=AccountType(data.get("account_type") or "CUSTOMER"),
            country=data.get("country"),
            city=data.get("city"),
            industry=data.get("industry"),
            website=data.get("website"),
            relationship_status=RelationshipStatus(data.get("relationship_status") or "TARGET"),
            is_existing_customer=bool(data.get("is_existing_customer")),
            is_existing_partner=bool(data.get("is_existing_partner")),
            is_approved_vendor=bool(data.get("is_approved_vendor")),
            has_msa=bool(data.get("has_msa")),
            contract_outsourcing_friendly=bool(data.get("contract_outsourcing_friendly")),
            payment_terms_days=data.get("payment_terms_days"),
            notes=data.get("notes"),
            owner_id=actor.id,
        )
        self.session.add(account)
        await self.session.flush()
        return account.id

    async def _account_id(self, name: str | None) -> uuid.UUID | None:
        if not name:
            return None
        return (
            await self.session.execute(
                select(Account.id).where(func.lower(Account.name) == str(name).lower())
            )
        ).scalar()

    async def _create_contact(self, data: dict[str, Any]) -> uuid.UUID | None:
        account_id = await self._account_id(data.get("account_name"))
        if account_id is None:
            return None

        contact = Contact(
            account_id=account_id,
            full_name=data["full_name"],
            job_title=data.get("job_title"),
            email=data.get("email"),
            phone=data.get("phone"),
            is_decision_maker=bool(data.get("is_decision_maker")),
            notes=data.get("notes"),
        )
        self.session.add(contact)
        await self.session.flush()
        return contact.id

    async def _create_project(self, data: dict[str, Any]) -> uuid.UUID | None:
        account_id = await self._account_id(data.get("account_name"))
        if account_id is None:
            return None

        project = Project(
            account_id=account_id,
            name=data["name"],
            description=data.get("description"),
            start_date=_as_date(data.get("start_date")),
            end_date=_as_date(data.get("end_date")),
        )
        self.session.add(project)
        await self.session.flush()
        return project.id

    async def _create_requirement(self, data: dict[str, Any], *, actor: User) -> uuid.UUID:
        requirement = Requirement(
            title=data["title"],
            role=data.get("role"),
            account_id=await self._account_id(data.get("account_name")),
            positions=data.get("positions") or 1,
            priority_source=PrioritySource(data.get("priority_source") or "P6_EXTERNAL_APPROVED"),
            country=data.get("country"),
            location=data.get("location"),
            experience_min_years=data.get("experience_min_years"),
            duration_months=data.get("duration_months"),
            rate_min=_as_decimal(data.get("rate_min")),
            rate_max=_as_decimal(data.get("rate_max")),
            rate_currency=data.get("rate_currency") or "QAR",
            rate_unit=RateUnit(data["rate_unit"]) if data.get("rate_unit") else None,
            owner_id=actor.id,
            # Imported rows are business data immediately: a human chose to
            # commit them, which is the review AD-7 asks for.
            review_status="ACCEPTED",
        )
        self.session.add(requirement)
        await self.session.flush()

        skills = SkillRepository(self.session)
        for name in data.get("skills") or []:
            skill = await skills.resolve(str(name))
            if skill is None:
                continue
            self.session.add(
                RequirementSkill(
                    requirement_id=requirement.id,
                    skill_id=skill.id,
                    importance=SkillImportance.MANDATORY,
                )
            )
        await self.session.flush()
        return requirement.id

    async def _create_resource(self, data: dict[str, Any], *, actor: User) -> uuid.UUID:
        resource = Resource(
            full_name=data["full_name"],
            email=data.get("email"),
            phone=data.get("phone"),
            resource_type=ResourceType(data.get("resource_type") or "CONSULTANT"),
            headline=data.get("headline"),
            total_experience_years=data.get("total_experience_years"),
            current_location_country=data.get("current_location_country"),
            current_location_city=data.get("current_location_city"),
            availability_status=AvailabilityStatus(
                data.get("availability_status") or "NOT_AVAILABLE"
            ),
            available_from=_as_date(data.get("available_from")),
            notice_period_days=data.get("notice_period_days") or 0,
            expected_cost_amount=_as_decimal(data.get("expected_cost_amount")),
            expected_cost_currency=data.get("expected_cost_currency"),
            expected_cost_unit=data.get("expected_cost_unit"),
            owner_id=actor.id,
            review_status="ACCEPTED",
        )
        self.session.add(resource)
        await self.session.flush()

        skills = SkillRepository(self.session)
        for name in data.get("skills") or []:
            skill = await skills.resolve(str(name))
            if skill is not None:
                self.session.add(ResourceSkill(resource_id=resource.id, skill_id=skill.id))
        await self.session.flush()
        return resource.id


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _as_date(value: Any) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _as_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


__all__ = ["ImportService"]
