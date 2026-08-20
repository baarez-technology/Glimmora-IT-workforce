"""Aggregate router for /api/v1.

Routers are mounted here as each phase delivers them, so this file doubles as a
live map of what is actually built versus what is still planned.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    accounts,
    audit,
    auth,
    delivery,
    matching,
    pipeline,
    platform,
    requirements,
    resources,
    reverse_matching,
    roles,
    scoring,
    system,
    users,
)

api_router = APIRouter()

# --- Phase 2 -------------------------------------------------------------
api_router.include_router(system.router)

# --- Phase 3: auth, users, roles -----------------------------------------
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(roles.router)
api_router.include_router(audit.router)
# --- Phase 4: accounts, contacts, projects, activities -------------------
api_router.include_router(accounts.accounts_router)
api_router.include_router(accounts.contacts_router)
api_router.include_router(accounts.projects_router)
api_router.include_router(accounts.technologies_router)
api_router.include_router(accounts.activities_router)
# --- Phase 5: requirements, JD parsing -----------------------------------
api_router.include_router(requirements.router)
api_router.include_router(requirements.skills_router)
# --- Phase 6: resources, CV parsing, documents ---------------------------
api_router.include_router(resources.router)
api_router.include_router(resources.documents_router)
# --- Phase 7: matching, scoring configuration ----------------------------
api_router.include_router(matching.router)
api_router.include_router(matching.scoring_router)
# --- Phase 8: reverse matching, bench radar ------------------------------
api_router.include_router(reverse_matching.router)
# --- Phase 9: addressability, commercial, opportunity scoring ------------
api_router.include_router(scoring.router)
# --- Phase 10: opportunities, submissions, interviews, communications ----
api_router.include_router(pipeline.opportunities_router)
api_router.include_router(pipeline.submissions_router)
api_router.include_router(pipeline.interviews_router)
api_router.include_router(pipeline.communications_router)
# --- Phase 11: deployments, billing, dashboards --------------------------
api_router.include_router(delivery.deployments_router)
api_router.include_router(delivery.billing_router)
api_router.include_router(delivery.dashboard_router)
# --- Phase 12: notifications, imports, exports ---------------------------
api_router.include_router(platform.notifications_router)
api_router.include_router(platform.imports_router)
api_router.include_router(platform.exports_router)

__all__ = ["api_router"]
