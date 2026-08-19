"""Resource, CV-parsing and document endpoints.

The download endpoint is the one place in the platform that hands out personal
data as a file, so it authenticates, authorises, audits, then streams — in that
order, every time (SECURITY.md section 6).
"""

from __future__ import annotations

import io
import uuid
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.core.deps import ActiveUser, SessionDep, client_ip, require, user_agent
from app.core.pagination import Page, PageParams, page_params
from app.core.permissions import Permission, permissions_for
from app.db.types import utcnow
from app.models.accounts import Account
from app.models.identity import AuditAction, User
from app.models.talent import (
    AvailabilityStatus,
    DocumentType,
    Resource,
    ResourceDocument,
    ResourceType,
    VisaStatus,
)
from app.repositories.talent import DocumentRepository
from app.schemas.talent import (
    AcceptCVRequest,
    CVParseResponse,
    DocumentExpiryInfo,
    DuplicateMatch,
    ExpiringDocumentsSummary,
    ResourceCertificationResponse,
    ResourceCreate,
    ResourceDocumentCreate,
    ResourceDocumentResponse,
    ResourceDocumentUpdate,
    ResourceExperienceResponse,
    ResourceResponse,
    ResourceSkillResponse,
    ResourceUpdate,
)
from app.services.audit import AuditService
from app.services.documents import (
    blocks_deployment,
    describe_document,
    expiry_status,
    work_authorisation_state,
)
from app.services.resources import ResourceService
from app.storage.service import read_object

router = APIRouter(prefix="/resources", tags=["resources"])
documents_router = APIRouter(prefix="/documents", tags=["documents"])


# --------------------------------------------------------------- serializers


async def _name_map(session: Any, model: Any, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Batched id -> display name, so a page never triggers N+1.

    Models label themselves differently: people have `full_name`, organisations
    have `name`.
    """
    ids = {value for value in ids if value}
    if not ids:
        return {}
    label = getattr(model, "full_name", None) or model.name
    rows = await session.execute(select(model.id, label).where(model.id.in_(ids)))
    return {row[0]: row[1] for row in rows}


def _expiry_info(expiry: date | None, *, today: date) -> DocumentExpiryInfo:
    status_value = expiry_status(expiry, today=today)
    return DocumentExpiryInfo(
        state=status_value.state,
        expiry_date=status_value.expiry_date,
        days_remaining=status_value.days_remaining,
        is_expired=status_value.is_expired,
        label=status_value.label,
    )


def _document_response(
    link: ResourceDocument,
    *,
    today: date,
    can_view_personal: bool,
    can_download: bool,
    resource_name: str | None = None,
) -> ResourceDocumentResponse:
    file_record = link.document
    return ResourceDocumentResponse(
        id=link.id,
        resource_id=link.resource_id,
        resource_name=resource_name,
        document_id=link.document_id,
        doc_type=link.doc_type,
        doc_type_label=describe_document(link.doc_type),
        title=link.title,
        original_filename=file_record.original_filename,
        content_type=file_record.content_type,
        size_bytes=file_record.size_bytes,
        issue_date=link.issue_date,
        # A passport number is personal data; the key is absent, not blanked.
        reference_number=link.reference_number if can_view_personal else None,
        issuing_country=link.issuing_country,
        notes=link.notes,
        expiry=_expiry_info(link.expiry_date, today=today),
        is_work_authorisation=link.is_work_authorisation,
        can_download=can_download or not link.is_personal,
        created_at=link.created_at,
    )


async def _serialize(
    session: Any, resources: list[Resource], *, actor: User
) -> list[ResourceResponse]:
    if not resources:
        return []

    granted = permissions_for(actor.role)
    can_see_cost = Permission.FIELD_RESOURCE_COST in granted
    can_see_billing = Permission.FIELD_BILLING_RATE in granted
    today = utcnow().date()

    owner_names = await _name_map(session, User, {r.owner_id for r in resources if r.owner_id})
    partner_names = await _name_map(
        session, Account, {r.partner_account_id for r in resources if r.partner_account_id}
    )

    items: list[ResourceResponse] = []
    for resource in resources:
        response = ResourceResponse.model_validate(resource.to_dict())
        response.owner_name = owner_names.get(resource.owner_id) if resource.owner_id else None
        response.partner_account_name = (
            partner_names.get(resource.partner_account_id) if resource.partner_account_id else None
        )
        response.needs_review = resource.is_awaiting_review
        response.is_bench = resource.is_bench
        response.ready_from = ResourceService.ready_from(resource, today=today)

        authorisation = work_authorisation_state(list(resource.documents), today=today)
        response.work_authorisation = DocumentExpiryInfo(
            state=authorisation.state,
            expiry_date=authorisation.expiry_date,
            days_remaining=authorisation.days_remaining,
            is_expired=authorisation.is_expired,
            label=authorisation.label,
        )
        response.blocks_deployment = blocks_deployment(list(resource.documents), today=today)
        response.document_count = len(resource.documents)

        response.skills = [
            ResourceSkillResponse(
                id=link.id,
                skill_id=link.skill_id,
                name=link.skill.name,
                category=link.skill.category,
                years=link.years,
                proficiency=link.proficiency,
                last_used_year=link.last_used_year,
                is_primary=link.is_primary,
            )
            for link in resource.skills
        ]
        response.experience = [
            ResourceExperienceResponse.model_validate(entry) for entry in resource.experience
        ]
        response.certifications = [
            ResourceCertificationResponse.model_validate(entry).model_copy(
                update={"expiry_state": expiry_status(entry.expires_at, today=today).state}
            )
            for entry in resource.certifications
        ]

        # Field-level redaction: the key is removed, not nulled (AD-6).
        if not can_see_cost:
            response.expected_cost_amount = None
            response.expected_cost_currency = None
            response.expected_cost_unit = None
        if not can_see_billing:
            response.target_billing_amount = None
            response.target_billing_currency = None
            response.target_billing_unit = None

        items.append(response)
    return items


async def _serialize_one(session: Any, resource: Resource, *, actor: User) -> ResourceResponse:
    return (await _serialize(session, [resource], actor=actor))[0]


def _redact_cost(response: ResourceResponse, actor: User) -> dict[str, Any]:
    """Drop restricted keys entirely rather than returning them as null."""
    granted = permissions_for(actor.role)
    payload = response.model_dump()
    if Permission.FIELD_RESOURCE_COST not in granted:
        for key in ("expected_cost_amount", "expected_cost_currency", "expected_cost_unit"):
            payload.pop(key, None)
    if Permission.FIELD_BILLING_RATE not in granted:
        for key in ("target_billing_amount", "target_billing_currency", "target_billing_unit"):
            payload.pop(key, None)
    return payload


# ---------------------------------------------------------------- resources


@router.get("", response_model=Page[ResourceResponse], summary="List resources")
async def list_resources(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.RESOURCE_READ))],
    params: Annotated[PageParams, Depends(page_params)],
    resource_type: Annotated[ResourceType | None, Query()] = None,
    availability_status: Annotated[AvailabilityStatus | None, Query()] = None,
    visa_status: Annotated[VisaStatus | None, Query()] = None,
    country: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    skill_id: Annotated[uuid.UUID | None, Query()] = None,
    review_status: Annotated[str | None, Query(max_length=24)] = None,
    max_notice_days: Annotated[int | None, Query(ge=0, le=365)] = None,
    bench_only: Annotated[bool, Query()] = False,
) -> Page[ResourceResponse]:
    resources, total = await ResourceService(session).list_resources(
        params,
        resource_type=resource_type,
        availability_status=availability_status,
        visa_status=visa_status,
        country=country,
        skill_id=skill_id,
        review_status=review_status,
        max_notice_days=max_notice_days,
        bench_only=bench_only,
    )
    return Page.build(await _serialize(session, resources, actor=actor), total, params)


@router.get("/available", response_model=Page[ResourceResponse], summary="Available now or soon")
async def list_available(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.RESOURCE_READ))],
    params: Annotated[PageParams, Depends(page_params)],
    within_days: Annotated[int, Query(ge=0, le=365)] = 30,
) -> Page[ResourceResponse]:
    resources, total = await ResourceService(session).list_resources(
        params,
        available_by=utcnow().date() + timedelta(days=within_days),
        max_notice_days=within_days,
    )
    return Page.build(await _serialize(session, resources, actor=actor), total, params)


@router.get("/bench", response_model=Page[ResourceResponse], summary="Unbilled capacity")
async def list_bench(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.RESOURCE_READ))],
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[ResourceResponse]:
    """The number the zero-bench engine exists to drive to zero (Phase 8)."""
    resources, total = await ResourceService(session).list_resources(params, bench_only=True)
    return Page.build(await _serialize(session, resources, actor=actor), total, params)


@router.get(
    "/check-duplicate",
    response_model=list[DuplicateMatch],
    summary="Pre-flight duplicate check",
)
async def check_duplicate(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.RESOURCE_READ))],
    email: Annotated[str | None, Query(max_length=255)] = None,
    phone: Annotated[str | None, Query(max_length=48)] = None,
    full_name: Annotated[str | None, Query(max_length=160)] = None,
) -> list[DuplicateMatch]:
    return await ResourceService(session).find_duplicates(
        email=email, phone=phone, full_name=full_name
    )


@router.post(
    "",
    response_model=ResourceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a resource",
)
async def create_resource(
    payload: ResourceCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.RESOURCE_CREATE))],
) -> ResourceResponse:
    resource = await ResourceService(session).create_resource(payload, actor=actor)
    return await _serialize_one(session, resource, actor=actor)


@router.get("/{resource_id}", response_model=ResourceResponse, summary="Get a resource")
async def get_resource(
    resource_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.RESOURCE_READ))],
) -> ResourceResponse:
    resource = await ResourceService(session).get_resource(resource_id)
    return await _serialize_one(session, resource, actor=actor)


@router.patch("/{resource_id}", response_model=ResourceResponse, summary="Update a resource")
async def update_resource(
    resource_id: uuid.UUID,
    payload: ResourceUpdate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.RESOURCE_UPDATE))],
) -> ResourceResponse:
    resource = await ResourceService(session).update_resource(resource_id, payload, actor=actor)
    return await _serialize_one(session, resource, actor=actor)


@router.delete(
    "/{resource_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Archive a resource"
)
async def archive_resource(
    resource_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.RESOURCE_DELETE))],
) -> None:
    await ResourceService(session).archive_resource(resource_id, actor=actor)


# ---------------------------------------------------------------- CV parse


@router.post(
    "/parse-cv",
    response_model=CVParseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Parse an uploaded CV",
)
async def parse_cv(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.CV_PARSE))],
    file: Annotated[UploadFile, File(description="PDF, Word or text CV")],
) -> CVParseResponse:
    content = await file.read()
    service = ResourceService(session)
    resource, duplicates = await service.parse_cv(
        filename=file.filename or "cv.pdf", content=content, actor=actor
    )

    payload = resource.parsed_payload or {}
    fields = service.cv_fields_for_review(resource)

    return CVParseResponse(
        resource_id=resource.id,
        source_text="",  # the CV text is not echoed back; the file is downloadable
        provider=str(payload.get("provider", "unknown")),
        model_id=str(payload.get("model_id", "unknown")),
        used_fallback=bool(payload.get("used_fallback", False)),
        overall_confidence=float(payload.get("overall_confidence") or 0.0),
        fields=fields,
        warnings=list(payload.get("warnings") or []),
        confirmation_required=[f.field for f in fields if f.requires_confirmation],
        duplicates=duplicates,
    )


@router.get(
    "/{resource_id}/parse-result",
    response_model=CVParseResponse,
    summary="Extracted CV fields and confidence",
)
async def cv_parse_result(
    resource_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.RESOURCE_READ))],
) -> CVParseResponse:
    service = ResourceService(session)
    resource = await service.get_resource(resource_id)
    payload = resource.parsed_payload or {}
    fields = service.cv_fields_for_review(resource)

    duplicates: list[DuplicateMatch] = []
    if resource.is_awaiting_review:
        duplicates = [
            match
            for match in await service.find_duplicates(
                email=resource.email, phone=resource.phone, full_name=resource.full_name
            )
            if match.resource_id != resource.id
        ]

    return CVParseResponse(
        resource_id=resource.id,
        source_text="",
        provider=str(payload.get("provider", "unknown")),
        model_id=str(payload.get("model_id", "unknown")),
        used_fallback=bool(payload.get("used_fallback", False)),
        overall_confidence=float(payload.get("overall_confidence") or 0.0),
        fields=fields,
        warnings=list(payload.get("warnings") or []),
        confirmation_required=[f.field for f in fields if f.requires_confirmation],
        duplicates=duplicates,
    )


@router.post(
    "/{resource_id}/accept-parse",
    response_model=ResourceResponse,
    summary="Accept the reviewed CV fields",
)
async def accept_cv(
    resource_id: uuid.UUID,
    payload: AcceptCVRequest,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.RESOURCE_UPDATE))],
) -> ResourceResponse:
    resource = await ResourceService(session).accept_cv(resource_id, payload, actor=actor)
    return await _serialize_one(session, resource, actor=actor)


# ---------------------------------------------------------------- documents


@router.get(
    "/{resource_id}/documents",
    response_model=list[ResourceDocumentResponse],
    summary="Documents held for a resource",
)
async def list_documents(
    resource_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.DOCUMENT_READ))],
) -> list[ResourceDocumentResponse]:
    service = ResourceService(session)
    resource = await service.get_resource(resource_id)
    links = await service.documents.for_resource(resource_id)

    granted = permissions_for(actor.role)
    today = utcnow().date()
    return [
        _document_response(
            link,
            today=today,
            can_view_personal=Permission.FIELD_DOCUMENT_PERSONAL_VIEW in granted,
            can_download=Permission.FIELD_DOCUMENT_PERSONAL_DOWNLOAD in granted,
            resource_name=resource.full_name,
        )
        for link in links
    ]


@router.post(
    "/{resource_id}/documents",
    response_model=ResourceDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document",
)
async def upload_document(
    resource_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.DOCUMENT_WRITE))],
    file: Annotated[UploadFile, File()],
    doc_type: Annotated[DocumentType, Form()],
    title: Annotated[str | None, Form()] = None,
    issue_date: Annotated[date | None, Form()] = None,
    expiry_date: Annotated[date | None, Form()] = None,
    issuing_country: Annotated[str | None, Form()] = None,
    reference_number: Annotated[str | None, Form()] = None,
) -> ResourceDocumentResponse:
    content = await file.read()
    service = ResourceService(session)
    resource = await service.get_resource(resource_id)

    link = await service.add_document(
        resource_id,
        ResourceDocumentCreate(
            doc_type=doc_type,
            title=title,
            issue_date=issue_date,
            expiry_date=expiry_date,
            issuing_country=issuing_country,
            reference_number=reference_number,
        ),
        filename=file.filename or "document",
        content=content,
        actor=actor,
    )
    await session.flush()

    granted = permissions_for(actor.role)
    return _document_response(
        link,
        today=utcnow().date(),
        can_view_personal=Permission.FIELD_DOCUMENT_PERSONAL_VIEW in granted,
        can_download=Permission.FIELD_DOCUMENT_PERSONAL_DOWNLOAD in granted,
        resource_name=resource.full_name,
    )


@documents_router.get(
    "/expiring",
    response_model=ExpiringDocumentsSummary,
    summary="Documents expired or expiring soon",
)
async def expiring_documents(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.DOCUMENT_READ))],
    days_ahead: Annotated[int, Query(ge=1, le=365)] = 60,
    work_authorisation_only: Annotated[bool, Query()] = False,
) -> ExpiringDocumentsSummary:
    """An expired work permit stops billing, so this is a revenue screen."""
    today = utcnow().date()
    links = await DocumentRepository(session).expiring(
        before=today + timedelta(days=days_ahead),
        work_authorisation_only=work_authorisation_only,
    )

    resource_names = await _name_map(session, Resource, {link.resource_id for link in links})
    granted = permissions_for(actor.role)

    expired: list[ResourceDocumentResponse] = []
    soon: list[ResourceDocumentResponse] = []
    for link in links:
        response = _document_response(
            link,
            today=today,
            can_view_personal=Permission.FIELD_DOCUMENT_PERSONAL_VIEW in granted,
            can_download=Permission.FIELD_DOCUMENT_PERSONAL_DOWNLOAD in granted,
            resource_name=resource_names.get(link.resource_id),
        )
        (expired if response.expiry.is_expired else soon).append(response)

    return ExpiringDocumentsSummary(
        expired=expired,
        expiring_soon=soon,
        counts={"expired": len(expired), "expiring_soon": len(soon)},
    )


@documents_router.patch(
    "/{document_id}", response_model=ResourceDocumentResponse, summary="Update document details"
)
async def update_document(
    document_id: uuid.UUID,
    payload: ResourceDocumentUpdate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.DOCUMENT_WRITE))],
) -> ResourceDocumentResponse:
    link = await ResourceService(session).update_document(document_id, payload, actor=actor)
    granted = permissions_for(actor.role)
    return _document_response(
        link,
        today=utcnow().date(),
        can_view_personal=Permission.FIELD_DOCUMENT_PERSONAL_VIEW in granted,
        can_download=Permission.FIELD_DOCUMENT_PERSONAL_DOWNLOAD in granted,
    )


@documents_router.delete(
    "/{document_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a document"
)
async def delete_document(
    document_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.DOCUMENT_WRITE))],
) -> None:
    await ResourceService(session).delete_document(document_id, actor=actor)


@documents_router.get("/{document_id}/download", summary="Download a document")
async def download_document(
    document_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    actor: ActiveUser,
) -> Response:
    """Authenticate, authorise, audit, then stream — in that order.

    For a passport or a visa, knowing who took a copy is the whole point of the
    control, so the audit entry is written before the bytes leave.
    """
    from app.core.errors import ForbiddenError, NotFoundError

    granted = permissions_for(actor.role)
    if Permission.DOCUMENT_READ not in granted:
        raise ForbiddenError()

    service = ResourceService(session)
    link = await service.documents.get(document_id)
    if link is None:
        raise NotFoundError("document", document_id)

    if link.is_personal and Permission.FIELD_DOCUMENT_PERSONAL_DOWNLOAD not in granted:
        raise ForbiddenError(
            "You can see that this document exists, but not download it.",
            log_detail=f"role={actor.role.value} blocked from {link.doc_type.value} download",
        )

    file_record = await service.documents.get_file(link.document_id)
    if file_record is None:
        raise NotFoundError("document", document_id)

    await AuditService(session).record(
        AuditAction.DOCUMENT_DOWNLOADED,
        summary=f"Downloaded {link.doc_type.value} ({file_record.original_filename})",
        actor=actor,
        entity_type="resource_document",
        entity_id=link.id,
        ip_address=client_ip(request),
        user_agent=user_agent(request),
    )
    await session.commit()

    content = read_object(file_record.storage_key)
    return StreamingResponse(
        io.BytesIO(content),
        media_type=file_record.content_type,
        headers={
            # Always an attachment, never rendered in place.
            "Content-Disposition": f'attachment; filename="{file_record.original_filename}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


__all__ = ["documents_router", "router"]
