"""Exception handlers producing the single API error contract.

Master brief section 24: never expose a raw exception. Handlers convert every
failure into the documented envelope and log the real detail against the
request id.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.errors import AppError, ErrorCode
from app.core.logging import get_logger

logger = get_logger("error")

_HTTP_STATUS_TO_CODE = {
    400: ErrorCode.VALIDATION_ERROR,
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.VALIDATION_ERROR,
    409: ErrorCode.CONFLICT,
    413: ErrorCode.PAYLOAD_TOO_LARGE,
    415: ErrorCode.UNSUPPORTED_MEDIA_TYPE,
    429: ErrorCode.RATE_LIMITED,
    503: ErrorCode.DEPENDENCY_UNAVAILABLE,
}

_FRIENDLY_HTTP_MESSAGE = {
    401: "Please sign in to continue.",
    403: "You do not have permission to do that.",
    404: "That page or record could not be found.",
    405: "That action is not supported here.",
    429: "Too many requests. Please wait a moment and try again.",
}


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def _envelope(
    code: ErrorCode,
    message: str,
    *,
    request: Request,
    details: list[dict[str, str]] | None = None,
    status_code: int,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    payload: dict[str, object] = {"code": code.value, "message": message}
    if details:
        payload["details"] = details
    if rid := _request_id(request):
        payload["request_id"] = rid
    return JSONResponse(status_code=status_code, content={"error": payload}, headers=headers)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    log = logger.warning if exc.status_code < 500 else logger.error
    log(
        "app_error",
        code=exc.code.value,
        status=exc.status_code,
        path=request.url.path,
        detail=exc.log_detail,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_payload(_request_id(request)),
        headers=exc.headers or None,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = []
    for error in exc.errors():
        location = [str(part) for part in error.get("loc", []) if part not in ("body", "query")]
        details.append(
            {
                "field": ".".join(location) or "request",
                "message": str(error.get("msg", "Invalid value")),
            }
        )
    logger.info("validation_error", path=request.url.path, field_count=len(details))
    return _envelope(
        ErrorCode.VALIDATION_ERROR,
        "Some of the information provided is not valid.",
        request=request,
        details=details,
        status_code=422,
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
    message = _FRIENDLY_HTTP_MESSAGE.get(exc.status_code)
    if message is None:
        # Starlette's default detail is a short generic phrase, safe to surface.
        message = str(exc.detail) if exc.detail else "The request could not be completed."
    headers = dict(exc.headers) if exc.headers else None
    return _envelope(code, message, request=request, status_code=exc.status_code, headers=headers)


async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    # The driver message can name columns and constraint values, so it is logged
    # rather than returned.
    logger.warning("integrity_error", path=request.url.path, detail=str(exc.orig))
    return _envelope(
        ErrorCode.CONFLICT,
        "That change conflicts with an existing record.",
        request=request,
        status_code=409,
    )


async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError) -> JSONResponse:
    logger.error("database_error", path=request.url.path, detail=str(exc), exc_info=True)
    return _envelope(
        ErrorCode.INTERNAL_ERROR,
        "We could not complete that action. Please try again.",
        request=request,
        status_code=500,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled_error", path=request.url.path, detail=str(exc), exc_info=True)
    return _envelope(
        ErrorCode.INTERNAL_ERROR,
        "Something went wrong. The issue has been logged.",
        request=request,
        status_code=500,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, integrity_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(SQLAlchemyError, sqlalchemy_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)


__all__ = ["register_exception_handlers"]
