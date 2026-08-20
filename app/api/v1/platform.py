"""Notifications, Excel import and export.

The import surface is deliberately three separate calls. A single "upload and
import" endpoint would be easier to build and impossible to trust: the point of
the preview step is that a human sees what will be written before anything is.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field

from app.core.deps import SessionDep, require
from app.core.permissions import Permission
from app.engines.importing.schema import SUPPORTED_IMPORTS, schema_for
from app.engines.importing.workbook import write_error_report, write_template
from app.models.identity import User
from app.models.notifications import NotificationCategory, NotificationSeverity
from app.models.platform import ImportEntity, ImportStatus, RowState
from app.services.exporting import ExportService
from app.services.importing import ImportService
from app.services.notifications import NotificationService, NotificationSweeps

notifications_router = APIRouter(prefix="/notifications", tags=["notifications"])
imports_router = APIRouter(prefix="/imports", tags=["imports"])
exports_router = APIRouter(prefix="/exports", tags=["exports"])

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
#: Spreadsheets are small; anything larger is a mistake or an attack.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


# ------------------------------------------------------------------ schemas


class NotificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category: NotificationCategory
    severity: NotificationSeverity
    title: str
    body: str | None
    entity_type: str | None
    entity_id: uuid.UUID | None
    action_url: str | None
    payload: dict[str, Any] | None
    is_read: bool
    read_at: datetime | None
    created_at: datetime


class UnreadCount(BaseModel):
    total: int
    critical: int
    by_category: dict[str, int]


class SweepResult(BaseModel):
    submission_sla: dict[str, int]
    document_expiry: dict[str, int]
    follow_up_overdue: dict[str, int]
    project_ending: dict[str, int]


class ImportRowResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    row_number: int
    validation_state: RowState
    raw: dict[str, Any] | None
    normalized: dict[str, Any] | None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_entity_id: uuid.UUID | None


class ImportBatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity_type: ImportEntity
    filename: str
    status: ImportStatus
    total_rows: int
    valid_rows: int
    invalid_rows: int
    duplicate_rows: int
    warning_rows: int
    committed_rows: int
    file_errors: list[str] = Field(default_factory=list)
    is_committable: bool = False
    created_at: datetime
    committed_at: datetime | None


class ImportPreview(BaseModel):
    batch: ImportBatchResponse
    rows: list[ImportRowResponse]


class CommitResult(BaseModel):
    created: int
    skipped: int
    #: Invalid rows, which were never written to a business table.
    never_written: int


class ImportEntityInfo(BaseModel):
    entity: str
    columns: list[dict[str, Any]]


#: JSON columns that are NULL when empty. `model_validate` would pass the None
#: straight through and fail a `list[str]` field, so they are set by hand.
_NULLABLE_LISTS = {"file_errors", "errors", "warnings"}


def _batch_response(batch: Any) -> ImportBatchResponse:
    response = ImportBatchResponse.model_validate(batch.to_dict(exclude=_NULLABLE_LISTS))
    response.file_errors = [str(item) for item in batch.file_errors or []]
    response.is_committable = batch.is_committable
    return response


def _row_response(row: Any) -> ImportRowResponse:
    response = ImportRowResponse.model_validate(row.to_dict(exclude=_NULLABLE_LISTS))
    response.errors = [str(item) for item in row.errors or []]
    response.warnings = [str(item) for item in row.warnings or []]
    return response


# ------------------------------------------------------------ notifications


@notifications_router.get("", response_model=list[NotificationResponse])
async def inbox(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.NOTIFICATION_READ))],
    unread_only: Annotated[bool, Query()] = False,
    category: Annotated[NotificationCategory | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[NotificationResponse]:
    rows = await NotificationService(session).inbox(
        actor, unread_only=unread_only, category=category, limit=limit
    )
    return [NotificationResponse.model_validate(row) for row in rows]


@notifications_router.get("/unread-count", response_model=UnreadCount)
async def unread_count(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.NOTIFICATION_READ))],
) -> UnreadCount:
    return UnreadCount(**await NotificationService(session).unread_count(actor))


@notifications_router.post("/{notification_id}/read", response_model=NotificationResponse)
async def mark_read(
    notification_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.NOTIFICATION_READ))],
) -> NotificationResponse:
    notification = await NotificationService(session).mark_read(notification_id, actor=actor)
    return NotificationResponse.model_validate(notification)


@notifications_router.post("/read-all")
async def mark_all_read(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.NOTIFICATION_READ))],
) -> dict[str, int]:
    return {"marked": await NotificationService(session).mark_all_read(actor)}


@notifications_router.post(
    "/sweep",
    response_model=SweepResult,
    summary="Run every notification sweep now (normally scheduled)",
)
async def run_sweeps(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.USER_CREATE))],
) -> SweepResult:
    return SweepResult(**await NotificationSweeps(session).run_all())


# ----------------------------------------------------------------- imports


@imports_router.get("/entities", response_model=list[ImportEntityInfo])
async def importable_entities(
    _: Annotated[User, Depends(require(Permission.IMPORT_RUN))],
) -> list[ImportEntityInfo]:
    return [
        ImportEntityInfo(
            entity=entity.value,
            columns=[
                {
                    "key": column.key,
                    "label": column.label,
                    "kind": column.kind,
                    "required": column.required,
                    "choices": list(column.choices) if column.choices else None,
                    "hint": column.hint,
                }
                for column in schema_for(entity).columns
            ],
        )
        for entity in SUPPORTED_IMPORTS
    ]


@imports_router.get("/{entity}/template.xlsx", summary="A blank workbook with the right columns")
async def import_template(
    entity: ImportEntity,
    _: Annotated[User, Depends(require(Permission.IMPORT_RUN))],
) -> Response:
    payload = write_template(schema_for(entity))
    return Response(
        content=payload,
        media_type=XLSX_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="glimmora-{entity.value}-template.xlsx"'
        },
    )


@imports_router.post(
    "/{entity}/upload",
    response_model=ImportPreview,
    status_code=status.HTTP_201_CREATED,
    summary="Validate into staging — writes nothing to business tables",
)
async def upload(
    entity: ImportEntity,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.IMPORT_RUN))],
    file: Annotated[UploadFile, File()],
) -> ImportPreview:
    payload = await file.read()
    if len(payload) > MAX_UPLOAD_BYTES:
        from app.core.errors import ValidationError

        raise ValidationError(
            "That file is too large — imports are capped at 10 MB.",
            details=[{"field": "file", "message": "Too large"}],
        )

    service = ImportService(session)
    batch = await service.stage(
        entity=entity, payload=payload, filename=file.filename or "upload.xlsx", actor=actor
    )
    rows = await service.rows(batch.id)
    return ImportPreview(batch=_batch_response(batch), rows=[_row_response(row) for row in rows])


@imports_router.get("", response_model=list[ImportBatchResponse])
async def list_batches(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.IMPORT_RUN))],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ImportBatchResponse]:
    batches = await ImportService(session).list_batches(limit=limit)
    return [_batch_response(batch) for batch in batches]


@imports_router.get("/{batch_id}/preview", response_model=ImportPreview)
async def preview(
    batch_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.IMPORT_RUN))],
    state: Annotated[RowState | None, Query()] = None,
) -> ImportPreview:
    service = ImportService(session)
    batch = await service.get(batch_id)
    rows = await service.rows(batch_id, state=state)
    return ImportPreview(batch=_batch_response(batch), rows=[_row_response(row) for row in rows])


@imports_router.post(
    "/{batch_id}/commit",
    response_model=CommitResult,
    summary="Write the valid rows. Invalid rows are never written.",
)
async def commit(
    batch_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.IMPORT_RUN))],
) -> CommitResult:
    service = ImportService(session)
    batch = await service.get(batch_id)
    return CommitResult(**await service.commit(batch, actor=actor))


@imports_router.post("/{batch_id}/discard", response_model=ImportBatchResponse)
async def discard(
    batch_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.IMPORT_RUN))],
) -> ImportBatchResponse:
    service = ImportService(session)
    batch = await service.get(batch_id)
    return _batch_response(await service.discard(batch, actor=actor))


@imports_router.get(
    "/{batch_id}/errors.xlsx",
    summary="The failed rows, annotated, ready to fix and re-upload",
)
async def error_report(
    batch_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.IMPORT_RUN))],
) -> Response:
    service = ImportService(session)
    batch = await service.get(batch_id)
    rows = await service.rows(batch_id, state=RowState.INVALID, limit=10_000)

    payload = write_error_report(
        schema=schema_for(batch.entity_type),
        failed=[
            {
                "row_number": row.row_number,
                "raw": row.raw,
                "errors": [str(item) for item in row.errors or []],
                "warnings": [str(item) for item in row.warnings or []],
            }
            for row in rows
        ],
    )
    return Response(
        content=payload,
        media_type=XLSX_MEDIA,
        headers={
            "Content-Disposition": (
                f'attachment; filename="glimmora-import-errors-{batch_id}.xlsx"'
            )
        },
    )


# ----------------------------------------------------------------- exports


@exports_router.get("/{entity}.xlsx", summary="Filtered export, respecting field permissions")
async def export(
    entity: ImportEntity,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.EXPORT_RUN))],
) -> Response:
    payload, filename = await ExportService(session).export(entity, actor=actor)
    return Response(
        content=payload,
        media_type=XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


__all__ = ["exports_router", "imports_router", "notifications_router"]
