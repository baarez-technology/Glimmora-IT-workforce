"""HTTP middleware: request correlation, security headers, body size limit."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.config import settings
from app.core.errors import ErrorCode
from app.core.logging import get_logger, request_id_ctx

logger = get_logger("http")

Handler = Callable[[Request], Awaitable[Response]]

# Paths whose access logs add noise without adding signal.
_QUIET_PATHS = frozenset({"/api/v1/system/health", "/favicon.ico"})


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, bind it to the log context, time the request."""

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise
        finally:
            request_id_ctx.reset(token)

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id

        if request.url.path not in _QUIET_PATHS:
            logger.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """SECURITY.md section 8. Applied to every response, including errors."""

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if settings.is_production:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # The API serves JSON and file streams only; nothing here should ever
        # execute in a browsing context.
        headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies before they are buffered into memory."""

    async def dispatch(self, request: Request, call_next: Handler) -> Response:
        content_length = request.headers.get("content-length")
        too_large = (
            content_length is not None
            and content_length.isdigit()
            and int(content_length) > settings.REQUEST_MAX_BYTES
        )
        if too_large:
            limit_mb = settings.MAX_UPLOAD_BYTES // (1024 * 1024)
            return JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": ErrorCode.PAYLOAD_TOO_LARGE.value,
                        "message": f"That upload is too large. The limit is {limit_mb} MB.",
                        "request_id": getattr(request.state, "request_id", None),
                    }
                },
            )
        return await call_next(request)


__all__ = [
    "BodySizeLimitMiddleware",
    "RequestContextMiddleware",
    "SecurityHeadersMiddleware",
]
