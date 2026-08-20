"""Phase 8: reverse matching and the zero-bench engine.

The load-bearing tests here are:

* a suggestion is ranked by whether the seat is *winnable*, not only by fit,
* the route is named, so HR can hand Sales something actionable,
* an unknown route is unknown — it must not silently discount a real option,
* milestones fire **once each, not daily**, which is the Phase 8 definition of
  done and the difference between an alert people act on and one they mute.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.core.permissions import Role
from app.engines.matching.bench import (
    SEVERITY_BY_MILESTONE,
    AlertSeverity,
    evaluate_resource,
    milestone_for,
)
from app.engines.matching.config import DEFAULT_THRESHOLDS
from app.engines.matching.reverse import (
    AccountView,
    RouteType,
    priority_for,
    requirement_is_open_for,
    resolve_route,
)

API = "/api/v1"
TODAY = date(2026, 6, 1)
MILESTONES = [90, 60, 30, 15, 7]


def account(**overrides) -> AccountView:
    base = {
        "id": uuid.uuid4(),
        "name": "Milaha",
        "account_type": "CUSTOMER",
        "relationship_status": "ACTIVE",
        "is_existing_customer": False,
        "is_existing_partner": False,
        "is_approved_vendor": False,
        "has_msa": False,
        "contract_outsourcing_friendly": False,
    }
    base.update(overrides)
    return AccountView(**base)


def route(account_view=None, via=None, preferred=False):
    return resolve_route(
        account=account_view, via=via, via_is_preferred=preferred, thresholds=DEFAULT_THRESHOLDS
    )


# ------------------------------------------------------------------- routing


class TestRouteResolution:
    def test_an_existing_customer_with_an_msa_is_the_most_reachable(self):
        resolved = route(account(is_existing_customer=True, has_msa=True))

        assert resolved.route_type is RouteType.DIRECT
        assert resolved.reachability == 1.0
        assert "MSA" in (resolved.evidence or "")

    def test_a_direct_relationship_beats_a_partner_route(self):
        direct = route(account(is_existing_customer=True), via=account(name="Prime X"))
        assert direct.route_type is RouteType.DIRECT
        assert "Prime X" not in direct.label

    def test_a_partner_route_is_named_so_sales_can_act_on_it(self):
        resolved = route(account(name="Ooredoo"), via=account(name="Prime X"))

        assert resolved.route_type is RouteType.VIA_PARTNER
        assert resolved.via_account_name == "Prime X"
        assert "Ooredoo" in resolved.label and "Prime X" in resolved.label

    def test_a_prime_contractor_route_is_labelled_as_one(self):
        resolved = route(
            account(name="Qatar Energy"),
            via=account(name="Prime X", account_type="PRIME_CONTRACTOR"),
        )
        assert resolved.route_type is RouteType.VIA_PRIME

    def test_a_preferred_route_outranks_a_merely_known_one(self):
        preferred = route(account(), via=account(name="Prime X"), preferred=True)
        known = route(account(), via=account(name="Prime X"), preferred=False)

        assert preferred.reachability > known.reachability

    def test_a_blocked_account_is_all_but_unreachable(self):
        resolved = route(account(relationship_status="BLOCKED", is_existing_customer=True))

        assert resolved.route_type is RouteType.NO_KNOWN_ROUTE
        assert resolved.reachability == DEFAULT_THRESHOLDS["reachability_blocked"]
        assert resolved.is_blocked

    def test_an_account_with_no_recorded_route_is_scored_low_not_hidden(self):
        resolved = route(account(name="Cold Prospect"))

        assert resolved.route_type is RouteType.NO_KNOWN_ROUTE
        assert resolved.reachability == DEFAULT_THRESHOLDS["reachability_no_route"]
        assert "no route recorded" in resolved.label

    def test_no_account_at_all_is_unknown_not_unreachable(self):
        resolved = route(None)

        assert resolved.route_type is RouteType.UNKNOWN
        assert resolved.reachability is None, "unknown must never be scored as zero"


class TestPriority:
    def test_priority_discounts_the_match_by_reachability(self):
        assert priority_for(90, 0.5) == 45.0
        assert priority_for(90, 1.0) == 90.0

    def test_an_unknown_route_does_not_discount_anything(self):
        # Guessing that an unrecorded account is unreachable would bury real
        # opportunities behind a data-entry gap.
        assert priority_for(90, None) == 90.0

    def test_a_reachable_good_match_outranks_an_unreachable_great_one(self):
        great_but_unreachable = priority_for(95, DEFAULT_THRESHOLDS["reachability_no_route"])
        good_and_direct = priority_for(78, DEFAULT_THRESHOLDS["reachability_direct_msa"])

        assert good_and_direct > great_but_unreachable


class TestRequirementSideFilters:
    def test_a_closed_requirement_is_never_suggested(self):
        included, reason = requirement_is_open_for(None, is_open=False, awaiting_review=False)
        assert not included
        assert reason

    def test_an_unreviewed_requirement_is_never_suggested(self):
        included, reason = requirement_is_open_for(None, is_open=True, awaiting_review=True)
        assert not included
        assert "review" in (reason or "").lower()

    def test_an_open_reviewed_requirement_passes(self):
        included, _ = requirement_is_open_for(None, is_open=True, awaiting_review=False)
        assert included


# ---------------------------------------------------------------- milestones


class TestBenchMilestones:
    @pytest.mark.parametrize(
        ("days", "expected"),
        [
            (120, None),
            (90, 90),
            (61, 90),
            (60, 60),
            (31, 60),
            (30, 30),
            (16, 30),
            (12, 15),
            (7, 7),
            (0, 7),
            (-2, None),
        ],
    )
    def test_the_tightest_milestone_reached_is_the_one_that_fires(self, days, expected):
        assert milestone_for(days, MILESTONES) == expected

    def test_a_missed_sweep_day_does_not_lose_the_milestone(self):
        # A worker outage means a resource is first seen at 12 days, not 15.
        # It must still get the 15-day alert rather than skipping to 7.
        assert milestone_for(12, MILESTONES) == 15

    def test_severity_escalates_as_the_runway_shortens(self):
        assert SEVERITY_BY_MILESTONE[90] is AlertSeverity.INFO
        assert SEVERITY_BY_MILESTONE[30] is AlertSeverity.WARNING
        assert SEVERITY_BY_MILESTONE[7] is AlertSeverity.CRITICAL

    def test_a_deployed_consultant_approaching_the_end_produces_a_milestone(self):
        milestone = evaluate_resource(
            resource_id="r1",
            available_from=TODAY + timedelta(days=30),
            availability_status="DEPLOYED",
            today=TODAY,
            milestones=MILESTONES,
        )
        assert milestone is not None
        assert milestone.milestone_days == 30
        assert milestone.severity is AlertSeverity.WARNING

    def test_somebody_already_available_is_not_a_milestone(self):
        # They are on the bench, not approaching it. That is the radar's job.
        assert (
            evaluate_resource(
                resource_id="r1",
                available_from=TODAY,
                availability_status="AVAILABLE",
                today=TODAY,
                milestones=MILESTONES,
            )
            is None
        )

    def test_no_end_date_means_no_milestone(self):
        assert (
            evaluate_resource(
                resource_id="r1",
                available_from=None,
                availability_status="DEPLOYED",
                today=TODAY,
                milestones=MILESTONES,
            )
            is None
        )

    def test_the_dedupe_key_is_stable_across_consecutive_sweeps(self):
        """The heart of "fires once, not daily"."""
        ends = TODAY + timedelta(days=30)
        first = evaluate_resource(
            resource_id="r1",
            available_from=ends,
            availability_status="DEPLOYED",
            today=TODAY,
            milestones=MILESTONES,
        )
        tomorrow = evaluate_resource(
            resource_id="r1",
            available_from=ends,
            availability_status="DEPLOYED",
            today=TODAY + timedelta(days=1),
            milestones=MILESTONES,
        )
        assert first is not None and tomorrow is not None
        assert first.dedupe_key == tomorrow.dedupe_key

    def test_a_tighter_milestone_gets_its_own_key(self):
        ends = TODAY + timedelta(days=30)
        at_30 = evaluate_resource(
            resource_id="r1",
            available_from=ends,
            availability_status="DEPLOYED",
            today=TODAY,
            milestones=MILESTONES,
        )
        at_7 = evaluate_resource(
            resource_id="r1",
            available_from=ends,
            availability_status="DEPLOYED",
            today=TODAY + timedelta(days=25),
            milestones=MILESTONES,
        )
        assert at_30 is not None and at_7 is not None
        assert at_30.dedupe_key != at_7.dedupe_key, "each milestone must alert once"


# ------------------------------------------------------------------ API level


@pytest.fixture
async def scenario(as_role):
    """An addressable account, an open requirement, and a consultant rolling off."""
    sales_client, _ = await as_role(Role.SALES)

    customer = (
        await sales_client.post(
            f"{API}/accounts",
            json={
                "name": f"Milaha Test {uuid.uuid4().hex[:6]}",
                "account_type": "CUSTOMER",
                "country": "QA",
                "relationship_status": "ACTIVE",
                "is_existing_customer": True,
                "has_msa": True,
            },
        )
    ).json()

    requirement = (
        await sales_client.post(
            f"{API}/requirements",
            json={
                "title": f"Reverse Requirement {uuid.uuid4().hex[:6]}",
                "role": "SAP FICO Consultant",
                "positions": 1,
                "priority_source": "P1_EXISTING_CUSTOMER",
                "account_id": customer["id"],
                "country": "QA",
                "location": "Doha",
                "work_mode": "ONSITE",
                "experience_min_years": 5,
                "rate_max": "22000",
                "rate_currency": "QAR",
                "rate_unit": "MONTHLY",
                "skills": [{"name": "SAP FICO", "importance": "MANDATORY", "min_years": 5}],
            },
        )
    ).json()

    hr_client, _ = await as_role(Role.HR_RESOURCING)
    resource = (
        await hr_client.post(
            f"{API}/resources",
            json={
                "full_name": f"Rolling Off {uuid.uuid4().hex[:6]}",
                "resource_type": "CONSULTANT",
                "availability_status": "DEPLOYED",
                "available_from": (date.today() + timedelta(days=30)).isoformat(),
                "notice_period_days": 0,
                "total_experience_years": 9,
                "current_location_country": "QA",
                "current_location_city": "Doha",
                "expected_cost_amount": "15000",
                "expected_cost_currency": "QAR",
                "expected_cost_unit": "MONTHLY",
                "skills": [
                    {"name": "SAP FICO", "years": 8, "is_primary": True, "last_used_year": 2026}
                ],
            },
        )
    ).json()

    return hr_client, resource, requirement, customer


class TestReverseMatchingEndpoints:
    async def test_running_produces_ranked_suggestions(self, scenario):
        client, resource, requirement, _ = scenario

        response = await client.post(
            f"{API}/reverse-matching/resources/{resource['id']}/run?limit=50"
        )
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["total"] >= 1
        priorities = [item["priority_score"] for item in body["suggestions"]]
        assert priorities == sorted(priorities, reverse=True)
        assert requirement["id"] in [item["requirement_id"] for item in body["suggestions"]]

    async def test_every_suggestion_names_its_route(self, scenario):
        client, resource, requirement, customer = scenario
        body = (
            await client.post(f"{API}/reverse-matching/resources/{resource['id']}/run?limit=50")
        ).json()

        # Whatever its rank, every suggestion resolves a route.
        assert all(item["route"]["route_type"] for item in body["suggestions"])

        # This scenario's account is an existing customer with an MSA, so its
        # own requirement must come back as a fully reachable direct route.
        mine = next(
            item for item in body["suggestions"] if item["requirement_id"] == requirement["id"]
        )
        assert mine["route"]["route_type"] == "DIRECT"
        assert customer["name"] in mine["route"]["label"]
        assert mine["route"]["reachability"] == 1.0

    async def test_every_suggestion_carries_its_explanation(self, scenario):
        client, resource, _, _ = scenario
        body = (await client.post(f"{API}/reverse-matching/resources/{resource['id']}/run")).json()

        for suggestion in body["suggestions"]:
            assert suggestion["components"], "a bare number is not a suggestion"
            assert suggestion["narrative"]
            assert suggestion["requirement_title"]

    async def test_reading_returns_the_stored_snapshot(self, scenario):
        client, resource, _, _ = scenario
        await client.post(f"{API}/reverse-matching/resources/{resource['id']}/run")

        body = (await client.get(f"{API}/reverse-matching/resources/{resource['id']}")).json()
        assert body["computed_at"] is not None
        assert body["resource_name"] == resource["full_name"]
        assert body["available_from"] == resource["available_from"]

    async def test_nothing_is_computed_until_it_is_asked_for(self, scenario):
        client, resource, _, _ = scenario
        body = (await client.get(f"{API}/reverse-matching/resources/{resource['id']}")).json()

        assert body["computed_at"] is None
        assert body["suggestions"] == []

    async def test_rerunning_replaces_rather_than_duplicates(self, scenario):
        client, resource, _, _ = scenario
        first = (await client.post(f"{API}/reverse-matching/resources/{resource['id']}/run")).json()
        second = (
            await client.post(f"{API}/reverse-matching/resources/{resource['id']}/run")
        ).json()

        assert first["total"] == second["total"]
        ids = [item["requirement_id"] for item in second["suggestions"]]
        assert len(ids) == len(set(ids))

    async def test_an_unknown_resource_is_a_404(self, scenario):
        client, _, _, _ = scenario
        response = await client.post(f"{API}/reverse-matching/resources/{uuid.uuid4()}/run")
        assert response.status_code == 404

    async def test_the_run_is_audited(self, scenario, as_role):
        client, resource, _, _ = scenario
        await client.post(f"{API}/reverse-matching/resources/{resource['id']}/run")

        admin, _ = await as_role(Role.ADMIN)
        entries = (await admin.get(f"{API}/audit?action=REVERSE_MATCH_GENERATED")).json()
        assert entries["items"]

    async def test_forward_and_reverse_score_the_same_pair_identically(self, scenario, as_role):
        """Two numbers for one pair would be indefensible to a client."""
        client, resource, _, _ = scenario
        reverse = (
            await client.post(f"{API}/reverse-matching/resources/{resource['id']}/run?limit=50")
        ).json()
        assert reverse["suggestions"], "the consultant must have somewhere to go"

        # Take the pair from the reverse ranking rather than assuming this
        # scenario's own requirement placed in the top N — the suite shares a
        # database and other files add demand.
        mine = reverse["suggestions"][0]

        sales, _ = await as_role(Role.SALES)
        forward = (
            await sales.post(f"{API}/matching/requirements/{mine['requirement_id']}/run?limit=100")
        ).json()
        theirs = next(item for item in forward["matches"] if item["resource_id"] == resource["id"])

        assert mine["overall_score"] == theirs["overall_score"]
        assert mine["band"] == theirs["band"]


class TestBenchRadar:
    async def test_the_radar_lists_people_approaching_the_bench(self, scenario):
        client, resource, _, _ = scenario
        body = (await client.get(f"{API}/reverse-matching/bench-radar")).json()

        rows = {row["resource_id"]: row for row in body["rows"]}
        assert resource["id"] in rows
        assert rows[resource["id"]]["days_until_available"] == 30

    async def test_it_counts_capacity_with_nowhere_to_go(self, scenario):
        client, resource, _, _ = scenario
        before = (await client.get(f"{API}/reverse-matching/bench-radar")).json()
        assert before["without_a_suggestion"] >= 1

        await client.post(f"{API}/reverse-matching/resources/{resource['id']}/run")
        after = (await client.get(f"{API}/reverse-matching/bench-radar")).json()

        row = next(r for r in after["rows"] if r["resource_id"] == resource["id"])
        assert row["top_suggestion"] is not None
        assert after["without_a_suggestion"] < before["without_a_suggestion"]

    async def test_the_horizon_is_respected(self, scenario):
        client, resource, _, _ = scenario
        near = (await client.get(f"{API}/reverse-matching/bench-radar?days_ahead=7")).json()
        assert resource["id"] not in [row["resource_id"] for row in near["rows"]]

    async def test_soonest_first(self, scenario):
        client, _, _, _ = scenario
        body = (await client.get(f"{API}/reverse-matching/bench-radar?days_ahead=365")).json()

        days = [
            row["days_until_available"]
            for row in body["rows"]
            if row["days_until_available"] is not None
        ]
        assert days == sorted(days)


class TestBenchSweep:
    async def test_the_sweep_raises_an_alert_for_a_milestone(self, scenario):
        client, _, _, _ = scenario
        result = (await client.post(f"{API}/reverse-matching/bench-sweep")).json()

        assert result["examined"] >= 1
        assert result["raised"] >= 1

    async def test_a_milestone_fires_once_not_daily(self, scenario):
        """The Phase 8 definition of done."""
        client, _, _, _ = scenario

        first = (await client.post(f"{API}/reverse-matching/bench-sweep")).json()
        second = (await client.post(f"{API}/reverse-matching/bench-sweep")).json()

        assert first["raised"] >= 1
        assert second["raised"] == 0
        assert second["skipped_duplicate"] >= first["raised"]

    async def test_the_sweep_is_audited(self, scenario, as_role):
        client, _, _, _ = scenario
        await client.post(f"{API}/reverse-matching/bench-sweep")

        admin, _ = await as_role(Role.ADMIN)
        entries = (await admin.get(f"{API}/audit?action=BENCH_SWEEP_RUN")).json()
        assert entries["items"]


class TestReverseMatchingAuthorization:
    async def test_management_may_read_but_not_run(self, scenario, as_role):
        _, resource, _, _ = scenario
        client, _ = await as_role(Role.MANAGEMENT)

        assert (
            await client.post(f"{API}/reverse-matching/resources/{resource['id']}/run")
        ).status_code == 403
        assert (
            await client.get(f"{API}/reverse-matching/resources/{resource['id']}")
        ).status_code == 200

    async def test_sales_may_read_but_not_run_the_redeployment_engine(self, scenario, as_role):
        # Redeployment is Resourcing's job; Sales sees the output.
        _, resource, _, _ = scenario
        client, _ = await as_role(Role.SALES)

        assert (
            await client.get(f"{API}/reverse-matching/resources/{resource['id']}")
        ).status_code == 200
        assert (
            await client.post(f"{API}/reverse-matching/resources/{resource['id']}/run")
        ).status_code == 403

    async def test_a_role_without_margin_sight_is_told_what_was_withheld(self, scenario):
        client, resource, _, _ = scenario
        body = (await client.post(f"{API}/reverse-matching/resources/{resource['id']}/run")).json()

        top = body["suggestions"][0]
        keys = {component["key"] for component in top["components"]}
        assert "cost" not in keys
        assert top["restricted_components"]
