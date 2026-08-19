"""Phase 7: the demand-to-resource matching engine.

The tests that matter most here are not "does it return a number". They are:

* is the number **reproducible** from the stored components,
* does a hard blocker (expired work permit, negative margin, missing mandatory
  skill) stop a high arithmetic score reading as a strong match,
* is unknown data reported as unknown rather than scored as zero, and
* does every response carry enough to explain itself to a client.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.permissions import Role
from app.engines.matching.config import (
    DEFAULT_MATCH_WEIGHTS,
    DEFAULT_THRESHOLDS,
    default_payload,
    validate_weights,
)
from app.engines.matching.engine import (
    MatchBand,
    RequirementView,
    ResourceView,
    apply_hard_filters,
    apply_suppressors,
    score_match,
    to_monthly,
)

API = "/api/v1"
TODAY = date(2026, 6, 1)

#: A CV that parses cleanly, so the profile it creates is genuinely awaiting
#: review rather than rejected at the door.
PENDING_CV = """Aisha Rahman
Senior SAP FICO Consultant
aisha.rahman@example.com | +974 5555 9876 | Doha, Qatar

PROFESSIONAL SUMMARY
SAP FICO consultant with S/4HANA finance rollouts across Gulf logistics and
energy clients, working with both onshore and offshore delivery teams.

EXPERIENCE

Senior SAP FICO Consultant, Northline Systems
Feb 2020 - Present
 - Led an S/4HANA finance migration for a Qatari logistics operator.

SAP FICO Consultant, Aurora Technology
Aug 2016 - Jan 2020

SKILLS
SAP FICO, SAP S/4HANA, SAP MM

Notice Period: 30 days
"""


# --------------------------------------------------------------- unit fixtures


def requirement_view(**overrides) -> RequirementView:
    base = {
        "id": uuid.uuid4(),
        "title": "SAP FICO Consultant",
        "mandatory_skills": ["SAP FICO", "SAP S/4HANA"],
        "preferred_skills": ["Power BI"],
        "required_years": {"SAP FICO": 5, "SAP S/4HANA": 3},
        "technologies": {"SAP"},
        "experience_min_years": 6,
        "country": "QA",
        "location": "Doha",
        "work_mode": "ONSITE",
        "start_by_date": TODAY + timedelta(days=30),
        "rate_max": 22000,
        "rate_unit": "MONTHLY",
        "positions": 1,
    }
    base.update(overrides)
    return RequirementView(**base)


def resource_view(**overrides) -> ResourceView:
    base = {
        "id": uuid.uuid4(),
        "full_name": "Test Consultant",
        "skills": {"SAP FICO": 8.0, "SAP S/4HANA": 4.0, "Power BI": 2.0},
        "skill_last_used": {"SAP FICO": 2026, "SAP S/4HANA": 2026},
        "primary_technologies": {"SAP"},
        "technologies": {"SAP"},
        "total_experience_years": 9.0,
        "country": "QA",
        "city": "Doha",
        "willing_to_relocate": False,
        "ready_from": TODAY + timedelta(days=10),
        "notice_period_days": 30,
        "available_from": None,
        "expected_cost": 15000,
        "expected_cost_unit": "MONTHLY",
        "work_authorisation_state": "VALID",
        "work_authorisation_days": 400,
        "needs_review": False,
        "availability_status": "AVAILABLE",
    }
    base.update(overrides)
    return ResourceView(**base)


def run(requirement=None, resource=None, **thresholds):
    merged = dict(DEFAULT_THRESHOLDS)
    merged.update(thresholds)
    return score_match(
        requirement or requirement_view(),
        resource or resource_view(),
        weights=DEFAULT_MATCH_WEIGHTS,
        thresholds=merged,
        today=TODAY,
    )


# --------------------------------------------------------------- the arithmetic


class TestScoreIsReproducible:
    def test_the_total_is_the_weighted_average_of_its_components(self):
        result = run()

        known = [c for c in result.components if c.is_known and c.weight > 0]
        expected = sum(c.score * c.weight for c in known) / sum(c.weight for c in known)

        assert result.overall_score == pytest.approx(round(expected, 1), abs=0.05)

    def test_every_component_reports_its_own_evidence(self):
        result = run()

        for component in result.components:
            if component.is_known:
                assert component.evidence or component.detail, component.key

    def test_a_strong_candidate_bands_strong(self):
        assert run().band is MatchBand.STRONG

    def test_scoring_is_deterministic(self):
        first, second = run(), run()
        assert first.overall_score == second.overall_score
        assert first.band is second.band


class TestUnknownIsNotZero:
    def test_an_unpriced_requirement_does_not_lower_the_score(self):
        priced = run()
        unpriced = run(requirement_view(rate_max=None, rate_unit=None))

        # Cost and commercial drop out of numerator *and* denominator, so a
        # requirement nobody has priced yet must not look like a bad fit.
        assert unpriced.overall_score >= priced.overall_score - 0.1
        assert "Cost fit" in unpriced.missing_information
        assert "Commercial fit" in unpriced.missing_information

    def test_missing_data_lowers_confidence_not_the_score(self):
        full = run()
        partial = run(requirement_view(rate_max=None, rate_unit=None))

        assert partial.confidence < full.confidence
        assert partial.confidence == pytest.approx(0.85, abs=0.01)

    def test_a_component_with_no_inputs_is_named_not_silently_dropped(self):
        result = run(resource=resource_view(total_experience_years=None))
        assert "Experience" in result.missing_information

    def test_a_resource_with_nothing_recorded_scores_zero_at_zero_confidence(self):
        empty = resource_view(
            skills={},
            skill_last_used={},
            technologies=set(),
            primary_technologies=set(),
            total_experience_years=None,
            country=None,
            city=None,
            ready_from=None,
            expected_cost=None,
            expected_cost_unit=None,
        )
        result = run(requirement_view(technologies=set()), empty)

        assert result.confidence < 0.5
        assert result.band is MatchBand.WEAK


class TestSuppressors:
    """A blocker must cap the headline, however good the arithmetic is."""

    def test_an_expired_work_permit_cannot_read_as_strong(self):
        result = run(resource=resource_view(work_authorisation_state="EXPIRED"))

        assert result.band is MatchBand.POSSIBLE
        assert result.overall_score > 80, "the score itself stays honest"
        assert result.suppressors == ["WORK_AUTH_EXPIRED"]
        assert any("expired" in warning.lower() for warning in result.warnings)

    def test_a_loss_making_placement_cannot_read_as_strong(self):
        result = run(resource=resource_view(expected_cost=26000))

        assert result.band is MatchBand.POSSIBLE
        assert "NEGATIVE_MARGIN" in result.suppressors
        assert any("lose money" in warning.lower() for warning in result.warnings)

    def test_a_suppressor_and_its_warning_are_not_both_listed(self):
        """One problem, one line. Two phrasings read as two problems."""
        expired = run(resource=resource_view(work_authorisation_state="EXPIRED"))
        assert len([w for w in expired.warnings if "expired" in w.lower()]) == 1

        loss_making = run(resource=resource_view(expected_cost=26000))
        cost_warnings = [
            w
            for w in loss_making.warnings
            if "above the client rate" in w.lower() or "lose money" in w.lower()
        ]
        assert len(cost_warnings) == 1

    def test_the_warning_list_leads_with_the_blocker(self):
        result = run(resource=resource_view(work_authorisation_state="EXPIRED"))
        assert "expired" in result.warnings[0].lower()

    def test_a_missing_mandatory_skill_caps_at_good(self):
        result = run(resource=resource_view(skills={"SAP S/4HANA": 6.0, "Power BI": 3.0}))

        assert result.band is not MatchBand.STRONG
        assert "SAP FICO" in result.gaps

    def test_suppressors_do_not_promote_a_weak_match(self):
        band, applied = apply_suppressors(
            MatchBand.WEAK, gaps=["SAP FICO"], work_authorisation_state="VALID", margin=0.3
        )
        assert band is MatchBand.WEAK
        assert applied == ["MISSING_MANDATORY_SKILL"]

    def test_a_clean_candidate_triggers_nothing(self):
        assert run().suppressors == []


class TestExplanation:
    def test_a_named_gap_is_returned_for_every_missing_mandatory_skill(self):
        result = run(resource=resource_view(skills={"Power BI": 3.0}))
        assert set(result.gaps) == {"SAP FICO", "SAP S/4HANA"}

    def test_the_narrative_never_replaces_the_numbers(self):
        result = run()
        assert result.narrative
        assert result.components, "the narrative is an addition, not a substitute"

    def test_warnings_are_not_duplicated_by_the_narrative(self):
        result = run(resource=resource_view(work_authorisation_state="EXPIRED"))
        sentences = [line.strip() for line in (result.narrative or "").split(".") if line.strip()]
        assert len(sentences) == len(set(sentences))

    def test_an_unreviewed_profile_is_flagged(self):
        result = run(resource=resource_view(needs_review=True))
        assert any("review" in warning.lower() for warning in result.warnings)

    def test_a_candidate_free_after_the_start_date_is_flagged(self):
        # The requirement starts in 30 days; this consultant is free in 90.
        late = resource_view(ready_from=TODAY + timedelta(days=90), notice_period_days=90)
        result = run(resource=late)

        assert any("start date" in warning.lower() for warning in result.warnings)

    def test_the_lateness_warning_agrees_with_the_availability_score(self):
        late = resource_view(ready_from=TODAY + timedelta(days=90), notice_period_days=90)
        result = run(resource=late)

        availability = result.component("availability")
        assert availability is not None and availability.score is not None
        assert availability.score < 50
        assert any("start date" in warning.lower() for warning in result.warnings)

    def test_a_candidate_free_before_the_start_date_is_not_flagged(self):
        result = run()
        assert not any("start date" in warning.lower() for warning in result.warnings)


class TestHardFilters:
    def test_an_unavailable_resource_is_excluded(self):
        outcome = apply_hard_filters(
            requirement_view(),
            resource_view(availability_status="NOT_AVAILABLE"),
            DEFAULT_THRESHOLDS,
        )
        assert not outcome.included

    def test_a_near_miss_on_experience_is_scored_not_filtered(self):
        # 5 years against 6 required, inside the one-year grace: a recruiter
        # would rather see the near-miss than an empty list.
        outcome = apply_hard_filters(
            requirement_view(), resource_view(total_experience_years=5.0), DEFAULT_THRESHOLDS
        )
        assert outcome.included

    def test_a_long_way_short_on_experience_is_filtered(self):
        outcome = apply_hard_filters(
            requirement_view(), resource_view(total_experience_years=2.0), DEFAULT_THRESHOLDS
        )
        assert not outcome.included
        assert outcome.reason

    def test_mandatory_skills_are_only_a_filter_when_configured(self):
        without = resource_view(skills={"Power BI": 3.0})
        assert apply_hard_filters(requirement_view(), without, DEFAULT_THRESHOLDS).included

        strict = dict(DEFAULT_THRESHOLDS, require_all_mandatory_skills=True)
        assert not apply_hard_filters(requirement_view(), without, strict).included


class TestRateNormalisation:
    @pytest.mark.parametrize(
        ("amount", "unit", "expected"),
        [
            (100, "HOURLY", 17600),
            (800, "DAILY", 17600),
            (20000, "MONTHLY", 20000),
            (240000, "ANNUAL", 20000),
        ],
    )
    def test_every_unit_lands_on_a_monthly_basis(self, amount, unit, expected):
        assert float(to_monthly(amount, unit)) == pytest.approx(expected)

    def test_an_unknown_unit_is_unknown_not_zero(self):
        assert to_monthly(100, "PER_SPRINT") is None
        assert to_monthly(None, "MONTHLY") is None

    def test_an_hourly_consultant_is_compared_fairly_with_a_monthly_rate(self):
        hourly = resource_view(expected_cost=85, expected_cost_unit="HOURLY")
        result = run(resource=hourly)
        cost = result.component("cost")
        assert cost is not None and cost.is_known
        # 85 * 22 * 8 = 14,960 against 22,000 -> ~32% margin.
        assert cost.detail["margin"] == pytest.approx(0.32, abs=0.01)


class TestWeightConfiguration:
    def test_the_documented_defaults_are_valid(self):
        validate_weights(DEFAULT_MATCH_WEIGHTS)

    def test_weights_must_sum_to_one_hundred(self):
        with pytest.raises(ValueError, match="sum to 100"):
            validate_weights(dict(DEFAULT_MATCH_WEIGHTS, skills=50))

    def test_no_component_may_be_left_unweighted(self):
        incomplete = dict(DEFAULT_MATCH_WEIGHTS)
        incomplete.pop("cost")
        with pytest.raises(ValueError, match="Missing weights"):
            validate_weights(incomplete)

    def test_unknown_components_are_rejected(self):
        with pytest.raises(ValueError, match="Unknown weights"):
            validate_weights(dict(DEFAULT_MATCH_WEIGHTS, vibes=0))

    def test_reweighting_changes_the_result(self):
        skills_only = dict.fromkeys(DEFAULT_MATCH_WEIGHTS, 0)
        skills_only["skills"] = 100

        weak_skills = resource_view(skills={"SAP FICO": 8.0})
        balanced = run(resource=weak_skills)
        rebalanced = score_match(
            requirement_view(),
            weak_skills,
            weights=skills_only,
            thresholds=DEFAULT_THRESHOLDS,
            today=TODAY,
        )
        assert rebalanced.overall_score < balanced.overall_score


# ------------------------------------------------------------------ API level


@pytest.fixture
async def sales(as_role):
    return await as_role(Role.SALES)


@pytest.fixture
async def resourcing(as_role):
    return await as_role(Role.HR_RESOURCING)


async def make_requirement(client, **overrides):
    payload = {
        "title": f"Matching Requirement {uuid.uuid4().hex[:8]}",
        "role": "SAP FICO Consultant",
        "positions": 1,
        "priority_source": "P1_EXISTING_CUSTOMER",
        "country": "QA",
        "location": "Doha",
        "work_mode": "ONSITE",
        "experience_min_years": 5,
        "rate_max": "22000",
        "rate_currency": "QAR",
        "rate_unit": "MONTHLY",
        "start_by_date": (date.today() + timedelta(days=45)).isoformat(),
        "skills": [
            {"name": "SAP FICO", "importance": "MANDATORY", "min_years": 5},
            {"name": "SAP S/4HANA", "importance": "PREFERRED"},
        ],
    }
    payload.update(overrides)
    response = await client.post(f"{API}/requirements", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def make_resource(client, **overrides):
    payload = {
        "full_name": f"Match Candidate {uuid.uuid4().hex[:8]}",
        "resource_type": "CONSULTANT",
        "availability_status": "AVAILABLE",
        "notice_period_days": 15,
        "total_experience_years": 9,
        "current_location_country": "QA",
        "current_location_city": "Doha",
        "expected_cost_amount": "15000",
        "expected_cost_currency": "QAR",
        "expected_cost_unit": "MONTHLY",
        "skills": [
            {"name": "SAP FICO", "years": 8, "is_primary": True, "last_used_year": 2026},
            {"name": "SAP S/4HANA", "years": 4, "last_used_year": 2026},
        ],
    }
    payload.update(overrides)
    response = await client.post(f"{API}/resources", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def scenario(as_role):
    """A priced requirement, one strong candidate and one weak one.

    ``as_role`` re-points a single shared client, so the roles are entered in
    order: Resourcing owns the talent cloud, Sales owns demand and matching.
    """
    hr_client, _ = await as_role(Role.HR_RESOURCING)
    strong = await make_resource(hr_client)
    weak = await make_resource(
        hr_client,
        total_experience_years=6,
        current_location_country="IN",
        current_location_city="Chennai",
        expected_cost_amount="20500",
        skills=[{"name": "Power BI", "years": 4, "last_used_year": 2024}],
    )

    sales_client, _ = await as_role(Role.SALES)
    requirement = await make_requirement(sales_client)
    return sales_client, requirement, strong, weak


class TestMatchingEndpoints:
    async def test_running_a_match_returns_a_ranked_list(self, scenario):
        client, requirement, strong, _ = scenario

        response = await client.post(f"{API}/matching/requirements/{requirement['id']}/run")
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["total"] >= 1
        scores = [match["overall_score"] for match in body["matches"]]
        assert scores == sorted(scores, reverse=True)
        assert body["matches"][0]["resource_id"] == strong["id"]

    async def test_every_match_carries_its_explanation(self, scenario):
        client, requirement, _, _ = scenario
        await client.post(f"{API}/matching/requirements/{requirement['id']}/run")

        body = (await client.get(f"{API}/matching/requirements/{requirement['id']}")).json()

        for match in body["matches"]:
            assert match["components"], "a bare percentage is not a match"
            assert match["band"]
            assert match["narrative"]
            assert match["confidence"] is not None
            for component in match["components"]:
                assert component["label"]
                assert component["weight"] is not None

    async def test_the_resource_is_named_so_the_list_is_usable(self, scenario):
        client, requirement, strong, _ = scenario
        await client.post(f"{API}/matching/requirements/{requirement['id']}/run")

        body = (await client.get(f"{API}/matching/requirements/{requirement['id']}")).json()
        top = body["matches"][0]

        assert top["resource_name"] == strong["full_name"]
        assert top["availability_status"]

    async def test_a_single_match_can_be_opened_on_its_own(self, scenario):
        client, requirement, strong, _ = scenario
        await client.post(f"{API}/matching/requirements/{requirement['id']}/run")

        response = await client.get(
            f"{API}/matching/requirements/{requirement['id']}/resources/{strong['id']}"
        )
        assert response.status_code == 200
        assert response.json()["components"]

    async def test_a_match_that_was_never_computed_is_a_404(self, scenario):
        client, requirement, _, _ = scenario
        response = await client.get(
            f"{API}/matching/requirements/{requirement['id']}/resources/{uuid.uuid4()}"
        )
        assert response.status_code == 404

    async def test_results_can_be_filtered_by_band_and_score(self, scenario):
        client, requirement, _, _ = scenario
        await client.post(f"{API}/matching/requirements/{requirement['id']}/run")

        strong_only = (
            await client.get(f"{API}/matching/requirements/{requirement['id']}?band=STRONG")
        ).json()
        assert all(match["band"] == "STRONG" for match in strong_only["matches"])

        high = (
            await client.get(f"{API}/matching/requirements/{requirement['id']}?min_score=99.9")
        ).json()
        assert all(match["overall_score"] >= 99.9 for match in high["matches"])

    async def test_rerunning_replaces_rather_than_duplicates(self, scenario):
        client, requirement, _, _ = scenario

        first = (await client.post(f"{API}/matching/requirements/{requirement['id']}/run")).json()
        second = (await client.post(f"{API}/matching/requirements/{requirement['id']}/run")).json()

        assert first["total"] == second["total"]
        ids = [match["resource_id"] for match in second["matches"]]
        assert len(ids) == len(set(ids))

    async def test_matching_an_unknown_requirement_is_a_404(self, sales):
        client, _ = sales
        response = await client.post(f"{API}/matching/requirements/{uuid.uuid4()}/run")
        assert response.status_code == 404

    async def test_a_requirement_with_no_matches_returns_an_empty_list_not_an_error(self, sales):
        client, _ = sales
        requirement = await make_requirement(
            client,
            experience_min_years=40,
            skills=[{"name": "COBOL", "importance": "MANDATORY"}],
        )
        response = await client.get(f"{API}/matching/requirements/{requirement['id']}")

        assert response.status_code == 200
        assert response.json()["matches"] == []
        assert response.json()["computed_at"] is None

    async def test_the_run_is_audited(self, scenario, as_role):
        client, requirement, _, _ = scenario
        await client.post(f"{API}/matching/requirements/{requirement['id']}/run")

        admin, _ = await as_role(Role.ADMIN)
        entries = (await admin.get(f"{API}/audit?action=MATCH_GENERATED")).json()
        assert entries["items"], "matching must leave a trail"

    async def test_the_snapshot_records_which_rules_produced_it(self, scenario):
        client, requirement, _, _ = scenario
        body = (await client.post(f"{API}/matching/requirements/{requirement['id']}/run")).json()

        assert body["weights_version"] == 1
        assert body["matches"][0]["engine_version"]
        assert body["matches"][0]["weights_version"] == 1


class TestUnreviewedResourcesAreNotMatched:
    async def test_a_parsed_profile_awaiting_review_is_not_matchable(self, as_role):
        hr_client, _ = await as_role(Role.HR_RESOURCING)
        parse = await hr_client.post(
            f"{API}/resources/parse-cv",
            files={"file": ("cv.txt", PENDING_CV.encode(), "text/plain")},
        )
        assert parse.status_code == 201, parse.text
        pending_id = parse.json()["resource_id"]

        sales_client, _ = await as_role(Role.SALES)
        requirement = await make_requirement(sales_client)
        await sales_client.post(f"{API}/matching/requirements/{requirement['id']}/run")
        body = (await sales_client.get(f"{API}/matching/requirements/{requirement['id']}")).json()

        assert pending_id not in [match["resource_id"] for match in body["matches"]]


class TestMatchingAuthorization:
    async def test_management_may_read_but_not_run(self, scenario, as_role):
        _, requirement, _, _ = scenario
        client, _ = await as_role(Role.MANAGEMENT)

        assert (
            await client.post(f"{API}/matching/requirements/{requirement['id']}/run")
        ).status_code == 403
        assert (
            await client.get(f"{API}/matching/requirements/{requirement['id']}")
        ).status_code == 200

    async def test_anonymous_access_is_rejected(self, app, scenario):
        _, requirement, _, _ = scenario

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as anonymous:
            response = await anonymous.get(f"{API}/matching/requirements/{requirement['id']}")
        assert response.status_code == 401

    async def test_a_role_without_margin_sight_is_told_what_was_withheld(self, scenario, as_role):
        client, requirement, _, _ = scenario
        await client.post(f"{API}/matching/requirements/{requirement['id']}/run")

        # Resourcing negotiates cost but does not see client margin.
        hr, _ = await as_role(Role.HR_RESOURCING)
        body = (await hr.get(f"{API}/matching/requirements/{requirement['id']}")).json()
        top = body["matches"][0]

        keys = {component["key"] for component in top["components"]}
        assert "cost" not in keys
        assert "commercial" not in keys
        # Withheld, not silently missing.
        assert top["restricted_components"]

    async def test_sales_sees_the_commercial_components(self, scenario):
        client, requirement, _, _ = scenario
        body = (await client.post(f"{API}/matching/requirements/{requirement['id']}/run")).json()

        keys = {component["key"] for component in body["matches"][0]["components"]}
        assert {"cost", "commercial"} <= keys


class TestScoringConfiguration:
    async def test_the_baseline_ruleset_is_readable(self, sales):
        client, _ = sales
        response = await client.get(f"{API}/scoring/configurations?kind=MATCH_WEIGHTS")

        assert response.status_code == 200
        configs = response.json()
        assert configs
        assert any(config["is_active"] for config in configs)

    async def test_only_admin_may_change_the_rules(self, sales):
        client, _ = sales
        response = await client.post(
            f"{API}/scoring/configurations",
            json={"kind": "MATCH_WEIGHTS", "name": "Sales rewrite", "payload": default_payload()},
        )
        assert response.status_code == 403

    async def test_admin_can_publish_a_new_version(self, as_role):
        client, _ = await as_role(Role.ADMIN)
        await client.get(f"{API}/scoring/configurations")  # ensure v1 exists

        payload = default_payload()
        payload["weights"] = dict(DEFAULT_MATCH_WEIGHTS, skills=40, experience=10)

        response = await client.post(
            f"{API}/scoring/configurations",
            json={"kind": "MATCH_WEIGHTS", "name": "Skills-heavy", "payload": payload},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["version"] > 1
        assert body["is_active"] is False, "publishing is not activating"

    async def test_weights_that_do_not_sum_to_one_hundred_are_rejected(self, as_role):
        client, _ = await as_role(Role.ADMIN)
        payload = default_payload()
        payload["weights"] = dict(DEFAULT_MATCH_WEIGHTS, skills=90)

        response = await client.post(
            f"{API}/scoring/configurations",
            json={"kind": "MATCH_WEIGHTS", "name": "Broken", "payload": payload},
        )
        assert response.status_code == 422
        assert "100" in response.text

    async def test_activating_a_version_deactivates_the_previous_one(self, as_role):
        client, _ = await as_role(Role.ADMIN)
        await client.get(f"{API}/scoring/configurations")

        payload = default_payload()
        payload["weights"] = dict(DEFAULT_MATCH_WEIGHTS, skills=35, technology=10)
        created = (
            await client.post(
                f"{API}/scoring/configurations",
                json={"kind": "MATCH_WEIGHTS", "name": "Tuned", "payload": payload},
            )
        ).json()

        activated = await client.post(f"{API}/scoring/configurations/{created['id']}/activate")
        assert activated.status_code == 200
        assert activated.json()["is_active"] is True

        configs = (await client.get(f"{API}/scoring/configurations?kind=MATCH_WEIGHTS")).json()
        assert len([config for config in configs if config["is_active"]]) == 1

    async def test_activating_is_audited(self, as_role):
        client, _ = await as_role(Role.ADMIN)
        await client.get(f"{API}/scoring/configurations")
        payload = default_payload()
        created = (
            await client.post(
                f"{API}/scoring/configurations",
                json={"kind": "MATCH_WEIGHTS", "name": "Audited", "payload": payload},
            )
        ).json()
        await client.post(f"{API}/scoring/configurations/{created['id']}/activate")

        entries = (await client.get(f"{API}/audit?action=SCORING_CONFIG_CHANGED")).json()
        assert entries["items"]

    async def test_activating_an_unknown_version_is_a_404(self, as_role):
        client, _ = await as_role(Role.ADMIN)
        response = await client.post(f"{API}/scoring/configurations/{uuid.uuid4()}/activate")
        assert response.status_code == 404


class TestConfigurationDrivesTheEngine:
    async def test_a_new_weighting_changes_the_next_run(self, scenario, as_role):
        client, requirement, _, _ = scenario
        before = (await client.post(f"{API}/matching/requirements/{requirement['id']}/run")).json()
        baseline = {m["resource_id"]: m["overall_score"] for m in before["matches"]}

        admin, _ = await as_role(Role.ADMIN)
        payload = default_payload()
        payload["weights"] = dict.fromkeys(DEFAULT_MATCH_WEIGHTS, 0) | {"location": 100}
        created = (
            await admin.post(
                f"{API}/scoring/configurations",
                json={"kind": "MATCH_WEIGHTS", "name": "Location only", "payload": payload},
            )
        ).json()
        await admin.post(f"{API}/scoring/configurations/{created['id']}/activate")

        sales_again, _ = await as_role(Role.SALES)
        after = (
            await sales_again.post(f"{API}/matching/requirements/{requirement['id']}/run")
        ).json()

        assert after["weights_version"] == created["version"]
        changed = {m["resource_id"]: m["overall_score"] for m in after["matches"]}
        assert changed != baseline, "rules are data — changing them must change the output"

        # Restore the baseline so later tests see the documented weights.
        configs = (await admin.get(f"{API}/scoring/configurations?kind=MATCH_WEIGHTS")).json()
        original = min(configs, key=lambda config: config["version"])
        await admin.post(f"{API}/scoring/configurations/{original['id']}/activate")


class TestPersistedSnapshot:
    async def test_the_stored_components_reproduce_the_stored_total(self, scenario):
        client, requirement, _, _ = scenario
        body = (await client.post(f"{API}/matching/requirements/{requirement['id']}/run")).json()

        for match in body["matches"]:
            known = [c for c in match["components"] if c["score"] is not None and c["weight"] > 0]
            if not known:
                continue
            expected = sum(c["score"] * c["weight"] for c in known) / sum(
                c["weight"] for c in known
            )
            assert match["overall_score"] == pytest.approx(expected, abs=0.1)

    async def test_the_snapshot_is_timestamped(self, scenario):
        client, requirement, _, _ = scenario
        await client.post(f"{API}/matching/requirements/{requirement['id']}/run")

        body = (await client.get(f"{API}/matching/requirements/{requirement['id']}")).json()
        computed = datetime.fromisoformat(body["computed_at"])
        assert abs((datetime.now(UTC) - computed).total_seconds()) < 120
