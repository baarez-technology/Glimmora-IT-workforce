"""Job search.

The tests that matter here are about the provider's quirk: `workType` comes
back empty unless the search itself was filtered. Reading the arrangement from
the response and filtering on it afterwards would make the Remote filter return
nothing, and nothing in the type system would have caught it.

No network: the adapter's translation is pure and tested directly.
"""

from __future__ import annotations

import pytest

from app.core.config import JobSearchProvider, settings
from app.engines.jobsearch.apify import ApifyJobSearch, _country_from, _posted_date
from app.engines.jobsearch.provider import SearchQuery
from app.models.jobfeed import WorkplaceType
from app.services.jobsearch import provider_for

API = "/api/v1"


def row(**overrides):
    """A row shaped like the actor's real output."""
    payload = {
        "id": "4424248218",
        "title": "Senior Backend Engineer",
        "companyName": "Snoonu",
        "location": "Doha, Doha, Qatar",
        "jobUrl": "https://www.linkedin.com/jobs/view/4424248218",
        "publishedAt": "2026-09-20",
        "description": "Backend services in .NET Core.",
        "workType": "",
    }
    payload.update(overrides)
    return payload


class TestQueryTranslation:
    """Our vocabulary into the actor's."""

    def setup_method(self):
        self.adapter = ApifyJobSearch(token="test-token")

    def test_the_arrangement_is_pushed_down_to_the_provider(self):
        """The whole reason the filter works.

        The response field is empty on an unfiltered search, so filtering after
        the fact would return nothing. The filter has to go in the request.
        """
        built = self.adapter.build_input(
            SearchQuery(titles=["Engineer"], workplace_type=WorkplaceType.REMOTE)
        )
        assert built["workTypes"] == ["2"]

    @pytest.mark.parametrize(
        ("workplace", "code"),
        [
            (WorkplaceType.ONSITE, "1"),
            (WorkplaceType.REMOTE, "2"),
            (WorkplaceType.HYBRID, "3"),
        ],
    )
    def test_each_arrangement_maps_to_linkedins_code(self, workplace, code):
        built = self.adapter.build_input(SearchQuery(titles=["Engineer"], workplace_type=workplace))
        assert built["workTypes"] == [code]

    def test_unknown_is_never_sent_as_a_filter(self):
        """There is no code for "not stated" -- asking for it would return nothing."""
        built = self.adapter.build_input(
            SearchQuery(titles=["Engineer"], workplace_type=WorkplaceType.UNKNOWN)
        )
        assert "workTypes" not in built

    def test_rows_are_capped_by_configuration_not_by_the_caller(self):
        built = self.adapter.build_input(SearchQuery(titles=["Engineer"], rows=10_000))
        assert built["rows"] == settings.JOB_SEARCH_MAX_ROWS

    def test_blank_titles_are_dropped(self):
        built = self.adapter.build_input(SearchQuery(titles=["Engineer", "  ", ""]))
        assert built["titles"] == ["Engineer"]


class TestResultTranslation:
    def setup_method(self):
        self.adapter = ApifyJobSearch(token="test-token")

    def test_an_unfiltered_result_is_unknown_not_onsite(self):
        """The failure this prevents.

        The actor returns workType="" for every row of an unfiltered search,
        including genuinely remote ones. Treating empty as onsite would
        mislabel the lot.
        """
        result = self.adapter.to_result(row(workType=""), asked_for=None)
        assert result.workplace_type is WorkplaceType.UNKNOWN

    def test_a_filtered_result_carries_the_arrangement_asked_for(self):
        result = self.adapter.to_result(row(workType=""), asked_for=WorkplaceType.REMOTE)
        assert result.workplace_type is WorkplaceType.REMOTE

    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Remote", WorkplaceType.REMOTE),
            ("Hybrid", WorkplaceType.HYBRID),
            ("On-site", WorkplaceType.ONSITE),
            ("onsite", WorkplaceType.ONSITE),
        ],
    )
    def test_a_stated_arrangement_wins(self, label, expected):
        result = self.adapter.to_result(row(workType=label), asked_for=None)
        assert result.workplace_type is expected

    def test_the_job_is_mapped_into_the_feed_shape(self):
        result = self.adapter.to_result(row(), asked_for=None)
        assert result.title == "Senior Backend Engineer"
        assert result.company_name == "Snoonu"
        assert result.country == "QA"
        assert result.url.endswith("4424248218")
        assert result.posted_at.isoformat() == "2026-09-20"


class TestCountry:
    def test_a_known_country_resolves(self):
        assert _country_from("Doha, Doha, Qatar") == "QA"
        assert _country_from("London, England, United Kingdom") == "GB"

    def test_an_unknown_country_is_left_empty_rather_than_guessed(self):
        # A wrong country is worse than none: it would filter the job out of
        # somebody's results entirely.
        assert _country_from("Somewhere, Freedonia") is None
        assert _country_from(None) is None

    def test_an_unparseable_date_is_none(self):
        assert _posted_date("not a date") is None
        assert _posted_date(None) is None


class TestQueryIdentity:
    def test_the_same_question_has_the_same_key(self):
        """A run costs money, so an identical search must hit the cache."""
        a = SearchQuery(titles=["Engineer", "Developer"], locations=["Qatar"])
        b = SearchQuery(titles=["developer", "ENGINEER"], locations=["  qatar "])
        assert a.cache_key() == b.cache_key()

    def test_a_different_arrangement_is_a_different_search(self):
        a = SearchQuery(titles=["Engineer"], workplace_type=WorkplaceType.REMOTE)
        b = SearchQuery(titles=["Engineer"], workplace_type=WorkplaceType.ONSITE)
        assert a.cache_key() != b.cache_key()


class TestProviderSelection:
    def test_no_provider_when_none_is_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "JOB_SEARCH_PROVIDER", JobSearchProvider.NULL)
        assert provider_for() is None

    def test_no_provider_when_the_token_is_missing(self, monkeypatch):
        """Configured but unusable is the same as absent.

        Better the screen hides than offers a box that always fails.
        """
        monkeypatch.setattr(settings, "JOB_SEARCH_PROVIDER", JobSearchProvider.APIFY)
        monkeypatch.setattr(settings, "APIFY_API_TOKEN", None)
        assert provider_for() is None

    def test_the_actor_name_is_configuration(self, monkeypatch):
        """Swapping actors is an env change, never a code change."""
        monkeypatch.setattr(settings, "APIFY_JOBS_ACTOR", "someone/other-actor")
        assert ApifyJobSearch(token="t").actor == "someone~other-actor"


@pytest.mark.anyio
class TestSearchEndpoints:
    async def test_availability_is_reported_rather_than_guessed(self, client, make_user):
        from app.core.permissions import Role

        person = await make_user(Role.INDIVIDUAL)
        login = await client.post(
            f"{API}/auth/login",
            json={"email": person.email, "password": "Glimmora-Test-2026!"},
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        response = await client.get(f"{API}/job-feed/search/available")
        assert response.status_code == 200
        assert "available" in response.json()

    async def test_staff_cannot_reach_job_search(self, as_role):
        from app.core.permissions import Role

        sales, _ = await as_role(Role.SALES)
        assert (await sales.get(f"{API}/job-feed/search/available")).status_code == 403

    async def test_a_finished_search_serialises_its_results(self, client, make_user, monkeypatch):
        """The collect endpoint, with results actually in it.

        This went to production returning 500 for every poll. `SearchResult` is
        a slotted dataclass, so it has no `__dict__` and `vars()` raises -- and
        nothing exercised this line, because the only endpoint test here asked
        whether search was *available*, never collected a run carrying results.
        """
        from datetime import date

        from app.core.permissions import Role
        from app.engines.jobsearch.provider import SearchResult, SearchRun, SearchStatus
        from app.models.jobfeed import WorkplaceType
        from app.services.jobsearch import JobSearchService

        async def finished(self, search_id: str) -> SearchRun:
            return SearchRun(
                id=search_id,
                status=SearchStatus.SUCCEEDED,
                results=[
                    SearchResult(
                        title="Frontend Developer",
                        company_name="Acme",
                        location="Bengaluru, India",
                        country="IN",
                        workplace_type=WorkplaceType.REMOTE,
                        description="Build things.",
                        url="https://example.com/jobs/1",
                        posted_at=date(2026, 9, 30),
                        external_id="1",
                    )
                ],
            )

        monkeypatch.setattr(JobSearchService, "collect", finished)

        person = await make_user(Role.INDIVIDUAL)
        login = await client.post(
            f"{API}/auth/login",
            json={"email": person.email, "password": "Glimmora-Test-2026!"},
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        response = await client.get(f"{API}/job-feed/search/abc123")
        assert response.status_code == 200, response.text

        body = response.json()
        assert body["status"] == "SUCCEEDED"
        assert len(body["results"]) == 1

        result = body["results"][0]
        assert result["title"] == "Frontend Developer"
        assert result["workplace_type"] == "REMOTE"
        assert result["posted_at"] == "2026-09-30"
