"""Roles, permissions and the RBAC matrix.

This module *is* the access-control policy (SECURITY.md sections 2-3). It is
data, not scattered `if role == ...` checks, so the matrix can be asserted by a
test and rendered in the Admin UI.

Two kinds of permission live here:

* **Action permissions** — may this role call this endpoint at all
  (``requirement:create``).
* **Field permissions** — may this role see this column in the response
  (``resource.cost:view``). Enforced in the serializer, because hiding a rate in
  the UI is not a security control.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "ADMIN"
    MANAGEMENT = "MANAGEMENT"
    SALES = "SALES"
    HR_RESOURCING = "HR_RESOURCING"


ROLE_LABELS: dict[Role, str] = {
    Role.ADMIN: "Administrator",
    Role.MANAGEMENT: "Management",
    Role.SALES: "Sales",
    Role.HR_RESOURCING: "HR / Resourcing",
}

ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.ADMIN: "Full system control: users, roles, scoring rules and audit.",
    Role.MANAGEMENT: (
        "Read across the whole business, including cost and margin. No user administration."
    ),
    Role.SALES: (
        "Demand, accounts, opportunities and submissions. "
        "Sees bill rates and margin, not consultant cost."
    ),
    Role.HR_RESOURCING: (
        "Talent, documents, availability and redeployment. Sees consultant cost, not client margin."
    ),
}


class Permission(StrEnum):
    # --- identity & platform ------------------------------------------
    USER_READ = "user:read"
    USER_CREATE = "user:create"
    USER_UPDATE = "user:update"
    USER_DEACTIVATE = "user:deactivate"
    ROLE_READ = "role:read"
    AUDIT_VIEW = "audit:view"
    SCORING_CONFIG_READ = "scoring_config:read"
    SCORING_CONFIG_EDIT = "scoring_config:edit"

    # --- accounts (Phase 4) --------------------------------------------
    ACCOUNT_READ = "account:read"
    ACCOUNT_CREATE = "account:create"
    ACCOUNT_UPDATE = "account:update"
    ACCOUNT_DELETE = "account:delete"
    CONTACT_READ = "contact:read"
    CONTACT_WRITE = "contact:write"
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    ACTIVITY_READ = "activity:read"
    ACTIVITY_WRITE = "activity:write"

    # --- demand (Phase 5) ----------------------------------------------
    REQUIREMENT_READ = "requirement:read"
    REQUIREMENT_CREATE = "requirement:create"
    REQUIREMENT_UPDATE = "requirement:update"
    REQUIREMENT_DELETE = "requirement:delete"
    JD_PARSE = "jd:parse"

    # --- talent (Phase 6) ----------------------------------------------
    RESOURCE_READ = "resource:read"
    RESOURCE_CREATE = "resource:create"
    RESOURCE_UPDATE = "resource:update"
    RESOURCE_DELETE = "resource:delete"
    CV_PARSE = "cv:parse"
    DOCUMENT_READ = "document:read"
    DOCUMENT_WRITE = "document:write"

    # --- intelligence (Phases 7-9) --------------------------------------
    MATCHING_READ = "matching:read"
    MATCHING_RUN = "matching:run"
    REVERSE_MATCHING_READ = "reverse_matching:read"
    REVERSE_MATCHING_RUN = "reverse_matching:run"
    SCORING_READ = "scoring:read"
    SCORING_RUN = "scoring:run"
    COMMERCIAL_READ = "commercial:read"
    COMMERCIAL_RUN = "commercial:run"

    # --- pipeline (Phase 10) --------------------------------------------
    OPPORTUNITY_READ = "opportunity:read"
    OPPORTUNITY_WRITE = "opportunity:write"
    SUBMISSION_READ = "submission:read"
    SUBMISSION_WRITE = "submission:write"
    INTERVIEW_READ = "interview:read"
    INTERVIEW_WRITE = "interview:write"
    COMMUNICATION_READ = "communication:read"
    COMMUNICATION_WRITE = "communication:write"

    # --- delivery (Phase 11) --------------------------------------------
    DEPLOYMENT_READ = "deployment:read"
    DEPLOYMENT_WRITE = "deployment:write"
    BILLING_READ = "billing:read"
    BILLING_WRITE = "billing:write"

    # --- platform (Phases 11-12) ----------------------------------------
    DASHBOARD_MANAGEMENT = "dashboard:management"
    DASHBOARD_SALES = "dashboard:sales"
    DASHBOARD_HR = "dashboard:hr"
    DASHBOARD_ADMIN = "dashboard:admin"
    IMPORT_RUN = "import:run"
    EXPORT_RUN = "export:run"

    # --- sensitive FIELD permissions (SECURITY.md section 3) -------------
    # Absence of these strips the key from the response entirely.
    FIELD_RESOURCE_COST = "resource.cost:view"
    FIELD_BILLING_RATE = "billing.rate:view"
    FIELD_MARGIN = "margin:view"
    FIELD_CONTRACT_VALUE = "contract_value:view"
    FIELD_DOCUMENT_PERSONAL_VIEW = "document.personal:view"
    FIELD_DOCUMENT_PERSONAL_DOWNLOAD = "document.personal:download"


P = Permission

# Every permission in the system. ADMIN holds all of them by definition.
ALL_PERMISSIONS: frozenset[Permission] = frozenset(Permission)

_MANAGEMENT: frozenset[Permission] = frozenset(
    {
        # Management observes the whole business and changes nothing operational.
        P.USER_READ,
        P.ROLE_READ,
        P.AUDIT_VIEW,
        P.SCORING_CONFIG_READ,
        P.ACCOUNT_READ,
        P.CONTACT_READ,
        P.PROJECT_READ,
        P.ACTIVITY_READ,
        P.REQUIREMENT_READ,
        P.RESOURCE_READ,
        P.DOCUMENT_READ,
        P.MATCHING_READ,
        P.REVERSE_MATCHING_READ,
        P.SCORING_READ,
        P.COMMERCIAL_READ,
        P.OPPORTUNITY_READ,
        P.SUBMISSION_READ,
        P.INTERVIEW_READ,
        P.COMMUNICATION_READ,
        P.DEPLOYMENT_READ,
        P.BILLING_READ,
        P.DASHBOARD_MANAGEMENT,
        P.EXPORT_RUN,
        # Sees both sides of the commercial picture.
        P.FIELD_RESOURCE_COST,
        P.FIELD_BILLING_RATE,
        P.FIELD_MARGIN,
        P.FIELD_CONTRACT_VALUE,
        # Oversight of personal documents without taking copies away.
        P.FIELD_DOCUMENT_PERSONAL_VIEW,
    }
)

_SALES: frozenset[Permission] = frozenset(
    {
        P.ROLE_READ,
        P.SCORING_CONFIG_READ,
        P.ACCOUNT_READ,
        P.ACCOUNT_CREATE,
        P.ACCOUNT_UPDATE,
        P.ACCOUNT_DELETE,
        P.CONTACT_READ,
        P.CONTACT_WRITE,
        P.PROJECT_READ,
        P.PROJECT_WRITE,
        P.ACTIVITY_READ,
        P.ACTIVITY_WRITE,
        P.REQUIREMENT_READ,
        P.REQUIREMENT_CREATE,
        P.REQUIREMENT_UPDATE,
        P.REQUIREMENT_DELETE,
        P.JD_PARSE,
        P.RESOURCE_READ,
        P.MATCHING_READ,
        P.MATCHING_RUN,
        P.REVERSE_MATCHING_READ,
        P.SCORING_READ,
        P.SCORING_RUN,
        P.COMMERCIAL_READ,
        P.COMMERCIAL_RUN,
        P.OPPORTUNITY_READ,
        P.OPPORTUNITY_WRITE,
        P.SUBMISSION_READ,
        P.SUBMISSION_WRITE,
        P.INTERVIEW_READ,
        P.INTERVIEW_WRITE,
        P.COMMUNICATION_READ,
        P.COMMUNICATION_WRITE,
        P.DEPLOYMENT_READ,
        P.DEPLOYMENT_WRITE,
        P.BILLING_READ,
        P.DASHBOARD_SALES,
        P.IMPORT_RUN,
        P.EXPORT_RUN,
        # Sales prices the client side and must prioritise on profitability
        # (SOW section 9), so it sees bill rate and margin — but never the
        # consultant's cost rate, which Resourcing negotiates.
        P.FIELD_BILLING_RATE,
        P.FIELD_MARGIN,
        P.FIELD_CONTRACT_VALUE,
    }
)

_HR_RESOURCING: frozenset[Permission] = frozenset(
    {
        P.ROLE_READ,
        P.SCORING_CONFIG_READ,
        P.ACCOUNT_READ,
        P.CONTACT_READ,
        P.PROJECT_READ,
        P.ACTIVITY_READ,
        P.ACTIVITY_WRITE,
        P.REQUIREMENT_READ,
        P.REQUIREMENT_UPDATE,
        P.JD_PARSE,
        P.RESOURCE_READ,
        P.RESOURCE_CREATE,
        P.RESOURCE_UPDATE,
        P.RESOURCE_DELETE,
        P.CV_PARSE,
        P.DOCUMENT_READ,
        P.DOCUMENT_WRITE,
        P.MATCHING_READ,
        P.MATCHING_RUN,
        P.REVERSE_MATCHING_READ,
        P.REVERSE_MATCHING_RUN,
        P.SCORING_READ,
        P.COMMERCIAL_READ,
        P.OPPORTUNITY_READ,
        P.SUBMISSION_READ,
        P.SUBMISSION_WRITE,
        P.INTERVIEW_READ,
        P.INTERVIEW_WRITE,
        P.COMMUNICATION_READ,
        P.COMMUNICATION_WRITE,
        P.DEPLOYMENT_READ,
        P.DEPLOYMENT_WRITE,
        P.DASHBOARD_HR,
        P.IMPORT_RUN,
        P.EXPORT_RUN,
        # Resourcing negotiates consultant cost and handles visas.
        P.FIELD_RESOURCE_COST,
        P.FIELD_DOCUMENT_PERSONAL_VIEW,
        P.FIELD_DOCUMENT_PERSONAL_DOWNLOAD,
    }
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: ALL_PERMISSIONS,
    Role.MANAGEMENT: _MANAGEMENT,
    Role.SALES: _SALES,
    Role.HR_RESOURCING: _HR_RESOURCING,
}

# Field permissions are called out separately so the Admin UI can present the
# "who can see money and passports" question on its own.
FIELD_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        P.FIELD_RESOURCE_COST,
        P.FIELD_BILLING_RATE,
        P.FIELD_MARGIN,
        P.FIELD_CONTRACT_VALUE,
        P.FIELD_DOCUMENT_PERSONAL_VIEW,
        P.FIELD_DOCUMENT_PERSONAL_DOWNLOAD,
    }
)


def permissions_for(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS[role]


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]


def permission_matrix() -> list[dict[str, object]]:
    """The full matrix, for the Admin > Roles screen and the policy test."""
    return [
        {
            "permission": permission.value,
            "is_field_permission": permission in FIELD_PERMISSIONS,
            "roles": {role.value: permission in ROLE_PERMISSIONS[role] for role in Role},
        }
        for permission in sorted(Permission, key=lambda item: item.value)
    ]


__all__ = [
    "ALL_PERMISSIONS",
    "FIELD_PERMISSIONS",
    "ROLE_DESCRIPTIONS",
    "ROLE_LABELS",
    "ROLE_PERMISSIONS",
    "Permission",
    "Role",
    "has_permission",
    "permission_matrix",
    "permissions_for",
]
