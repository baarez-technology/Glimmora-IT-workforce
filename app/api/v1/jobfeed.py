"""The individual job feed API.

Two routers. `/auth/register` is public — it is how an individual creates their
own account — and is rate limited accordingly. Everything under `/job-feed`
requires authentication and returns only the caller's own rows.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.deps import SessionDep, require
from app.core.permissions import Permission
from app.core.rate_limit import rate_limit
from app.engines.jobalerts.addressing import address_for
from app.engines.jobsearch.provider import SearchQuery
from app.models.identity import User
from app.models.jobfeed import JobSource, WorkplaceType
from app.services.jobalerts import (
    JobAlertService,
    extract_recipients,
    first_item,
    received_at_of,
    sender_of,
    verify_webhook_secret,
)
from app.services.jobfeed import JobFeedService, serialise
from app.services.jobsearch import JobSearchService
from app.services.user import UserService

router = APIRouter(prefix="/job-feed", tags=["job feed"])
register_router = APIRouter(prefix="/auth", tags=["auth"])
#: Not under /job-feed: nothing here is authenticated as a user, and keeping
#: it separate stops it inheriting the feed router's permission dependency.
webhook_router = APIRouter(prefix="/inbound", tags=["job alerts"])


# ----------------------------------------------------------------- schemas


class RegisterRequest(BaseModel):
    """Self-service signup.

    Deliberately has no `role` field. The role is set to INDIVIDUAL by the
    service; accepting one from an unauthenticated caller would let anyone
    create themselves an administrator.
    """

    email: EmailStr
    full_name: str = Field(min_length=2, max_length=160)
    password: str = Field(min_length=12, max_length=200)


class RegisteredResponse(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: str


class JobFeedItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    received_at: datetime
    is_read: bool
    is_saved: bool

    posting_id: uuid.UUID
    title: str
    company_name: str | None
    location: str | None
    country: str | None
    workplace_type: WorkplaceType
    description: str | None
    url: str | None
    source: JobSource
    source_name: str | None
    posted_at: date | None


class JobFeedPage(BaseModel):
    items: list[JobFeedItemResponse]
    total: int
    limit: int
    offset: int


class JobFeedCounts(BaseModel):
    total: int
    unread: int
    saved: int
    ONSITE: int
    REMOTE: int
    HYBRID: int
    UNKNOWN: int


class ItemStateUpdate(BaseModel):
    is_read: bool | None = None
    is_saved: bool | None = None


class AddJobRequest(BaseModel):
    """Add a job to your own feed by hand.

    Useful in its own right — somebody finds a role elsewhere and wants it in
    one place — and it is the seam the email capture and the search providers
    will deliver through, so they inherit deduplication from day one.
    """

    title: str = Field(min_length=2, max_length=240)
    company_name: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, min_length=2, max_length=2)
    workplace_type: WorkplaceType | None = None
    description: str | None = None
    url: str | None = Field(default=None, max_length=1000)
    posted_at: date | None = None


class SearchRequest(BaseModel):
    """What to look for.

    `titles` rather than one keyword because the provider takes several, and
    searching three titles at once costs the same as searching one.
    """

    titles: list[str] = Field(min_length=1, max_length=5)
    locations: list[str] = Field(default_factory=list, max_length=5)
    workplace_type: WorkplaceType | None = None
    posted_within: str | None = Field(default=None, max_length=16)
    rows: int = Field(default=25, ge=1, le=100)


class SearchStarted(BaseModel):
    search_id: str
    status: str


class SearchResultResponse(BaseModel):
    title: str
    company_name: str | None
    location: str | None
    country: str | None
    workplace_type: WorkplaceType
    description: str | None
    url: str | None
    posted_at: date | None
    external_id: str | None


class SearchRunResponse(BaseModel):
    search_id: str
    status: str
    results: list[SearchResultResponse]
    error: str | None = None


class SearchAvailability(BaseModel):
    available: bool
    provider: str | None


class AlertConnection(BaseModel):
    """What the connect screen needs to be truthful."""

    enabled: bool
    forwarding_address: str
    #: Derived from mail actually received, not from a flag. The screen cannot
    #: claim a working connection that has never delivered anything.
    verified: bool
    count: int
    last_received_at: datetime | None


class PasteAlertRequest(BaseModel):
    """Import an alert by pasting it.

    Real and useful on its own, and it means the whole path can be exercised
    before any DNS exists.
    """

    body: str = Field(min_length=20)
    subject: str | None = Field(default=None, max_length=400)
    sender: str | None = Field(default=None, max_length=320)


class AlertIngestResult(BaseModel):
    recognised: bool
    added: int
    unparsed: int
    message: str | None = None


# ------------------------------------------------------------ registration


@register_router.post(
    "/register",
    response_model=RegisteredResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an individual account",
    dependencies=[Depends(rate_limit("register", limit=10))],
)
async def register(payload: RegisterRequest, session: SessionDep) -> RegisteredResponse:
    user = await UserService(session).register_individual(
        email=payload.email,
        full_name=payload.full_name.strip(),
        password=payload.password,
    )
    return RegisteredResponse(
        id=user.id, email=user.email, full_name=user.full_name, role=user.role.value
    )


# ---------------------------------------------------------------- the feed


@router.get("", response_model=JobFeedPage, summary="Your job feed")
async def list_feed(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.JOB_FEED_READ))],
    workplace_type: Annotated[WorkplaceType | None, Query()] = None,
    unread_only: Annotated[bool, Query()] = False,
    saved_only: Annotated[bool, Query()] = False,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JobFeedPage:
    items, total = await JobFeedService(session).list_items(
        actor=actor,
        workplace_type=workplace_type,
        unread_only=unread_only,
        saved_only=saved_only,
        q=q,
        limit=limit,
        offset=offset,
    )
    return JobFeedPage(
        items=[JobFeedItemResponse.model_validate(serialise(item)) for item in items],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/search/available",
    response_model=SearchAvailability,
    summary="Whether job search is configured",
)
async def search_available(
    _: Annotated[User, Depends(require(Permission.JOB_FEED_READ))],
) -> SearchAvailability:
    """So the client can hide the search screen rather than offer a dead box."""
    service = JobSearchService()
    return SearchAvailability(
        available=service.available,
        provider=service.provider.name if service.provider else None,
    )


@router.post(
    "/search",
    response_model=SearchStarted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a job search",
)
async def start_search(
    payload: SearchRequest,
    _: Annotated[User, Depends(require(Permission.JOB_FEED_READ))],
) -> SearchStarted:
    """Accepted, not completed.

    A provider run takes about thirty seconds. Holding the request open for
    that long would tie up a worker and die at the usual proxy timeout, so the
    caller gets a handle and collects the results afterwards.
    """
    started = await JobSearchService().start(
        SearchQuery(
            titles=payload.titles,
            locations=payload.locations,
            workplace_type=payload.workplace_type,
            posted_within=payload.posted_within,
            rows=payload.rows,
        )
    )
    return SearchStarted(**started)


@router.get(
    "/search/{search_id}",
    response_model=SearchRunResponse,
    summary="Collect a search once it has finished",
)
async def collect_search(
    search_id: str,
    _: Annotated[User, Depends(require(Permission.JOB_FEED_READ))],
) -> SearchRunResponse:
    run = await JobSearchService().collect(search_id)
    return SearchRunResponse(
        search_id=run.id,
        status=run.status.value,
        results=[SearchResultResponse(**vars(item)) for item in run.results],
        error=run.error,
    )


@router.get(
    "/alerts/connection",
    response_model=AlertConnection,
    summary="Your forwarding address and whether anything has arrived",
)
async def alert_connection(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.JOB_FEED_READ))],
) -> AlertConnection:
    state = await JobAlertService(session).connection_state(actor=actor)
    return AlertConnection(
        enabled=state["enabled"],
        forwarding_address=address_for(actor.id),
        verified=state["count"] > 0,
        count=state["count"],
        last_received_at=state["last_received_at"],
    )


@router.post(
    "/alerts/paste",
    response_model=AlertIngestResult,
    summary="Import an alert email by pasting it",
)
async def paste_alert(
    payload: PasteAlertRequest,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.JOB_FEED_WRITE))],
) -> AlertIngestResult:
    """Into your own feed, never anyone else's.

    The actor is the authenticated caller, so a pasted email cannot be
    addressed at somebody else however its headers read.
    """
    looks_like_html = "<" in payload.body and ">" in payload.body
    result = await JobAlertService(session).ingest(
        recipients=None,
        subject=payload.subject,
        sender=payload.sender,
        body_html=payload.body if looks_like_html else None,
        body_text=None if looks_like_html else payload.body,
        actor=actor,
    )
    return AlertIngestResult(**result)


@webhook_router.post(
    "/brevo/{secret}",
    response_model=AlertIngestResult,
    summary="Inbound alert email from Brevo",
)
async def brevo_inbound(
    secret: str,
    payload: dict[str, Any],
    session: SessionDep,
) -> AlertIngestResult:
    """Unauthenticated by necessity, guarded by an unguessable path.

    Brevo posts as itself, with no bearer token and no signature, so the
    secret in the URL is the whole of the authentication. A wrong or missing
    one is a 404 rather than a 403: a 403 would confirm the endpoint exists.

    Whose feed is written to comes from the recipient address in the envelope
    and nothing else -- a sender cannot choose somebody else's feed by
    claiming to be them.
    """
    verify_webhook_secret(secret)

    item = first_item(payload)
    result = await JobAlertService(session).ingest(
        recipients=extract_recipients(item),
        subject=item.get("Subject"),
        sender=sender_of(item),
        body_html=item.get("RawHtmlBody"),
        body_text=item.get("RawTextBody") or item.get("ExtractedMarkdownMessage"),
        received_at=received_at_of(item),
    )
    return AlertIngestResult(**result)


@router.get("/counts", response_model=JobFeedCounts, summary="Counts for the filter chips")
async def feed_counts(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.JOB_FEED_READ))],
) -> JobFeedCounts:
    return JobFeedCounts(**await JobFeedService(session).counts(actor=actor))


@router.get("/{item_id}", response_model=JobFeedItemResponse, summary="One item from your feed")
async def get_item(
    item_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.JOB_FEED_READ))],
) -> JobFeedItemResponse:
    item = await JobFeedService(session).get_item(item_id, actor=actor)
    return JobFeedItemResponse.model_validate(serialise(item))


@router.patch("/{item_id}", response_model=JobFeedItemResponse, summary="Mark read, save or unsave")
async def update_item(
    item_id: uuid.UUID,
    payload: ItemStateUpdate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.JOB_FEED_WRITE))],
) -> JobFeedItemResponse:
    item = await JobFeedService(session).set_state(
        item_id, actor=actor, is_read=payload.is_read, is_saved=payload.is_saved
    )
    return JobFeedItemResponse.model_validate(serialise(item))


@router.post("/read-all", summary="Mark everything in your feed as read")
async def mark_all_read(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.JOB_FEED_WRITE))],
) -> dict[str, int]:
    return {"marked": await JobFeedService(session).mark_all_read(actor=actor)}


@router.post(
    "/jobs",
    response_model=JobFeedItemResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a job to your own feed",
)
async def add_job(
    payload: AddJobRequest,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.JOB_FEED_WRITE))],
) -> JobFeedItemResponse:
    item = await JobFeedService(session).deliver(
        actor=actor,
        title=payload.title,
        company_name=payload.company_name,
        location=payload.location,
        country=payload.country,
        workplace_type=payload.workplace_type,
        description=payload.description,
        url=payload.url,
        posted_at=payload.posted_at,
        source=JobSource.MANUAL,
    )
    await session.refresh(item, ["posting"])
    return JobFeedItemResponse.model_validate(serialise(item))
