"""The single error contract for the whole API (API.md section 1).

Raw exceptions and stack traces are never serialized to a client. Every failure
leaves the application as one shape, carrying a request_id that correlates with
the structured log entry holding the real detail.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    DUPLICATE_SUBMISSION = "DUPLICATE_SUBMISSION"
    RATE_LIMITED = "RATE_LIMITED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.VALIDATION_ERROR: 422,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.DUPLICATE_SUBMISSION: 409,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.DEPENDENCY_UNAVAILABLE: 503,
    ErrorCode.PAYLOAD_TOO_LARGE: 413,
    ErrorCode.UNSUPPORTED_MEDIA_TYPE: 415,
    ErrorCode.INTERNAL_ERROR: 500,
}


class AppError(Exception):
    """Base class for every deliberate, user-facing failure.

    `message` is written for a Glimmora user, not for a developer. Developer
    detail belongs in `log_detail`, which is logged and never serialized.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    message: str = "Something went wrong. Please try again."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: list[dict[str, Any]] | None = None,
        log_detail: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.details = details or []
        self.log_detail = log_detail
        self.headers = headers or {}
        super().__init__(self.message)

    @property
    def status_code(self) -> int:
        return _STATUS_BY_CODE[self.code]

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": self.message,
        }
        if self.details:
            payload["details"] = self.details
        if request_id:
            payload["request_id"] = request_id
        return {"error": payload}


class ValidationError(AppError):
    code = ErrorCode.VALIDATION_ERROR
    message = "Some of the information provided is not valid."


class UnauthenticatedError(AppError):
    code = ErrorCode.UNAUTHENTICATED
    message = "Please sign in to continue."


class ForbiddenError(AppError):
    code = ErrorCode.FORBIDDEN
    message = "You do not have permission to do that."


class NotFoundError(AppError):
    code = ErrorCode.NOT_FOUND
    message = "That record could not be found."

    def __init__(self, entity: str = "record", identifier: Any = None, **kwargs: Any) -> None:
        message = f"That {entity} could not be found."
        super().__init__(
            message,
            log_detail=f"{entity} id={identifier!r} not found",
            **kwargs,
        )


class ConflictError(AppError):
    code = ErrorCode.CONFLICT
    message = "That change conflicts with the current state of the record."


class DuplicateSubmissionError(AppError):
    """Raised when a candidate has already been put forward for a requirement.

    Carries the prior submission so the UI can show who submitted, when, and the
    current status (master brief section 10).
    """

    code = ErrorCode.DUPLICATE_SUBMISSION
    message = "This candidate has already been submitted for this requirement."

    def __init__(self, *, submitted_at: str, submitted_by: str, current_status: str) -> None:
        super().__init__(
            details=[
                {"field": "resource_id", "message": "Already submitted"},
                {"field": "submitted_at", "message": submitted_at},
                {"field": "submitted_by", "message": submitted_by},
                {"field": "current_status", "message": current_status},
            ]
        )


class RateLimitedError(AppError):
    code = ErrorCode.RATE_LIMITED
    message = "Too many requests. Please wait a moment and try again."


class DependencyUnavailableError(AppError):
    code = ErrorCode.DEPENDENCY_UNAVAILABLE
    message = "A service this action depends on is temporarily unavailable."

    def __init__(self, dependency: str, **kwargs: Any) -> None:
        super().__init__(log_detail=f"dependency unavailable: {dependency}", **kwargs)
        self.dependency = dependency


class PayloadTooLargeError(AppError):
    code = ErrorCode.PAYLOAD_TOO_LARGE
    message = "That file is too large."


class UnsupportedMediaTypeError(AppError):
    code = ErrorCode.UNSUPPORTED_MEDIA_TYPE
    message = "That file type is not supported."


class AIUnavailableError(DependencyUnavailableError):
    """AI degraded — never blocks the business workflow (AI_ARCHITECTURE.md s7)."""

    message = (
        "AI parsing is temporarily unavailable. "
        "The record has been saved and you can complete the fields manually."
    )


class DocumentParseError(AppError):
    code = ErrorCode.VALIDATION_ERROR
    message = (
        "Unable to read this document. It may be an unsupported format, "
        "corrupted, or contain too little text. You can still enter the details manually."
    )


__all__ = [
    "AIUnavailableError",
    "AppError",
    "ConflictError",
    "DependencyUnavailableError",
    "DocumentParseError",
    "DuplicateSubmissionError",
    "ErrorCode",
    "ForbiddenError",
    "NotFoundError",
    "PayloadTooLargeError",
    "RateLimitedError",
    "UnauthenticatedError",
    "UnsupportedMediaTypeError",
    "ValidationError",
]
