"""Apify adapter for job search.

Two findings from testing the live actor shaped this file, and both would have
produced a broken feature if assumed rather than checked.

**`workType` is only populated when you filter by it.** An unfiltered search
returns the field empty on every row, including genuinely remote jobs. So the
arrangement cannot be read from the response and then filtered client-side --
that would make the Remote filter return nothing at all. The filter is pushed
down to the actor, and the arrangement comes from what was asked for.

**An unfiltered result has no stated arrangement.** It is recorded as UNKNOWN
rather than guessed, which is why UNKNOWN is a first-class value everywhere
else in this feature.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.engines.jobsearch.provider import (
    SearchQuery,
    SearchResult,
    SearchRun,
    SearchStatus,
)
from app.models.jobfeed import WorkplaceType

logger = get_logger("jobsearch.apify")

_BASE = "https://api.apify.com/v2"

#: LinkedIn's own codes, confirmed against the actor's input schema.
_WORKPLACE_TO_CODE: dict[WorkplaceType, str] = {
    WorkplaceType.ONSITE: "1",
    WorkplaceType.REMOTE: "2",
    WorkplaceType.HYBRID: "3",
}
_CODE_TO_WORKPLACE = {code: workplace for workplace, code in _WORKPLACE_TO_CODE.items()}

#: What the actor echoes back in `workType` once a filter is applied.
_LABEL_TO_WORKPLACE: dict[str, WorkplaceType] = {
    "on-site": WorkplaceType.ONSITE,
    "onsite": WorkplaceType.ONSITE,
    "remote": WorkplaceType.REMOTE,
    "hybrid": WorkplaceType.HYBRID,
}

#: Apify run states that mean "still going".
_PENDING = {"READY", "RUNNING"}


def _country_from(location: str | None) -> str | None:
    """Best effort two-letter code from "Doha, Doha, Qatar".

    Deliberately small: a wrong country is worse than no country, so only
    names we are sure of are mapped.
    """
    if not location:
        return None
    known = {
        "qatar": "QA",
        "united arab emirates": "AE",
        "saudi arabia": "SA",
        "kuwait": "KW",
        "bahrain": "BH",
        "oman": "OM",
        "india": "IN",
        "united kingdom": "GB",
        "united states": "US",
    }
    tail = location.split(",")[-1].strip().lower()
    return known.get(tail)


def _posted_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


class ApifyJobSearch:
    """Calls one Apify actor. Which actor is configuration."""

    name = "apify"

    def __init__(self, token: str | None = None, actor: str | None = None) -> None:
        self.token = token or settings.APIFY_API_TOKEN
        # Apify addresses actors as owner~name in URLs.
        self.actor = (actor or settings.APIFY_JOBS_ACTOR).replace("/", "~")
        self._resolved_actor_id: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    def build_input(self, query: SearchQuery) -> dict[str, Any]:
        """Translate our query into the actor's vocabulary."""
        payload: dict[str, Any] = {
            "titles": [title.strip() for title in query.titles if title.strip()],
            "rows": min(query.rows, settings.JOB_SEARCH_MAX_ROWS),
        }
        if query.locations:
            payload["locations"] = [item.strip() for item in query.locations if item.strip()]
        if query.workplace_type and query.workplace_type in _WORKPLACE_TO_CODE:
            # Pushed down rather than filtered on return: the response field is
            # empty unless the search itself was filtered.
            payload["workTypes"] = [_WORKPLACE_TO_CODE[query.workplace_type]]
        if query.posted_within:
            payload["publishedAt"] = query.posted_within
        return payload

    def to_result(self, row: dict[str, Any], asked_for: WorkplaceType | None) -> SearchResult:
        """One actor row in the shape the feed stores."""
        label = str(row.get("workType") or "").strip().lower()
        workplace = _LABEL_TO_WORKPLACE.get(label)
        if workplace is None:
            # The actor echoes the arrangement only on a filtered search. On an
            # unfiltered one nothing was stated, and nothing is invented.
            workplace = asked_for or WorkplaceType.UNKNOWN

        location = row.get("location") or None
        return SearchResult(
            title=str(row.get("title") or "").strip(),
            company_name=(row.get("companyName") or None),
            location=location,
            country=_country_from(location),
            workplace_type=workplace,
            description=(row.get("description") or None),
            url=(row.get("jobUrl") or None),
            posted_at=_posted_date(row.get("publishedAt")),
            external_id=(str(row["id"]) if row.get("id") else None),
        )

    async def _actor_id(self, client: httpx.AsyncClient) -> str | None:
        """Our actor's id, resolved once and remembered.

        Needed because a run reports the actor it belongs to by id, not by
        name, and the name is what configuration gives us.
        """
        if self._resolved_actor_id is not None:
            return self._resolved_actor_id or None
        response = await client.get(f"{_BASE}/acts/{self.actor}", headers=self._headers())
        if response.status_code >= 400:
            # Resolution failing must not break search; the guard is a
            # narrowing, not the thing that makes this safe.
            self._resolved_actor_id = ""
            return None
        self._resolved_actor_id = str(response.json()["data"]["id"])
        return self._resolved_actor_id

    async def start(self, query: SearchQuery) -> SearchRun:
        if not self.configured:
            return SearchRun(id="", status=SearchStatus.FAILED, error="No Apify token configured.")

        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{_BASE}/acts/{self.actor}/runs",
                headers=self._headers(),
                content=json.dumps(self.build_input(query)),
            )
        if response.status_code >= 400:
            logger.warning("apify_start_failed", status=response.status_code)
            return SearchRun(
                id="", status=SearchStatus.FAILED, error="The search could not be started."
            )

        data = response.json()["data"]
        logger.info("apify_search_started", run_id=data["id"], actor=self.actor)
        return SearchRun(id=str(data["id"]), status=SearchStatus.RUNNING)

    async def collect(self, run_id: str, asked_for: WorkplaceType | None = None) -> SearchRun:
        if not self.configured:
            return SearchRun(id=run_id, status=SearchStatus.FAILED, error="Not configured.")

        async with httpx.AsyncClient(timeout=60) as client:
            run = await client.get(f"{_BASE}/actor-runs/{run_id}", headers=self._headers())
            if run.status_code >= 400:
                return SearchRun(
                    id=run_id, status=SearchStatus.FAILED, error="That search was not found."
                )

            data = run.json()["data"]

            # A run id is opaque, but it is still a handle a caller supplies.
            # Refuse to read a dataset belonging to any other actor through our
            # token: this endpoint returns our job searches, not whatever else
            # the account happens to have run.
            actor_id = await self._actor_id(client)
            if actor_id and str(data.get("actId") or "") != actor_id:
                logger.warning("apify_run_wrong_actor", run_id=run_id)
                return SearchRun(
                    id=run_id, status=SearchStatus.FAILED, error="That search was not found."
                )

            status = str(data.get("status") or "")
            if status in _PENDING:
                return SearchRun(id=run_id, status=SearchStatus.RUNNING)
            if status != "SUCCEEDED":
                logger.warning("apify_run_unsuccessful", run_id=run_id, status=status)
                return SearchRun(
                    id=run_id, status=SearchStatus.FAILED, error="The search did not complete."
                )

            dataset_id = data.get("defaultDatasetId")
            if not dataset_id:
                return SearchRun(id=run_id, status=SearchStatus.SUCCEEDED, results=[])

            items = await client.get(
                f"{_BASE}/datasets/{dataset_id}/items",
                headers=self._headers(),
                params={"clean": "true", "limit": settings.JOB_SEARCH_MAX_ROWS},
            )
            rows = items.json() if items.status_code < 400 else []

        results = [self.to_result(row, asked_for) for row in rows if row.get("title")]
        logger.info("apify_search_collected", run_id=run_id, results=len(results))
        return SearchRun(id=run_id, status=SearchStatus.SUCCEEDED, results=results)
