"""Portable column types.

DATABASE.md section 1: PostgreSQL is the production database, but the same
models must run on SQLite so the test suite and the no-infrastructure
development path need no Docker daemon.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CHAR, DateTime, Numeric, String, TypeDecorator
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.types import JSON

# JSONB on PostgreSQL, JSON everywhere else.
JSONType = JSON().with_variant(JSONB, "postgresql")

# Money: NUMERIC(14, 2) — never a float (DATABASE.md section 1).
MoneyType = Numeric(14, 2)

# Scores are 0-100 with one decimal of headroom for weighted composition.
ScoreType = Numeric(5, 2)


class GUID(TypeDecorator[uuid.UUID]):
    """UUID primary keys: native uuid on PostgreSQL, CHAR(36) elsewhere."""

    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PGUUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        return value if dialect.name == "postgresql" else str(value)

    def process_result_value(self, value: Any, dialect: Any) -> uuid.UUID | None:
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetimes stored as UTC.

    SQLite drops tzinfo silently, which turns an SLA countdown into a lie. This
    normalises on the way in and re-attaches UTC on the way out so both
    dialects behave identically.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Any) -> Any:
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"Expected datetime, received {type(value).__name__}")
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Any) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class StrEnumType(TypeDecorator[str]):
    """Store Python str-enums as VARCHAR.

    DATABASE.md section 1: no native PostgreSQL enums, so adding a status value
    is a code change rather than a migration dance.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type, length: int = 48) -> None:
        self.enum_class = enum_class
        super().__init__(length=length)

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return str(getattr(value, "value", value))
        candidate = str(value)
        self.enum_class(candidate)  # raises ValueError on an unknown member
        return candidate

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        return None if value is None else self.enum_class(value)


def utcnow() -> datetime:
    """Timezone-aware now. Used as the default for every timestamp column."""
    return datetime.now(UTC)


def money(value: float | int | str | Decimal | None) -> Decimal | None:
    """Coerce to a 2dp Decimal. Never build money from a float directly."""
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"))


__all__ = [
    "GUID",
    "JSONType",
    "MoneyType",
    "ScoreType",
    "StrEnumType",
    "UTCDateTime",
    "money",
    "utcnow",
]
