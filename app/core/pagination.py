"""Shared pagination, sorting and search primitives (API.md section 1).

Every list endpoint in the platform returns the same envelope, so the frontend
DataTable is written once and reused everywhere.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Generic, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError

T = TypeVar("T")

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25


class PageParams(BaseModel):
    """Query parameters shared by every list endpoint."""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    sort: str | None = Field(default=None, description="Field name, prefix with '-' for descending")
    q: str | None = Field(default=None, max_length=200, description="Free-text search")

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size

    def sort_spec(self) -> tuple[str, bool] | None:
        """Return (field, descending) or None."""
        if not self.sort:
            return None
        field = self.sort.strip()
        descending = field.startswith("-")
        return (field.lstrip("-+"), descending)


def page_params(
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    sort: Annotated[str | None, Query(max_length=64)] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> PageParams:
    """FastAPI dependency so pagination appears in the OpenAPI schema."""
    return PageParams(page=page, page_size=page_size, sort=sort, q=q)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int

    @classmethod
    def build(cls, items: list[T], total: int, params: PageParams) -> Page[T]:
        return cls(
            items=items,
            total=total,
            page=params.page,
            page_size=params.page_size,
            pages=max(1, math.ceil(total / params.page_size)) if total else 0,
        )

    @classmethod
    def empty(cls, params: PageParams) -> Page[T]:
        return cls(items=[], total=0, page=params.page, page_size=params.page_size, pages=0)


def apply_sort(stmt: Select[Any], model: Any, params: PageParams, allowed: set[str]) -> Select[Any]:
    """Apply an allow-listed sort. Unknown fields are rejected, never ignored.

    Silently ignoring a bad sort field hides frontend bugs and makes list screens
    look non-deterministic, so this raises instead.
    """
    spec = params.sort_spec()
    if spec is None:
        return stmt
    field, descending = spec
    if field not in allowed:
        raise ValidationError(
            "That sort field is not supported.",
            details=[{"field": "sort", "message": f"Allowed: {', '.join(sorted(allowed))}"}],
        )
    column = getattr(model, field)
    return stmt.order_by(column.desc() if descending else column.asc())


async def paginate(
    session: AsyncSession,
    stmt: Select[Any],
    params: PageParams,
) -> tuple[list[Any], int]:
    """Run a count query and a page query for the given statement."""
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int((await session.execute(count_stmt)).scalar_one())
    if total == 0:
        return [], 0
    result = await session.execute(stmt.offset(params.offset).limit(params.page_size))
    return list(result.scalars().unique().all()), total


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Page",
    "PageParams",
    "apply_sort",
    "page_params",
    "paginate",
]
