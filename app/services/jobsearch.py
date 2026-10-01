"""Job search.

Search is browsing, not collecting. A result is not put into anybody's feed
until they choose to keep it — otherwise one careless query would bury a
person's feed under fifty jobs they never asked for.

Results are cached by query, because a run costs real money and takes about
thirty seconds. Asking the same question twice within the window is free and
instant.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.core.cache import get_cache
from app.core.config import JobSearchProvider, settings
from app.core.errors import DependencyUnavailableError, ValidationError
from app.engines.jobsearch.apify import ApifyJobSearch
from app.engines.jobsearch.provider import SearchQuery, SearchRun, SearchStatus
from app.models.jobfeed import WorkplaceType

_RUN_PREFIX = "jobsearch:run:"
_QUERY_PREFIX = "jobsearch:query:"


def provider_for() -> ApifyJobSearch | None:
    """The configured provider, or None.

    None is not a degraded mode. With nothing configured the search endpoints
    report themselves unavailable and the screen hides, rather than offering a
    box that can never return anything.
    """
    if settings.JOB_SEARCH_PROVIDER is JobSearchProvider.APIFY:
        adapter = ApifyJobSearch()
        return adapter if adapter.configured else None
    return None


class JobSearchService:
    """Start a search, then collect it. Two calls because a run outlives one."""

    def __init__(self) -> None:
        self.provider = provider_for()
        self.cache = get_cache()

    @property
    def available(self) -> bool:
        return self.provider is not None

    def _require_provider(self) -> ApifyJobSearch:
        if self.provider is None:
            raise DependencyUnavailableError(
                "job search",
                message="Job search is not configured on this deployment.",
            )
        return self.provider

    async def start(self, query: SearchQuery) -> dict[str, Any]:
        """Begin a search, or hand back one already answered."""
        provider = self._require_provider()

        if not [title for title in query.titles if title.strip()]:
            raise ValidationError(
                "Say what you are looking for.",
                details=[{"field": "titles", "message": "Required"}],
            )

        cached_id = await self.cache.get(f"{_QUERY_PREFIX}{query.cache_key()}")
        if cached_id:
            # Already paid for. Return the same handle rather than run again.
            return {"search_id": str(cached_id), "status": SearchStatus.SUCCEEDED.value}

        run = await provider.start(query)
        if run.status is SearchStatus.FAILED:
            raise DependencyUnavailableError(
                "job search", message=run.error or "The search could not be started."
            )

        await self.cache.set(
            f"{_RUN_PREFIX}{run.id}",
            {
                "cache_key": query.cache_key(),
                "workplace_type": query.workplace_type.value if query.workplace_type else None,
            },
            ttl=settings.JOB_SEARCH_CACHE_SECONDS,
        )
        return {"search_id": run.id, "status": run.status.value}

    async def collect(self, search_id: str) -> SearchRun:
        """Where a search has got to, with results once it is done."""
        provider = self._require_provider()

        results = await self.cache.get(f"jobsearch:results:{search_id}")
        if results is not None:
            return SearchRun(
                id=search_id,
                status=SearchStatus.SUCCEEDED,
                results=[_result_from(row) for row in results],
            )

        meta = await self.cache.get(f"{_RUN_PREFIX}{search_id}") or {}
        asked = meta.get("workplace_type")
        run = await provider.collect(search_id, asked_for=WorkplaceType(asked) if asked else None)

        if run.status is SearchStatus.SUCCEEDED:
            await self.cache.set(
                f"jobsearch:results:{search_id}",
                [asdict(item) for item in run.results],
                ttl=settings.JOB_SEARCH_CACHE_SECONDS,
            )
            if meta.get("cache_key"):
                await self.cache.set(
                    f"{_QUERY_PREFIX}{meta['cache_key']}",
                    search_id,
                    ttl=settings.JOB_SEARCH_CACHE_SECONDS,
                )
        return run


def _result_from(row: dict[str, Any]) -> Any:
    from datetime import date

    from app.engines.jobsearch.provider import SearchResult

    posted = row.get("posted_at")
    if isinstance(posted, str):
        try:
            posted = date.fromisoformat(posted)
        except ValueError:
            posted = None

    return SearchResult(
        title=row["title"],
        company_name=row.get("company_name"),
        location=row.get("location"),
        country=row.get("country"),
        workplace_type=WorkplaceType(row.get("workplace_type", "UNKNOWN")),
        description=row.get("description"),
        url=row.get("url"),
        posted_at=posted,
        external_id=row.get("external_id"),
    )
