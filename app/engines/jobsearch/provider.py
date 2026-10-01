"""Job search providers.

One interface, so the screens never learn which service is behind them.
Swapping Apify for Adzuna, Jooble or a different actor is a new adapter and an
environment variable, not a change to the API or the UI.

Searching is **asynchronous by necessity**, not by preference. A scraper run
takes about thirty seconds; holding an HTTP request open for that long ties up
a worker and dies at the default proxy timeout. So a search is started, and its
results are collected afterwards — the same shape as the Excel import, which is
staged for the same honesty reasons.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Protocol

from app.models.jobfeed import WorkplaceType


class SearchStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(slots=True, frozen=True)
class SearchQuery:
    """What the person asked for."""

    titles: list[str]
    locations: list[str] = field(default_factory=list)
    workplace_type: WorkplaceType | None = None
    #: '', 'r2592000' (month), 'r604800' (week), 'r86400' (24 hours)
    posted_within: str | None = None
    rows: int = 25

    def cache_key(self) -> str:
        """Identity of a search, so the same question is not paid for twice."""
        parts = [
            "|".join(sorted(title.strip().lower() for title in self.titles)),
            "|".join(sorted(place.strip().lower() for place in self.locations)),
            self.workplace_type.value if self.workplace_type else "",
            self.posted_within or "",
            str(self.rows),
        ]
        return "\x1f".join(parts)


@dataclass(slots=True)
class SearchResult:
    """One job, already in the shape the feed stores."""

    title: str
    company_name: str | None
    location: str | None
    country: str | None
    workplace_type: WorkplaceType
    description: str | None
    url: str | None
    posted_at: date | None
    external_id: str | None = None


@dataclass(slots=True)
class SearchRun:
    """A search in flight, or finished."""

    id: str
    status: SearchStatus
    results: list[SearchResult] = field(default_factory=list)
    error: str | None = None


class JobSearchProviderProtocol(Protocol):
    """What any provider must do.

    Two calls, not one, because a run outlives a request.
    """

    name: str

    async def start(self, query: SearchQuery) -> SearchRun:
        """Begin a search and return its handle immediately."""
        ...

    async def collect(self, run_id: str) -> SearchRun:
        """Where a run has got to, with results once it has finished."""
        ...
