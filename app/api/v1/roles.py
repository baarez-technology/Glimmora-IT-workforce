"""Role catalogue and the permission matrix.

Exposed read-only to every signed-in user so the UI can explain *why* something
is hidden, rather than silently omitting it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.deps import require
from app.core.permissions import (
    ROLE_DESCRIPTIONS,
    ROLE_LABELS,
    ROLE_PERMISSIONS,
    Permission,
    Role,
    permission_matrix,
)
from app.models.identity import User
from app.schemas.identity import PermissionMatrixRow, RoleCatalogueResponse, RoleResponse

router = APIRouter(prefix="/roles", tags=["roles"])


@router.get("", response_model=RoleCatalogueResponse, summary="Roles and permission matrix")
async def list_roles(
    _: Annotated[User, Depends(require(Permission.ROLE_READ))],
) -> RoleCatalogueResponse:
    roles = [
        RoleResponse(
            role=role,
            label=ROLE_LABELS[role],
            description=ROLE_DESCRIPTIONS[role],
            permission_count=len(ROLE_PERMISSIONS[role]),
            permissions=sorted(permission.value for permission in ROLE_PERMISSIONS[role]),
        )
        for role in Role
    ]
    matrix = [PermissionMatrixRow(**row) for row in permission_matrix()]  # type: ignore[arg-type]
    return RoleCatalogueResponse(roles=roles, matrix=matrix)


__all__ = ["router"]
