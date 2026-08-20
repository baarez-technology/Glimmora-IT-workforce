"""Excel import staging.

The rule this module exists to enforce: **nothing reaches a business table until
a human has seen what would be written.** An import is three steps — upload,
preview, commit — and the first two write only to these staging tables.

A spreadsheet is the least trustworthy input the platform accepts. It arrives
with merged cells, stray whitespace, dates as text and duplicate rows, and the
person sending it is usually not the person who will live with the consequences.
Silently importing 400 rows of which 30 are wrong is how a clean database stops
being clean.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseEntity
from app.db.types import GUID, JSONType, StrEnumType, UTCDateTime


class ImportEntity(StrEnum):
    """Entities the importer understands (API.md)."""

    CUSTOMERS = "customers"
    CONTACTS = "contacts"
    PROJECTS = "projects"
    REQUIREMENTS = "requirements"
    RESOURCES = "resources"
    DEPLOYMENTS = "deployments"
    BILLING = "billing"


class ImportStatus(StrEnum):
    #: Parsed and validated, nothing written. The only state an upload produces.
    STAGED = "STAGED"
    COMMITTED = "COMMITTED"
    #: Explicitly abandoned. Kept rather than deleted so the attempt is auditable.
    DISCARDED = "DISCARDED"
    FAILED = "FAILED"


class RowState(StrEnum):
    VALID = "VALID"
    #: Would fail validation. Never committed.
    INVALID = "INVALID"
    #: Matches something already in the database. Skipped unless the user says
    #: otherwise, because a re-import should not silently create twins.
    DUPLICATE = "DUPLICATE"
    #: Importable, but something is worth knowing first.
    WARNING = "WARNING"


#: States that will actually be written on commit.
COMMITTABLE_STATES: frozenset[RowState] = frozenset({RowState.VALID, RowState.WARNING})


class ImportBatch(BaseEntity):
    __tablename__ = "import_batches"

    entity_type: Mapped[ImportEntity] = mapped_column(
        StrEnumType(ImportEntity), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[ImportStatus] = mapped_column(
        StrEnumType(ImportStatus), nullable=False, default=ImportStatus.STAGED, index=True
    )

    total_rows: Mapped[int] = mapped_column(nullable=False, default=0)
    valid_rows: Mapped[int] = mapped_column(nullable=False, default=0)
    invalid_rows: Mapped[int] = mapped_column(nullable=False, default=0)
    duplicate_rows: Mapped[int] = mapped_column(nullable=False, default=0)
    warning_rows: Mapped[int] = mapped_column(nullable=False, default=0)
    committed_rows: Mapped[int] = mapped_column(nullable=False, default=0)

    #: Header problems and file-level failures, kept separate from row errors.
    file_errors: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)

    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    committed_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    committed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    rows: Mapped[list[ImportRow]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        lazy="raise",
        passive_deletes=True,
    )

    # Named for both columns: `status` alone collides with the index the
    # naming convention generates for the indexed column itself.
    __table_args__ = (Index("ix_import_batches_status_created", "status", "created_at"),)

    @property
    def is_committable(self) -> bool:
        return self.status is ImportStatus.STAGED and (self.valid_rows + self.warning_rows) > 0


class ImportRow(BaseEntity):
    __tablename__ = "import_rows"

    batch_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: 1-based spreadsheet row number, header excluded — so an error message
    #: names the row the user can actually see in Excel.
    row_number: Mapped[int] = mapped_column(nullable=False)

    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    normalized: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    validation_state: Mapped[RowState] = mapped_column(
        StrEnumType(RowState), nullable=False, index=True
    )
    errors: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)
    warnings: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)

    #: Set on commit, so a re-run of the same batch is a no-op rather than a
    #: second insert.
    created_entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    #: What this row duplicates, when it does.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    batch: Mapped[ImportBatch] = relationship(back_populates="rows", lazy="raise")

    __table_args__ = (
        UniqueConstraint("batch_id", "row_number", name="import_row_unique"),
        Index("ix_import_rows_state", "batch_id", "validation_state"),
    )


__all__ = [
    "COMMITTABLE_STATES",
    "ImportBatch",
    "ImportEntity",
    "ImportRow",
    "ImportStatus",
    "RowState",
]
