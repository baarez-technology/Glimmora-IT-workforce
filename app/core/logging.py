"""Structured logging with request correlation and PII redaction.

SECURITY.md section 7: personal data is not written to application logs.
Redaction happens in the processor chain, so it applies to every logger in the
process including third-party libraries routed through stdlib logging.
"""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

import structlog

from app.core.config import settings

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_ctx: ContextVar[str | None] = ContextVar("user_id", default=None)

# Keys whose values never appear in logs, at any nesting depth.
SENSITIVE_KEYS = frozenset(
    {
        "password",
        "new_password",
        "current_password",
        "hashed_password",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "api_key",
        "secret",
        "jwt_secret",
        "secret_key",
        "email",
        "phone",
        "passport",
        "passport_number",
        "visa",
        "visa_number",
        "qid",
        "national_id",
        "reference_number",
        "cv_text",
        "resume_text",
        "body",
    }
)

REDACTED = "[redacted]"

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        value = _EMAIL_RE.sub(REDACTED, value)
        value = _BEARER_RE.sub("Bearer " + REDACTED, value)
    return value


def _redact(obj: Any, depth: int = 0) -> Any:
    if depth > 6:
        return obj
    if isinstance(obj, dict):
        return {
            k: (REDACTED if k.lower() in SENSITIVE_KEYS else _redact(v, depth + 1))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return type(obj)(_redact(v, depth + 1) for v in obj)
    return _redact_value(obj)


def redaction_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return _redact(event_dict)


def context_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    if (rid := request_id_ctx.get()) is not None:
        event_dict.setdefault("request_id", rid)
    if (uid := user_id_ctx.get()) is not None:
        event_dict.setdefault("user_id", uid)
    return event_dict


def configure_logging() -> None:
    """Idempotent: safe to call from the API, the worker and the test suite."""
    level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        context_processor,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redaction_processor,
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if settings.LOG_JSON or settings.is_production
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level, force=True)
    for noisy in ("uvicorn.access", "sqlalchemy.engine", "botocore", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(level, logging.WARNING))


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Business events (master brief section 25). Emitting these through one helper
# keeps event names consistent and greppable across API and worker processes.
# --------------------------------------------------------------------------

BUSINESS_EVENTS = frozenset(
    {
        "user_login",
        "requirement_created",
        "jd_uploaded",
        "jd_parsed",
        "cv_uploaded",
        "cv_parsed",
        "match_generated",
        "reverse_match_generated",
        "opportunity_created",
        "score_calculated",
        "cv_submitted",
        "interview_created",
        "candidate_selected",
        "deployment_created",
        "billing_created",
        "document_expiring",
        "notification_sent",
    }
)


def log_business_event(event: str, **fields: Any) -> None:
    """Emit one of the tracked business events with a stable name."""
    logger = get_logger("business")
    if event not in BUSINESS_EVENTS:
        logger.warning("unknown_business_event", attempted_event=event)
    logger.info(event, **fields)


__all__ = [
    "BUSINESS_EVENTS",
    "configure_logging",
    "get_logger",
    "log_business_event",
    "request_id_ctx",
    "user_id_ctx",
]
