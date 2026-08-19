"""Account, routing, contact, project and activity endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select

from app.core.deps import SessionDep, require
from app.core.pagination import Page, PageParams, page_params
from app.core.permissions import Permission
from app.db.types import utcnow
from app.models.accounts import (
    Account,
    AccountRelationship,
    AccountType,
    Activity,
    ActivityType,
    Contact,
    Project,
    ProjectStatus,
    RelationshipStatus,
)
from app.models.identity import User
from app.repositories.accounts import AccountRepository, TechnologyRepository
from app.schemas.accounts import (
    AccountCreate,
    AccountResponse,
    AccountRouteCreate,
    AccountRouteResponse,
    AccountUpdate,
    ActivityCreate,
    ActivityResponse,
    ActivityUpdate,
    ContactCreate,
    ContactResponse,
    ContactUpdate,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    TechnologyResponse,
)
from app.services.accounts import (
    AccountService,
    ActivityService,
    ContactService,
    ProjectService,
    is_follow_up_overdue,
)

accounts_router = APIRouter(prefix="/accounts", tags=["accounts"])
contacts_router = APIRouter(prefix="/contacts", tags=["contacts"])
projects_router = APIRouter(prefix="/projects", tags=["projects"])
activities_router = APIRouter(prefix="/activities", tags=["activities"])
technologies_router = APIRouter(prefix="/technologies", tags=["technologies"])


# --------------------------------------------------------------- serializers
# Names are resolved in one batched query per page rather than per row, so a
# list screen never triggers N+1.


async def _name_map(session: Any, model: Any, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    ids = {value for value in ids if value}
    if not ids:
        return {}
    label = model.full_name if model is User else model.name
    rows = await session.execute(select(model.id, label).where(model.id.in_(ids)))
    return {row[0]: row[1] for row in rows}


async def _account_response(
    session: Any, account: Account, *, addressability: bool = False
) -> AccountResponse:
    response = AccountResponse.model_validate(account)
    owners = await _name_map(session, User, {account.owner_id} if account.owner_id else set())
    response.owner_name = owners.get(account.owner_id) if account.owner_id else None

    counts = await AccountRepository(session).counts_for([account.id])
    stats = counts.get(account.id, {})
    response.contact_count = stats.get("contacts", 0)
    response.decision_maker_count = stats.get("decision_makers", 0)
    response.project_count = stats.get("projects", 0)
    response.route_count = stats.get("routes", 0)

    if addressability:
        response.addressability = await AccountService(session).addressability_signals(account)
    return response


async def _account_page(session: Any, accounts: list[Account]) -> list[AccountResponse]:
    if not accounts:
        return []
    owner_names = await _name_map(session, User, {a.owner_id for a in accounts if a.owner_id})
    counts = await AccountRepository(session).counts_for([a.id for a in accounts])

    items: list[AccountResponse] = []
    for account in accounts:
        response = AccountResponse.model_validate(account)
        response.owner_name = owner_names.get(account.owner_id) if account.owner_id else None
        stats = counts.get(account.id, {})
        response.contact_count = stats.get("contacts", 0)
        response.decision_maker_count = stats.get("decision_makers", 0)
        response.project_count = stats.get("projects", 0)
        response.route_count = stats.get("routes", 0)
        items.append(response)
    return items


def _route_response(route: AccountRelationship) -> AccountRouteResponse:
    response = AccountRouteResponse.model_validate(route)
    target = route.to_account
    if target is not None:
        response.to_account_name = target.name
        response.to_account_type = target.account_type
    return response


async def _contact_page(session: Any, contacts: list[Contact]) -> list[ContactResponse]:
    account_names = await _name_map(session, Account, {c.account_id for c in contacts})
    items = []
    for contact in contacts:
        response = ContactResponse.model_validate(contact)
        response.account_name = account_names.get(contact.account_id)
        items.append(response)
    return items


async def _project_page(session: Any, projects: list[Project]) -> list[ProjectResponse]:
    account_ids = {p.account_id for p in projects} | {
        p.prime_contractor_id for p in projects if p.prime_contractor_id
    }
    account_names = await _name_map(session, Account, account_ids)

    items = []
    for project in projects:
        # Validated from columns only: the ORM's `technologies` attribute holds
        # ProjectTechnology link rows, not the Technology records the response
        # declares, so letting Pydantic read it would fail validation.
        response = ProjectResponse.model_validate(project.to_dict())
        response.account_name = account_names.get(project.account_id)
        response.prime_contractor_name = (
            account_names.get(project.prime_contractor_id) if project.prime_contractor_id else None
        )
        response.technologies = [
            TechnologyResponse.model_validate(link.technology) for link in project.technologies
        ]
        items.append(response)
    return items


async def _activity_page(session: Any, activities: list[Activity]) -> list[ActivityResponse]:
    now = utcnow()
    user_names = await _name_map(session, User, {a.user_id for a in activities if a.user_id})
    account_names = await _name_map(
        session, Account, {a.account_id for a in activities if a.account_id}
    )
    project_names = await _name_map(
        session, Project, {a.project_id for a in activities if a.project_id}
    )
    contact_ids = {a.contact_id for a in activities if a.contact_id}
    contact_names: dict[uuid.UUID, str] = {}
    if contact_ids:
        rows = await session.execute(
            select(Contact.id, Contact.full_name).where(Contact.id.in_(contact_ids))
        )
        contact_names = {row[0]: row[1] for row in rows}

    items = []
    for activity in activities:
        response = ActivityResponse.model_validate(activity)
        response.is_follow_up_open = activity.is_follow_up_open
        response.is_follow_up_overdue = is_follow_up_overdue(activity, now=now)
        response.user_name = user_names.get(activity.user_id) if activity.user_id else None
        response.account_name = (
            account_names.get(activity.account_id) if activity.account_id else None
        )
        response.contact_name = (
            contact_names.get(activity.contact_id) if activity.contact_id else None
        )
        response.project_name = (
            project_names.get(activity.project_id) if activity.project_id else None
        )
        items.append(response)
    return items


# ------------------------------------------------------------------ accounts


@accounts_router.get("", response_model=Page[AccountResponse], summary="List accounts")
async def list_accounts(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.ACCOUNT_READ))],
    params: Annotated[PageParams, Depends(page_params)],
    account_type: Annotated[AccountType | None, Query()] = None,
    relationship_status: Annotated[RelationshipStatus | None, Query()] = None,
    owner_id: Annotated[uuid.UUID | None, Query()] = None,
    country: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    is_existing_customer: Annotated[bool | None, Query()] = None,
    is_approved_vendor: Annotated[bool | None, Query()] = None,
) -> Page[AccountResponse]:
    accounts, total = await AccountService(session).list_accounts(
        params,
        account_type=account_type,
        relationship_status=relationship_status,
        owner_id=owner_id,
        country=country,
        is_existing_customer=is_existing_customer,
        is_approved_vendor=is_approved_vendor,
    )
    return Page.build(await _account_page(session, accounts), total, params)


@accounts_router.post(
    "",
    response_model=AccountResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create account",
)
async def create_account(
    payload: AccountCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.ACCOUNT_CREATE))],
) -> AccountResponse:
    account = await AccountService(session).create_account(payload, actor=actor)
    return await _account_response(session, account, addressability=True)


@accounts_router.get("/{account_id}", response_model=AccountResponse, summary="Get account")
async def get_account(
    account_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.ACCOUNT_READ))],
) -> AccountResponse:
    account = await AccountService(session).get_account(account_id)
    return await _account_response(session, account, addressability=True)


@accounts_router.patch("/{account_id}", response_model=AccountResponse, summary="Update account")
async def update_account(
    account_id: uuid.UUID,
    payload: AccountUpdate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.ACCOUNT_UPDATE))],
) -> AccountResponse:
    account = await AccountService(session).update_account(account_id, payload, actor=actor)
    return await _account_response(session, account, addressability=True)


@accounts_router.delete(
    "/{account_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Archive account"
)
async def archive_account(
    account_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.ACCOUNT_DELETE))],
) -> None:
    await AccountService(session).archive_account(account_id, actor=actor)


# -------------------------------------------------------------------- routes


@accounts_router.get(
    "/{account_id}/routes",
    response_model=list[AccountRouteResponse],
    summary="Routes into this account",
)
async def list_routes(
    account_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.ACCOUNT_READ))],
) -> list[AccountRouteResponse]:
    routes = await AccountService(session).list_routes(account_id)
    return [_route_response(route) for route in routes]


@accounts_router.post(
    "/{account_id}/routes",
    response_model=AccountRouteResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a route",
)
async def add_route(
    account_id: uuid.UUID,
    payload: AccountRouteCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.ACCOUNT_UPDATE))],
) -> AccountRouteResponse:
    route = await AccountService(session).add_route(account_id, payload, actor=actor)
    await session.flush()
    routes = await AccountService(session).list_routes(account_id)
    return next(_route_response(r) for r in routes if r.id == route.id)


@accounts_router.delete(
    "/{account_id}/routes/{route_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a route",
)
async def remove_route(
    account_id: uuid.UUID,
    route_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.ACCOUNT_UPDATE))],
) -> None:
    await AccountService(session).remove_route(account_id, route_id, actor=actor)


@accounts_router.get(
    "/{account_id}/timeline",
    response_model=Page[ActivityResponse],
    summary="Account activity timeline",
)
async def account_timeline(
    account_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.ACTIVITY_READ))],
    params: Annotated[PageParams, Depends(page_params)],
) -> Page[ActivityResponse]:
    await AccountService(session).get_account(account_id)
    activities, total = await ActivityService(session).list_activities(
        params, account_id=account_id
    )
    return Page.build(await _activity_page(session, activities), total, params)


# ------------------------------------------------------------------ contacts


@contacts_router.get("", response_model=Page[ContactResponse], summary="List contacts")
async def list_contacts(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.CONTACT_READ))],
    params: Annotated[PageParams, Depends(page_params)],
    account_id: Annotated[uuid.UUID | None, Query()] = None,
    is_decision_maker: Annotated[bool | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> Page[ContactResponse]:
    contacts, total = await ContactService(session).list_contacts(
        params, account_id=account_id, is_decision_maker=is_decision_maker, is_active=is_active
    )
    return Page.build(await _contact_page(session, contacts), total, params)


@contacts_router.post(
    "",
    response_model=ContactResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create contact",
)
async def create_contact(
    payload: ContactCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.CONTACT_WRITE))],
) -> ContactResponse:
    contact = await ContactService(session).create_contact(payload, actor=actor)
    return (await _contact_page(session, [contact]))[0]


@contacts_router.get("/{contact_id}", response_model=ContactResponse, summary="Get contact")
async def get_contact(
    contact_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.CONTACT_READ))],
) -> ContactResponse:
    contact = await ContactService(session).get_contact(contact_id)
    return (await _contact_page(session, [contact]))[0]


@contacts_router.patch("/{contact_id}", response_model=ContactResponse, summary="Update contact")
async def update_contact(
    contact_id: uuid.UUID,
    payload: ContactUpdate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.CONTACT_WRITE))],
) -> ContactResponse:
    contact = await ContactService(session).update_contact(contact_id, payload, actor=actor)
    return (await _contact_page(session, [contact]))[0]


@contacts_router.delete(
    "/{contact_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete contact"
)
async def delete_contact(
    contact_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.CONTACT_WRITE))],
) -> None:
    await ContactService(session).delete_contact(contact_id, actor=actor)


# ------------------------------------------------------------------ projects


@projects_router.get("", response_model=Page[ProjectResponse], summary="List projects")
async def list_projects(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.PROJECT_READ))],
    params: Annotated[PageParams, Depends(page_params)],
    account_id: Annotated[uuid.UUID | None, Query()] = None,
    project_status: Annotated[ProjectStatus | None, Query(alias="status")] = None,
    technology_id: Annotated[uuid.UUID | None, Query()] = None,
    ending_before: Annotated[datetime | None, Query()] = None,
) -> Page[ProjectResponse]:
    projects, total = await ProjectService(session).list_projects(
        params,
        account_id=account_id,
        status=project_status,
        technology_id=technology_id,
        ending_before=ending_before,
    )
    return Page.build(await _project_page(session, projects), total, params)


@projects_router.post(
    "",
    response_model=ProjectResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create project",
)
async def create_project(
    payload: ProjectCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.PROJECT_WRITE))],
) -> ProjectResponse:
    project = await ProjectService(session).create_project(payload, actor=actor)
    return (await _project_page(session, [project]))[0]


@projects_router.get("/{project_id}", response_model=ProjectResponse, summary="Get project")
async def get_project(
    project_id: uuid.UUID,
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.PROJECT_READ))],
) -> ProjectResponse:
    project = await ProjectService(session).get_project(project_id)
    return (await _project_page(session, [project]))[0]


@projects_router.patch("/{project_id}", response_model=ProjectResponse, summary="Update project")
async def update_project(
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.PROJECT_WRITE))],
) -> ProjectResponse:
    project = await ProjectService(session).update_project(project_id, payload, actor=actor)
    return (await _project_page(session, [project]))[0]


@projects_router.delete(
    "/{project_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Archive project"
)
async def archive_project(
    project_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.PROJECT_WRITE))],
) -> None:
    await ProjectService(session).archive_project(project_id, actor=actor)


# -------------------------------------------------------------- technologies


@technologies_router.get(
    "", response_model=list[TechnologyResponse], summary="Technology master list"
)
async def list_technologies(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.PROJECT_READ))],
) -> list[TechnologyResponse]:
    technologies = await TechnologyRepository(session).list_all()
    return [TechnologyResponse.model_validate(technology) for technology in technologies]


# ---------------------------------------------------------------- activities


@activities_router.get("", response_model=Page[ActivityResponse], summary="List activities")
async def list_activities(
    session: SessionDep,
    _: Annotated[User, Depends(require(Permission.ACTIVITY_READ))],
    params: Annotated[PageParams, Depends(page_params)],
    account_id: Annotated[uuid.UUID | None, Query()] = None,
    contact_id: Annotated[uuid.UUID | None, Query()] = None,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
    activity_type: Annotated[ActivityType | None, Query()] = None,
    user_id: Annotated[uuid.UUID | None, Query()] = None,
) -> Page[ActivityResponse]:
    activities, total = await ActivityService(session).list_activities(
        params,
        account_id=account_id,
        contact_id=contact_id,
        project_id=project_id,
        activity_type=activity_type,
        user_id=user_id,
    )
    return Page.build(await _activity_page(session, activities), total, params)


@activities_router.get(
    "/follow-ups", response_model=Page[ActivityResponse], summary="Open and overdue follow-ups"
)
async def list_follow_ups(
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.ACTIVITY_READ))],
    params: Annotated[PageParams, Depends(page_params)],
    mine_only: Annotated[bool, Query()] = True,
    overdue_only: Annotated[bool, Query()] = False,
) -> Page[ActivityResponse]:
    activities, total = await ActivityService(session).list_activities(
        params,
        user_id=actor.id if mine_only else None,
        open_follow_ups_only=True,
        overdue_only=overdue_only,
        now=utcnow(),
    )
    return Page.build(await _activity_page(session, activities), total, params)


@activities_router.post(
    "",
    response_model=ActivityResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Log an activity",
)
async def create_activity(
    payload: ActivityCreate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.ACTIVITY_WRITE))],
) -> ActivityResponse:
    activity = await ActivityService(session).create_activity(payload, actor=actor)
    return (await _activity_page(session, [activity]))[0]


@activities_router.patch(
    "/{activity_id}", response_model=ActivityResponse, summary="Update an activity"
)
async def update_activity(
    activity_id: uuid.UUID,
    payload: ActivityUpdate,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.ACTIVITY_WRITE))],
) -> ActivityResponse:
    activity = await ActivityService(session).update_activity(activity_id, payload, actor=actor)
    return (await _activity_page(session, [activity]))[0]


@activities_router.post(
    "/{activity_id}/complete", response_model=ActivityResponse, summary="Complete a follow-up"
)
async def complete_follow_up(
    activity_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.ACTIVITY_WRITE))],
) -> ActivityResponse:
    activity = await ActivityService(session).complete_follow_up(activity_id, actor=actor)
    return (await _activity_page(session, [activity]))[0]


@activities_router.delete(
    "/{activity_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete an activity"
)
async def delete_activity(
    activity_id: uuid.UUID,
    session: SessionDep,
    actor: Annotated[User, Depends(require(Permission.ACTIVITY_WRITE))],
) -> None:
    await ActivityService(session).delete_activity(activity_id, actor=actor)


__all__ = [
    "accounts_router",
    "activities_router",
    "contacts_router",
    "projects_router",
    "technologies_router",
]
