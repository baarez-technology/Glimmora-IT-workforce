"""Base repository.

Repositories own data access only: no business rules, no authorization, no
HTTP concepts. Services compose them.
"""

from __future__ import annotations

import uuid
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------ reading
    def select(self) -> Select[Any]:
        stmt = select(self.model)
        # Soft-deleted rows are invisible unless a caller asks for them
        # explicitly, so no downstream query has to remember the filter.
        if hasattr(self.model, "deleted_at"):
            stmt = stmt.where(self.model.deleted_at.is_(None))  # type: ignore[attr-defined]
        return stmt

    async def get(self, entity_id: uuid.UUID) -> ModelT | None:
        stmt = self.select().where(self.model.id == entity_id)  # type: ignore[attr-defined]
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def exists(self, entity_id: uuid.UUID) -> bool:
        return await self.get(entity_id) is not None

    # ------------------------------------------------------------ writing
    async def add(self, entity: ModelT) -> ModelT:
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)
        await self.session.flush()

    async def flush(self) -> None:
        await self.session.flush()


__all__ = ["BaseRepository"]
