"""Declarative base and the mixins every business entity shares."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import GUID, UTCDateTime, utcnow

# Deterministic constraint names so Alembic autogenerate produces stable,
# reviewable migrations instead of database-assigned identifiers.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        identifier = getattr(self, "id", None)
        return f"<{type(self).__name__} id={identifier}>"


class UUIDPrimaryKeyMixin:
    """UUID v4 primary key, generated in Python so the id exists before flush."""

    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4, sort_order=-100
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, nullable=False, index=True, sort_order=100
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow, nullable=False, sort_order=101
    )


class SoftDeleteMixin:
    """Archiving for entities users can remove but auditors still need.

    Repositories filter `deleted_at IS NULL` by default; only Admin hard-deletes.
    """

    deleted_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), nullable=True, index=True, sort_order=102
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class BaseEntity(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Standard business entity: UUID id + timestamps."""

    __abstract__ = True

    def to_dict(self, exclude: set[str] | None = None) -> dict[str, Any]:
        """Shallow column dump, used by the audit diff builder."""
        skip = exclude or set()
        return {
            column.key: getattr(self, column.key)
            for column in self.__table__.columns
            if column.key not in skip
        }


class SoftDeleteEntity(BaseEntity, SoftDeleteMixin):
    __abstract__ = True


__all__ = [
    "Base",
    "BaseEntity",
    "SoftDeleteEntity",
    "SoftDeleteMixin",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
]
