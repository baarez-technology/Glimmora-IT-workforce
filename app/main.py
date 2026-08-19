"""FastAPI application factory for the Glimmora IT Workforce Intelligence Engine."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exception_handlers import register_exception_handlers
from app.core.logging import configure_logging, get_logger
from app.core.middleware import (
    BodySizeLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
from app.db.session import check_database, dispose_engine

logger = get_logger("app")

DESCRIPTION = """
Internal IT outsourcing demand-to-deployment platform.

Core loop: **IT Demand -> Addressability -> Resource Match -> Sales Action ->
CV Submission -> Interview -> Selection -> Deployment -> Billing -> Redeployment**

Internal tool. Not a SaaS product.
""".strip()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()

    degraded = settings.degraded_components()
    logger.info(
        "startup",
        environment=settings.APP_ENV.value,
        database="sqlite" if settings.is_sqlite else "postgresql",
        degraded_count=len(degraded),
    )
    for component, reason in degraded.items():
        logger.warning("running_on_fallback", component=component, reason=reason)

    if insecure := settings.insecure_defaults_in_use():
        logger.warning("placeholder_secrets_in_use", keys=insecure)

    db_ok, db_error = await check_database()
    if not db_ok:
        # Do not crash: the API should come up and report an unhealthy database
        # rather than crash-loop, so operators can see the reason at /health.
        logger.error("database_unavailable_at_startup", error=db_error)

    yield

    logger.info("shutdown")
    await dispose_engine()


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(
        title=settings.APP_NAME,
        description=DESCRIPTION,
        version="0.2.0",
        docs_url=settings.docs_url,
        redoc_url=None,
        openapi_url=settings.openapi_url,
        lifespan=lifespan,
    )

    # Middleware runs bottom-up: request context is outermost so every log line
    # and every error response carries the request id.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(api_router, prefix=settings.API_V1_PREFIX)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "name": settings.APP_NAME,
            "api": settings.API_V1_PREFIX,
            "health": f"{settings.API_V1_PREFIX}/system/health",
        }

    return app


app = create_app()
