"""Job search.

Search is browsing, not collecting. A result is not put into anybody's feed
until they choose to keep it — otherwise one careless query would bury a
person's feed under fifty jobs they never asked for.

Results are cached by query, because a run costs real money and takes around a
minute. Asking the same question twice within the window is free and instant.

**Why "thorough" exists.** The actor only echoes `workType` on a search that
filtered by it, so an unfiltered search returns every row as UNKNOWN — and a
Remote/Hybrid/Onsite filter over those results would match nothing. A thorough
search asks the three arrangements separately and merges, so every row carries
a stated arrangement. It costs three runs instead of one, which is why it is a
choice the caller makes and not the default behaviour of an ordinary search.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from typing import Any

from app.core.cache import get_cache
from app.core.config import JobSearchProvider, settings
from app.core.errors import DependencyUnavailableError, ValidationError
from app.core.logging import get_logger
from app.engines.jobsearch.apify import ApifyJobSearch
from app.engines.jobsearch.provider import SearchQuery, SearchResult, SearchRun, SearchStatus
from app.models.jobfeed import WorkplaceType

logger = get_logger("jobsearch")

_RUN_PREFIX = "jobsearch:run:"
_QUERY_PREFIX = "jobsearch:query:"

#: A fanned-out search is one handle over several provider runs.
_FAN_PREFIX = "fan:"

#: The arrangements a thorough search asks for separately. UNKNOWN is absent
#: on purpose: it is the absence of an answer, not something to ask for.
_FAN_ARRANGEMENTS = (WorkplaceType.REMOTE, WorkplaceType.HYBRID, WorkplaceType.ONSITE)


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

    async def start(self, query: SearchQuery, *, thorough: bool = False) -> dict[str, Any]:
        """Begin a search, or hand back one already answered.

        `thorough` only means anything when no arrangement was asked for: with
        one chosen, the single run already returns a stated arrangement.
        """
        provider = self._require_provider()

        if not [title for title in query.titles if title.strip()]:
            raise ValidationError(
                "Say what you are looking for.",
                details=[{"field": "titles", "message": "Required"}],
            )

        if thorough and query.workplace_type is None:
            return await self._start_fanned(query)

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

    async def _start_fanned(self, query: SearchQuery) -> dict[str, Any]:
        """Ask the three arrangements at once and hand back one handle.

        Runs are started concurrently: they are independent, and starting them
        in sequence would add three round trips to a wait that is already the
        slowest part of the feature.
        """
        provider = self._require_provider()
        fan_key = f"{_QUERY_PREFIX}fan:{query.cache_key()}"

        cached_id = await self.cache.get(fan_key)
        if cached_id:
            return {"search_id": str(cached_id), "status": SearchStatus.SUCCEEDED.value}

        variants = [replace(query, workplace_type=arrangement) for arrangement in _FAN_ARRANGEMENTS]
        runs = await asyncio.gather(
            *(provider.start(variant) for variant in variants), return_exceptions=True
        )

        started: list[tuple[str, WorkplaceType]] = []
        for arrangement, run in zip(_FAN_ARRANGEMENTS, runs, strict=True):
            if isinstance(run, BaseException) or run.status is SearchStatus.FAILED:
                # One arrangement failing is a thinner result set, not a failed
                # search. Only all three failing is a failure.
                logger.warning("fan_variant_failed", arrangement=arrangement.value)
                continue
            started.append((run.id, arrangement))

        if not started:
            raise DependencyUnavailableError(
                "job search", message="The search could not be started."
            )

        for run_id, arrangement in started:
            await self.cache.set(
                f"{_RUN_PREFIX}{run_id}",
                {"cache_key": None, "workplace_type": arrangement.value},
                ttl=settings.JOB_SEARCH_CACHE_SECONDS,
            )

        search_id = _FAN_PREFIX + ",".join(run_id for run_id, _ in started)
        await self.cache.set(
            f"{_RUN_PREFIX}{search_id}",
            {"cache_key": fan_key, "members": [run_id for run_id, _ in started]},
            ttl=settings.JOB_SEARCH_CACHE_SECONDS,
        )
        return {"search_id": search_id, "status": SearchStatus.RUNNING.value}

    async def _collect_fanned(self, search_id: str) -> SearchRun:
        """Merge the members. Still RUNNING until every one of them is done."""
        members = search_id[len(_FAN_PREFIX) :].split(",")
        runs = await asyncio.gather(
            *(self._collect_single(member) for member in members), return_exceptions=True
        )

        collected = [run for run in runs if isinstance(run, SearchRun)]
        if not collected or all(run.status is SearchStatus.FAILED for run in collected):
            return SearchRun(
                id=search_id, status=SearchStatus.FAILED, error="The search did not complete."
            )
        if any(run.status is SearchStatus.RUNNING for run in collected):
            return SearchRun(id=search_id, status=SearchStatus.RUNNING)

        merged = _merge([item for run in collected for item in run.results])

        meta = await self.cache.get(f"{_RUN_PREFIX}{search_id}") or {}
        if merged and meta.get("cache_key"):
            await self.cache.set(
                meta["cache_key"], search_id, ttl=settings.JOB_SEARCH_CACHE_SECONDS
            )
        return SearchRun(id=search_id, status=SearchStatus.SUCCEEDED, results=merged)

    async def collect(self, search_id: str) -> SearchRun:
        """Where a search has got to, with results once it is done."""
        self._require_provider()
        if search_id.startswith(_FAN_PREFIX):
            return await self._collect_fanned(search_id)
        return await self._collect_single(search_id)

    async def _collect_single(self, search_id: str) -> SearchRun:
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
            # Fill blanks from the job text before caching, so a cache hit and
            # a fresh collect return the same rows.
            run.results = _with_inference(run.results)

        # An empty success is not cached. Apify reports a run SUCCEEDED before
        # its dataset is necessarily readable, so the first collect after a
        # finish can legitimately come back with nothing -- and caching that
        # pins an empty result set for the whole window, which is how a
        # working search appears to return nothing for fifteen minutes.
        if run.status is SearchStatus.SUCCEEDED and run.results:
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
        workplace_inferred=bool(row.get("workplace_inferred", False)),
    )


def _identity(item: SearchResult) -> tuple[str, str, str]:
    """What makes two rows the same job across arrangements."""
    return (
        (item.title or "").strip().lower(),
        (item.company_name or "").strip().lower(),
        (item.location or "").strip().lower(),
    )


def _merge(items: list[SearchResult]) -> list[SearchResult]:
    """One row per job, newest first, preferring a stated arrangement.

    The same vacancy can come back from more than one arrangement run when a
    listing is tagged loosely. Keeping both would show it twice, and picking
    arbitrarily would sometimes drop the stated arrangement for an inferred
    one, so a stated row always wins.
    """
    best: dict[tuple[str, str, str], SearchResult] = {}
    for item in items:
        key = _identity(item)
        held = best.get(key)
        if held is None:
            best[key] = item
            continue
        held_known = (
            held.workplace_type is not WorkplaceType.UNKNOWN and not held.workplace_inferred
        )
        item_known = (
            item.workplace_type is not WorkplaceType.UNKNOWN and not item.workplace_inferred
        )
        if item_known and not held_known:
            best[key] = item

    return sorted(
        best.values(),
        key=lambda row: (row.posted_at is not None, row.posted_at),
        reverse=True,
    )


def _with_inference(items: list[SearchResult]) -> list[SearchResult]:
    """Fill UNKNOWN arrangements from the job text, flagged as inferred.

    Only ever fills a blank: a provider-stated arrangement is never replaced by
    a guess. Roughly a quarter of unfiltered rows say it somewhere in the title
    or description, which is worth having as long as the UI admits which is
    which.
    """
    from app.services.jobfeed import classify_workplace

    filled: list[SearchResult] = []
    for item in items:
        if item.workplace_type is WorkplaceType.UNKNOWN:
            guess = classify_workplace(" ".join(filter(None, [item.title, item.description])))
            if guess is not WorkplaceType.UNKNOWN:
                item = replace(item, workplace_type=guess, workplace_inferred=True)
        filled.append(item)
    return filled
