"""System endpoints: health and non-secret runtime configuration.

Health deliberately reports *which dependencies are running on a fallback*, so a
degraded run is never mistaken for a healthy one (ARCHITECTURE.md section 6).
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from app.core.config import settings
from app.db.session import check_database

router = APIRouter(prefix="/system", tags=["system"])

HealthState = Literal["healthy", "degraded", "unhealthy"]


class ComponentHealth(BaseModel):
    name: str
    state: Literal["ok", "fallback", "down"]
    detail: str | None = None


class HealthResponse(BaseModel):
    status: HealthState
    version: str
    environment: str
    components: list[ComponentHealth]
    degraded: dict[str, str] = Field(
        default_factory=dict,
        description="Dependencies running on a documented fallback rather than the primary.",
    )


class ConfigResponse(BaseModel):
    """Non-secret runtime configuration the frontend needs. No credentials, ever."""

    app_name: str
    environment: str
    api_prefix: str
    base_currency: str
    default_timezone: str
    max_upload_mb: int
    ai_enabled: bool
    sla_thresholds_hours: dict[str, int]
    document_expiring_soon_days: int
    bench_milestone_days: list[int]


@router.get("/health", response_model=HealthResponse, summary="Dependency health")
async def health(response: Response) -> HealthResponse:
    components: list[ComponentHealth] = []
    degraded = settings.degraded_components()

    db_ok, db_error = await check_database()
    components.append(
        ComponentHealth(
            name="database",
            state="ok" if db_ok else "down",
            detail=degraded.get("database") or (db_error if not db_ok else None),
        )
    )
    if db_ok and "database" in degraded:
        components[-1].state = "fallback"

    for name in ("vector_store", "object_storage", "cache", "queue", "llm", "embeddings", "email"):
        components.append(
            ComponentHealth(
                name=name,
                state="fallback" if name in degraded else "ok",
                detail=degraded.get(name),
            )
        )

    if not db_ok:
        status: HealthState = "unhealthy"
    elif degraded:
        status = "degraded"
    else:
        status = "healthy"

    # Degraded is intentionally still 200: the platform is usable. Only a dead
    # database is a failed health check.
    response.status_code = 503 if status == "unhealthy" else 200

    return HealthResponse(
        status=status,
        version="0.2.0",
        environment=settings.APP_ENV.value,
        components=components,
        degraded=degraded,
    )


@router.get("/config", response_model=ConfigResponse, summary="Public runtime configuration")
async def public_config() -> ConfigResponse:
    return ConfigResponse(
        app_name=settings.APP_NAME,
        environment=settings.APP_ENV.value,
        api_prefix=settings.API_V1_PREFIX,
        base_currency=settings.BASE_CURRENCY,
        default_timezone=settings.DEFAULT_TIMEZONE,
        max_upload_mb=settings.MAX_UPLOAD_BYTES // (1024 * 1024),
        ai_enabled=settings.LLM_PROVIDER.value != "null",
        sla_thresholds_hours={
            "urgent": settings.SLA_URGENT_HOURS,
            "due_soon": settings.SLA_DUE_SOON_HOURS,
        },
        document_expiring_soon_days=settings.DOCUMENT_EXPIRING_SOON_DAYS,
        bench_milestone_days=settings.bench_milestone_days,
    )


__all__ = ["router"]
