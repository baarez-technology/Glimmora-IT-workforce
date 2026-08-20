"""Phase 9: addressability, commercial and the Glimmora Opportunity Score.

SCORING.md section 8 sets the strictest test obligations in the codebase, and
they are enumerated here deliberately:

1. golden-file tests for every worked example, including the SOW's 94/88/91 → 91
2. every factor at MET, NOT_MET, UNKNOWN and NOT_APPLICABLE
3. weight sums validated for every seeded config
4. property test: improving any single factor never lowers the total
5. missing-component redistribution, for each component being null
6. every hard suppressor
7. currency conversion and one-off amortisation, to the cent
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.permissions import Role
from app.engines.scoring.addressability import (
    AddressabilityBand,
    AddressabilityInput,
    FactorState,
    score_addressability,
    supply_gate_for,
)
from app.engines.scoring.commercial import (
    CommercialInput,
    calculate,
    convert,
    score_commercial,
    to_monthly,
)
from app.engines.scoring.config import (
    DEFAULT_ADDRESSABILITY_RULES,
    DEFAULT_COMMERCIAL_BANDS,
    DEFAULT_CURRENCY_RATES,
    DEFAULT_OPPORTUNITY_WEIGHTS,
    Factor,
    validate_addressability_rules,
    validate_commercial_bands,
    validate_opportunity_weights,
)
from app.engines.scoring.opportunity import OpportunityBand, compose

API = "/api/v1"
NOW = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)
RULES = DEFAULT_ADDRESSABILITY_RULES
BANDS = DEFAULT_COMMERCIAL_BANDS
WEIGHTS = DEFAULT_OPPORTUNITY_WEIGHTS
RATES = DEFAULT_CURRENCY_RATES


def addressability(**overrides):
    base = {
        "account_known": True,
        "contract_outsourcing_friendly": True,
        "is_existing_customer": True,
        "is_existing_partner": False,
        "has_preferred_route": False,
        "is_approved_vendor": True,
        "has_msa": True,
        "has_decision_maker": True,
        "relationship_status": "ACTIVE",
        "requirement_is_open": True,
        "response_deadline_at": NOW + timedelta(hours=36),
        "monthly_rate": Decimal("22000"),
        "best_talent_match": 91,
    }
    base.update(overrides)
    return score_addressability(AddressabilityInput(**base), rules=RULES, now=NOW)


def _stub_addressability(score: float):
    from app.engines.scoring.addressability import AddressabilityResult

    return AddressabilityResult(
        score=score,
        raw_total=score,
        supply_gate=1.0,
        band=AddressabilityBand.ADDRESSABLE,
        confidence=1.0,
    )


def _stub_commercial(score: float | None, margin: float | None = 34.0):
    from app.engines.scoring.commercial import CommercialCalculation, CommercialResult

    calc = CommercialCalculation(None, None, None, margin, None, None, 24, 1)
    return CommercialResult(score=score, calculation=calc)


# ------------------------------------------------------- 1. golden examples


class TestGoldenExamples:
    def test_the_sow_worked_example_reproduces_ninety_one(self):
        """0.40*94 + 0.35*88 + 0.25*91 = 91.15 -> 91. The Phase 9 acceptance test."""
        result = compose(
            talent_match=94,
            addressability=_stub_addressability(88),
            commercial=_stub_commercial(91),
            weights_config=WEIGHTS,
        )

        assert result.score == 91
        assert result.band is OpportunityBand.PURSUE_NOW
        assert result.confidence == 1.0

    def test_the_sow_example_contributions_are_exact(self):
        result = compose(
            talent_match=94,
            addressability=_stub_addressability(88),
            commercial=_stub_commercial(91),
            weights_config=WEIGHTS,
        )
        contributions = {item.key: item.contribution for item in result.components}

        assert contributions["talent_match"] == pytest.approx(37.6)
        assert contributions["addressability"] == pytest.approx(30.8)
        assert contributions["commercial"] == pytest.approx(22.75)

    def test_the_addressability_worked_example_reproduces_eighty_five(self):
        """SCORING.md section 2: Milaha, direct, all factors met bar the route."""
        result = addressability()

        assert result.raw_total == 85
        assert result.supply_gate == 1.00
        assert result.score == 85

    def test_a_direct_relationship_scores_zero_on_route_without_being_penalised(self):
        result = addressability()
        route = result.factor(Factor.PARTNER_ROUTE)

        assert route is not None
        assert route.state is FactorState.NOT_APPLICABLE
        assert route.points == 0
        assert "no partner route required" in (route.evidence or "").lower()
        # The whole point: a correct zero must not appear as something to fix.
        assert route.evidence not in result.risks
        assert route.label not in result.missing_information


# ------------------------------------------------------------ 2. all states


class TestEveryFactorState:
    def test_a_met_factor_scores_its_full_points(self):
        result = addressability(is_existing_customer=True)
        factor = result.factor(Factor.EXISTING_CUSTOMER)
        assert factor is not None
        assert factor.state is FactorState.MET
        assert factor.points == factor.max_points

    def test_a_not_met_factor_scores_zero_and_becomes_a_risk(self):
        result = addressability(is_approved_vendor=False, has_msa=False)
        factor = result.factor(Factor.APPROVED_VENDOR)

        assert factor is not None
        assert factor.state is FactorState.NOT_MET
        assert factor.points == 0
        assert any("approved vendor" in risk.lower() for risk in result.risks)

    def test_an_unknown_factor_is_reported_as_missing_not_as_a_no(self):
        result = addressability(contract_outsourcing_friendly=None)
        factor = result.factor(Factor.OUTSOURCING_FRIENDLY)

        assert factor is not None
        assert factor.state is FactorState.UNKNOWN
        assert factor.points == 0
        assert factor.label in result.missing_information
        assert factor.evidence not in result.risks

    def test_not_applicable_is_neither_a_risk_nor_missing_information(self):
        result = addressability()
        route = result.factor(Factor.PARTNER_ROUTE)
        assert route is not None and route.state is FactorState.NOT_APPLICABLE
        assert route.label not in result.missing_information

    def test_unknown_lowers_confidence_but_not_applicable_does_not(self):
        known = addressability()
        unknown = addressability(contract_outsourcing_friendly=None)

        assert known.confidence == 1.0, "NOT_APPLICABLE counts as answered"
        assert unknown.confidence < 1.0

    @pytest.mark.parametrize(
        "field",
        [
            "contract_outsourcing_friendly",
            "is_existing_customer",
            "has_decision_maker",
        ],
    )
    def test_every_boolean_factor_supports_all_three_states(self, field):
        states = {
            addressability(**{field: value}).factor(_KEY_FOR[field]).state  # type: ignore[union-attr]
            for value in (True, False, None)
        }
        assert states == {FactorState.MET, FactorState.NOT_MET, FactorState.UNKNOWN}


_KEY_FOR = {
    "contract_outsourcing_friendly": Factor.OUTSOURCING_FRIENDLY,
    "is_existing_customer": Factor.EXISTING_CUSTOMER,
    "has_decision_maker": Factor.DECISION_MAKER,
}


class TestSubmissionWindow:
    def test_no_deadline_is_an_open_window_not_a_missed_one(self):
        factor = addressability(response_deadline_at=None).factor(Factor.CAN_SUBMIT)
        assert factor is not None and factor.state is FactorState.MET

    def test_a_passed_deadline_scores_zero(self):
        factor = addressability(response_deadline_at=NOW - timedelta(hours=1)).factor(
            Factor.CAN_SUBMIT
        )
        assert factor is not None and factor.state is FactorState.NOT_MET

    def test_a_closed_requirement_scores_zero_on_being_active(self):
        factor = addressability(requirement_is_open=False).factor(Factor.REQUIREMENT_ACTIVE)
        assert factor is not None and factor.state is FactorState.NOT_MET


class TestSupplyGate:
    @pytest.mark.parametrize(
        ("best_match", "expected"),
        [(91, 1.00), (70, 1.00), (60, 0.85), (55, 0.85), (45, 0.60), (40, 0.60), (20, 0.35)],
    )
    def test_the_gate_follows_the_documented_bands(self, best_match, expected):
        gate, _ = supply_gate_for(best_match, RULES)
        assert gate == expected

    def test_no_matching_run_lands_on_the_floor_and_says_why(self):
        gate, evidence = supply_gate_for(None, RULES)
        assert gate == RULES["supply_gate_floor"]
        assert "no matching" in evidence.lower()

    def test_the_gate_suppresses_rather_than_shaves(self):
        """Reachability with nobody to send is not addressability."""
        reachable = addressability(best_talent_match=91)
        no_supply = addressability(best_talent_match=10)

        assert reachable.score == 85
        assert no_supply.score == pytest.approx(round(85 * 0.35))
        assert no_supply.band is AddressabilityBand.NOT_ADDRESSABLE

    def test_the_gate_appears_as_a_risk_when_it_bites(self):
        result = addressability(best_talent_match=45)
        assert any("supply gate" in risk.lower() for risk in result.risks)


# ------------------------------------------------------ 3. config validation


class TestConfigValidation:
    def test_the_seeded_addressability_rules_are_valid(self):
        validate_addressability_rules(RULES)

    def test_the_seeded_commercial_bands_are_valid(self):
        validate_commercial_bands(BANDS)

    def test_the_seeded_opportunity_weights_are_valid(self):
        validate_opportunity_weights(WEIGHTS)

    def test_addressability_points_must_sum_to_one_hundred(self):
        broken = {**RULES, "factors": {**RULES["factors"], Factor.EXISTING_CUSTOMER: 50}}
        with pytest.raises(ValueError, match="sum to 100"):
            validate_addressability_rules(broken)

    def test_an_unknown_factor_is_rejected(self):
        broken = {**RULES, "factors": {**RULES["factors"], "vibes": 0}}
        with pytest.raises(ValueError, match="Unknown factors"):
            validate_addressability_rules(broken)

    def test_opportunity_weights_must_sum_to_one(self):
        broken = {**WEIGHTS, "weights": {**WEIGHTS["weights"], "talent_match": 0.9}}
        with pytest.raises(ValueError, match="sum to 1"):
            validate_opportunity_weights(broken)

    def test_commercial_maxima_must_sum_to_one_hundred(self):
        with pytest.raises(ValueError, match="sum to 100"):
            validate_commercial_bands({**BANDS, "margin_max": 80})

    def test_bands_must_be_ordered_high_to_low(self):
        with pytest.raises(ValueError, match="ordered"):
            validate_commercial_bands({**BANDS, "duration_bands": [[3, 6], [24, 15]]})


# ---------------------------------------------------------- 4. monotonicity


class TestMonotonicity:
    """Improving any single input must never lower the total."""

    def test_opportunity_composition_is_monotonic(self):
        for talent in range(0, 101, 20):
            for addr in range(0, 101, 20):
                for comm in range(0, 101, 20):
                    base = compose(
                        talent_match=talent,
                        addressability=_stub_addressability(addr),
                        commercial=_stub_commercial(comm),
                        weights_config=WEIGHTS,
                    ).score
                    for dt, da, dc in ((20, 0, 0), (0, 20, 0), (0, 0, 20)):
                        if talent + dt > 100 or addr + da > 100 or comm + dc > 100:
                            continue
                        better = compose(
                            talent_match=talent + dt,
                            addressability=_stub_addressability(addr + da),
                            commercial=_stub_commercial(comm + dc),
                            weights_config=WEIGHTS,
                        ).score
                        assert better >= base

    def test_addressability_is_monotonic_in_every_factor(self):
        flags = [
            "contract_outsourcing_friendly",
            "is_existing_customer",
            "is_approved_vendor",
            "has_decision_maker",
        ]
        for combo in itertools.product([False, True], repeat=len(flags)):
            settings = dict(zip(flags, combo, strict=True))
            base = addressability(has_msa=False, **settings).score
            for flag, value in settings.items():
                if value:
                    continue
                improved = addressability(has_msa=False, **{**settings, flag: True}).score
                assert improved >= base, f"improving {flag} lowered the score"

    def test_a_better_candidate_never_lowers_addressability(self):
        previous = -1.0
        for match in range(0, 101, 10):
            score = addressability(best_talent_match=match).score
            assert score >= previous
            previous = score


# ------------------------------------------------------- 5. redistribution


class TestMissingComponentRedistribution:
    def test_an_unknown_commercial_score_is_redistributed_not_zeroed(self):
        with_commercial = compose(
            talent_match=94,
            addressability=_stub_addressability(88),
            commercial=_stub_commercial(91),
            weights_config=WEIGHTS,
        )
        without = compose(
            talent_match=94,
            addressability=_stub_addressability(88),
            commercial=None,
            weights_config=WEIGHTS,
        )

        # Zeroing would give 0.40*94 + 0.35*88 = 68. Redistribution keeps it at
        # the level of what is actually known.
        assert without.score > 68
        assert without.score == pytest.approx(with_commercial.score, abs=2)
        assert without.confidence == pytest.approx(0.75, abs=0.01)

    def test_an_unknown_talent_score_is_redistributed(self):
        result = compose(
            talent_match=None,
            addressability=_stub_addressability(88),
            commercial=_stub_commercial(91),
            weights_config=WEIGHTS,
        )
        assert result.confidence == pytest.approx(0.60, abs=0.01)
        assert result.score == pytest.approx(89, abs=1)

    def test_an_unknown_addressability_score_is_redistributed(self):
        result = compose(
            talent_match=94,
            addressability=None,
            commercial=_stub_commercial(91),
            weights_config=WEIGHTS,
        )
        assert result.confidence == pytest.approx(0.65, abs=0.01)

    def test_a_missing_component_is_named_not_silently_dropped(self):
        result = compose(
            talent_match=94,
            addressability=_stub_addressability(88),
            commercial=None,
            weights_config=WEIGHTS,
        )
        assert any("commercial" in item.lower() for item in result.missing_information)
        commercial = result.component("commercial")
        assert commercial is not None and commercial.score is None

    def test_nothing_known_scores_zero_at_zero_confidence(self):
        result = compose(
            talent_match=None, addressability=None, commercial=None, weights_config=WEIGHTS
        )
        assert result.score == 0
        assert result.confidence == 0.0
        assert result.band is OpportunityBand.DEPRIORITIZE


# --------------------------------------------------------- 6. suppressors


class TestHardSuppressors:
    def test_low_addressability_caps_at_review(self):
        result = compose(
            talent_match=94,
            addressability=_stub_addressability(30),
            commercial=_stub_commercial(91),
            weights_config=WEIGHTS,
        )
        assert result.band is OpportunityBand.REVIEW
        assert any("addressability" in item.lower() for item in result.suppressors)

    def test_an_expired_sla_caps_at_deprioritize(self):
        result = compose(
            talent_match=94,
            addressability=_stub_addressability(88),
            commercial=_stub_commercial(91),
            weights_config=WEIGHTS,
            sla_expired=True,
        )
        assert result.band is OpportunityBand.DEPRIORITIZE
        assert "still open" in result.recommended_action.lower()

    def test_an_expired_work_permit_caps_at_review(self):
        result = compose(
            talent_match=94,
            addressability=_stub_addressability(88),
            commercial=_stub_commercial(91),
            weights_config=WEIGHTS,
            best_match_work_auth_expired=True,
        )
        assert result.band is OpportunityBand.REVIEW
        assert "work permit" in result.recommended_action.lower()

    def test_a_negative_margin_caps_at_review(self):
        result = compose(
            talent_match=94,
            addressability=_stub_addressability(88),
            commercial=_stub_commercial(5, margin=-4.0),
            weights_config=WEIGHTS,
        )
        assert result.band is OpportunityBand.REVIEW
        assert any("negative margin" in item.lower() for item in result.suppressors)

    def test_the_score_itself_is_never_altered_by_a_suppressor(self):
        clean = compose(
            talent_match=94,
            addressability=_stub_addressability(88),
            commercial=_stub_commercial(91),
            weights_config=WEIGHTS,
        )
        suppressed = compose(
            talent_match=94,
            addressability=_stub_addressability(88),
            commercial=_stub_commercial(91),
            weights_config=WEIGHTS,
            sla_expired=True,
        )
        assert suppressed.score == clean.score
        assert suppressed.band is not clean.band

    def test_a_suppressor_never_promotes_a_band(self):
        result = compose(
            talent_match=10,
            addressability=_stub_addressability(20),
            commercial=_stub_commercial(10),
            weights_config=WEIGHTS,
            sla_expired=True,
        )
        assert result.band is OpportunityBand.DEPRIORITIZE

    def test_the_suppressor_action_outranks_the_generic_band_action(self):
        result = compose(
            talent_match=94,
            addressability=_stub_addressability(88),
            commercial=_stub_commercial(91),
            weights_config=WEIGHTS,
            best_match_work_auth_expired=True,
        )
        assert "renew" in result.recommended_action.lower()
        assert result.recommended_action != "Close the named gaps first"


# ---------------------------------------- 7. currency and amortisation


class TestCommercialCalculator:
    def test_one_off_costs_are_amortised_across_the_engagement(self):
        result = calculate(
            CommercialInput(
                bill_rate=Decimal("22000"),
                bill_unit="MONTHLY",
                cost_rate=Decimal("14000"),
                cost_unit="MONTHLY",
                visa_cost=Decimal("6000"),
                insurance_cost=Decimal("3000"),
                duration_months=24,
            ),
            bands=BANDS,
            rates=RATES,
        )

        assert result.one_off_total == Decimal("9000.00")
        assert result.one_off_monthly == Decimal("375.00")  # 9000 / 24, to the cent
        assert result.monthly_cost == Decimal("14375.00")
        assert result.gross_profit == Decimal("7625.00")

    def test_amortisation_can_be_switched_off(self):
        result = calculate(
            CommercialInput(
                bill_rate=Decimal("22000"),
                bill_unit="MONTHLY",
                cost_rate=Decimal("14000"),
                cost_unit="MONTHLY",
                visa_cost=Decimal("9000"),
                duration_months=24,
            ),
            bands={**BANDS, "amortise_one_off_costs": False},
            rates=RATES,
        )
        assert result.monthly_cost == Decimal("23000.00")
        assert result.margin_percent is not None and result.margin_percent < 0

    def test_contract_value_and_total_profit_scale_with_positions(self):
        one = calculate(
            CommercialInput(
                bill_rate=Decimal("20000"),
                bill_unit="MONTHLY",
                cost_rate=Decimal("14000"),
                cost_unit="MONTHLY",
                duration_months=12,
                positions=1,
            ),
            bands=BANDS,
            rates=RATES,
        )
        three = calculate(
            CommercialInput(
                bill_rate=Decimal("20000"),
                bill_unit="MONTHLY",
                cost_rate=Decimal("14000"),
                cost_unit="MONTHLY",
                duration_months=12,
                positions=3,
            ),
            bands=BANDS,
            rates=RATES,
        )
        assert three.contract_value == one.contract_value * 3
        assert three.total_profit == one.total_profit * 3
        assert three.margin_percent == one.margin_percent, "margin is per head"

    @pytest.mark.parametrize(
        ("amount", "unit", "expected"),
        [
            (100, "HOURLY", Decimal("17600.00")),
            (800, "DAILY", Decimal("17600.00")),
            (20000, "MONTHLY", Decimal("20000.00")),
            (240000, "ANNUAL", Decimal("20000.00")),
        ],
    )
    def test_every_rate_unit_normalises_to_monthly(self, amount, unit, expected):
        assert to_monthly(Decimal(str(amount)), unit, working_days=22, hours_per_day=8) == expected

    def test_conversion_is_exact_and_flagged(self):
        money = convert(Decimal("6000"), "USD", rates=RATES)
        assert money is not None
        assert money.amount == Decimal("21840.00")  # 6000 * 3.64
        assert money.is_converted is True

    def test_the_base_currency_is_not_marked_as_converted(self):
        money = convert(Decimal("20000"), "QAR", rates=RATES)
        assert money is not None and money.is_converted is False

    def test_an_unknown_currency_is_not_assumed_to_be_parity(self):
        assert convert(Decimal("5000"), "ZZZ", rates=RATES) is None

        result = calculate(
            CommercialInput(bill_rate=Decimal("5000"), bill_unit="MONTHLY", bill_currency="ZZZ"),
            bands=BANDS,
            rates=RATES,
        )
        assert any("exchange rate" in item for item in result.missing_information)

    def test_a_converted_figure_is_flagged_as_a_risk(self):
        result = score_commercial(
            CommercialInput(
                bill_rate=Decimal("6000"),
                bill_unit="MONTHLY",
                bill_currency="USD",
                cost_rate=Decimal("14000"),
                cost_unit="MONTHLY",
                duration_months=12,
            ),
            bands=BANDS,
            rates=RATES,
        )
        assert any("converted" in risk.lower() for risk in result.risks)

    def test_a_zero_revenue_does_not_divide_by_zero(self):
        result = calculate(
            CommercialInput(
                bill_rate=Decimal("0"),
                bill_unit="MONTHLY",
                cost_rate=Decimal("100"),
                cost_unit="MONTHLY",
            ),
            bands=BANDS,
            rates=RATES,
        )
        assert result.margin_percent == 0.0


class TestCommercialScore:
    def test_an_unknown_bill_rate_returns_null_not_zero(self):
        result = score_commercial(
            CommercialInput(cost_rate=Decimal("14000"), cost_unit="MONTHLY"),
            bands=BANDS,
            rates=RATES,
        )
        assert result.score is None
        assert result.confidence == 0.0
        assert "Client bill rate not confirmed" in result.missing_information

    def test_margin_dominates_the_score(self):
        def score_at(margin_cost: str) -> float:
            result = score_commercial(
                CommercialInput(
                    bill_rate=Decimal("20000"),
                    bill_unit="MONTHLY",
                    cost_rate=Decimal(margin_cost),
                    cost_unit="MONTHLY",
                    duration_months=12,
                ),
                bands=BANDS,
                rates=RATES,
            )
            assert result.score is not None
            return result.score

        assert score_at("12000") > score_at("18000")

    def test_the_sub_scores_sum_to_the_total(self):
        result = score_commercial(
            CommercialInput(
                bill_rate=Decimal("22000"),
                bill_unit="MONTHLY",
                cost_rate=Decimal("13000"),
                cost_unit="MONTHLY",
                duration_months=24,
            ),
            bands=BANDS,
            rates=RATES,
        )
        assert result.score == sum(item.points for item in result.sub_scores)


# ------------------------------------------------------------- API level


@pytest.fixture
async def scored(as_role):
    """An addressable account, a priced requirement, a matched consultant."""
    sales, _ = await as_role(Role.SALES)

    account = (
        await sales.post(
            f"{API}/accounts",
            json={
                "name": f"Milaha Score {uuid.uuid4().hex[:6]}",
                "account_type": "CUSTOMER",
                "country": "QA",
                "relationship_status": "ACTIVE",
                "is_existing_customer": True,
                "is_approved_vendor": True,
                "has_msa": True,
                "contract_outsourcing_friendly": True,
            },
        )
    ).json()

    await sales.post(
        f"{API}/contacts",
        json={
            "account_id": account["id"],
            "full_name": "Procurement Lead",
            "is_decision_maker": True,
        },
    )

    requirement = (
        await sales.post(
            f"{API}/requirements",
            json={
                "title": f"Scored Requirement {uuid.uuid4().hex[:6]}",
                "role": "SAP FICO Consultant",
                "positions": 1,
                "priority_source": "P1_EXISTING_CUSTOMER",
                "account_id": account["id"],
                "country": "QA",
                "location": "Doha",
                "work_mode": "ONSITE",
                "experience_min_years": 5,
                "duration_months": 24,
                "rate_max": "22000",
                "rate_currency": "QAR",
                "rate_unit": "MONTHLY",
                "skills": [{"name": "SAP FICO", "importance": "MANDATORY", "min_years": 5}],
            },
        )
    ).json()

    hr, _ = await as_role(Role.HR_RESOURCING)
    await hr.post(
        f"{API}/resources",
        json={
            "full_name": f"Scored Candidate {uuid.uuid4().hex[:6]}",
            "resource_type": "CONSULTANT",
            "availability_status": "AVAILABLE",
            "notice_period_days": 0,
            "total_experience_years": 9,
            "current_location_country": "QA",
            "current_location_city": "Doha",
            "expected_cost_amount": "14000",
            "expected_cost_currency": "QAR",
            "expected_cost_unit": "MONTHLY",
            "skills": [
                {"name": "SAP FICO", "years": 8, "is_primary": True, "last_used_year": 2026}
            ],
        },
    )

    sales, _ = await as_role(Role.SALES)
    await sales.post(f"{API}/matching/requirements/{requirement['id']}/run")
    return sales, requirement, account


class TestScoringEndpoints:
    async def test_recomputing_produces_a_full_explanation(self, scored):
        client, requirement, _ = scored

        response = await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["score"] >= 0
        assert body["band"]
        assert body["components"], "a bare number is never returned"
        assert body["factors"], "the addressability factors must be shown"
        assert body["narrative"]
        assert body["recommended_action"]
        assert body["engine_version"]

    async def test_every_factor_carries_a_state_and_evidence(self, scored):
        client, requirement, _ = scored
        body = (
            await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")
        ).json()

        assert len(body["factors"]) == 8
        for factor in body["factors"]:
            assert factor["state"] in {"MET", "NOT_MET", "NOT_APPLICABLE", "UNKNOWN"}
            assert factor["evidence"]
            assert factor["max_points"] > 0

    async def test_the_components_reproduce_the_headline(self, scored):
        client, requirement, _ = scored
        body = (
            await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")
        ).json()

        total = sum(item["contribution"] for item in body["components"])
        assert body["score"] == pytest.approx(total, abs=1)

    async def test_the_score_records_which_rule_versions_produced_it(self, scored):
        client, requirement, _ = scored
        body = (
            await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")
        ).json()

        assert body["addressability_config_version"] == 1
        assert body["commercial_config_version"] == 1
        assert body["opportunity_config_version"] == 1

    async def test_explaining_before_any_run_scores_without_persisting(self, scored):
        client, requirement, _ = scored

        response = await client.get(f"{API}/scoring/requirements/{requirement['id']}/explain")
        assert response.status_code == 200
        assert response.json()["components"]

        history = (
            await client.get(f"{API}/scoring/requirements/{requirement['id']}/history")
        ).json()
        assert history == [], "a read must not persist a snapshot"

    async def test_snapshots_are_appended_never_overwritten(self, scored):
        client, requirement, _ = scored
        await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")
        await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")

        history = (
            await client.get(f"{API}/scoring/requirements/{requirement['id']}/history")
        ).json()
        assert len(history) == 2, "score history is the record of why we decided"

    async def test_the_ranked_board_returns_current_scores_only(self, scored):
        client, requirement, _ = scored
        await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")
        await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")

        board = (await client.get(f"{API}/scoring/opportunities")).json()
        mine = [row for row in board if row["requirement_id"] == requirement["id"]]
        assert len(mine) == 1

    async def test_an_unknown_requirement_is_a_404(self, scored):
        client, _, _ = scored
        response = await client.post(f"{API}/scoring/requirements/{uuid.uuid4()}/recompute")
        assert response.status_code == 404

    async def test_scoring_is_audited(self, scored, as_role):
        client, requirement, _ = scored
        await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")

        admin, _ = await as_role(Role.ADMIN)
        entries = (await admin.get(f"{API}/audit?action=SCORE_COMPUTED")).json()
        assert entries["items"]


class TestCommercialPreviewEndpoint:
    async def test_the_calculator_persists_nothing(self, scored):
        client, _, _ = scored
        response = await client.post(
            f"{API}/scoring/commercial/preview",
            json={
                "bill_rate": "22000",
                "bill_unit": "MONTHLY",
                "cost_rate": "14000",
                "cost_unit": "MONTHLY",
                "visa_cost": "6000",
                "duration_months": 24,
            },
        )
        assert response.status_code == 200
        body = response.json()

        assert float(body["monthly_revenue"]) == 22000
        assert float(body["one_off_monthly"]) == 250  # 6000 / 24
        assert body["margin_percent"] is not None

    async def test_it_reports_what_it_could_not_calculate(self, scored):
        client, _, _ = scored
        body = (
            await client.post(
                f"{API}/scoring/commercial/preview",
                json={"cost_rate": "14000", "cost_unit": "MONTHLY", "duration_months": 12},
            )
        ).json()

        assert body["margin_percent"] is None
        assert "Client bill rate not confirmed" in body["missing_information"]

    async def test_resourcing_cannot_run_the_commercial_calculator(self, as_role):
        client, _ = await as_role(Role.HR_RESOURCING)
        response = await client.post(
            f"{API}/scoring/commercial/preview",
            json={"bill_rate": "20000", "bill_unit": "MONTHLY"},
        )
        assert response.status_code == 403


class TestSimulation:
    async def test_a_draft_ruleset_can_be_previewed_before_activation(self, scored, as_role):
        client, requirement, _ = scored
        await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")

        admin, _ = await as_role(Role.ADMIN)
        draft = (
            await admin.post(
                f"{API}/scoring/configurations",
                json={
                    "kind": "OPPORTUNITY_WEIGHTS",
                    "name": "Commercial-heavy",
                    "payload": {
                        **WEIGHTS,
                        "weights": {
                            "talent_match": 0.20,
                            "addressability": 0.30,
                            "commercial": 0.50,
                        },
                    },
                },
            )
        ).json()
        assert draft["is_active"] is False

        response = await admin.post(f"{API}/scoring/configurations/{draft['id']}/simulate")
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["evaluated"] >= 1
        assert body["kind"] == "OPPORTUNITY_WEIGHTS"
        assert "distribution_before" in body and "distribution_after" in body
        for row in body["rows"]:
            assert row["before_score"] is not None
            assert row["after_score"] is not None

    async def test_simulating_does_not_persist_anything(self, scored, as_role):
        client, requirement, _ = scored
        await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")
        before = (
            await client.get(f"{API}/scoring/requirements/{requirement['id']}/history")
        ).json()

        admin, _ = await as_role(Role.ADMIN)
        draft = (
            await admin.post(
                f"{API}/scoring/configurations",
                json={
                    "kind": "OPPORTUNITY_WEIGHTS",
                    "name": "Draft only",
                    "payload": {
                        **WEIGHTS,
                        "weights": {
                            "talent_match": 0.5,
                            "addressability": 0.3,
                            "commercial": 0.2,
                        },
                    },
                },
            )
        ).json()
        await admin.post(f"{API}/scoring/configurations/{draft['id']}/simulate")

        after = (await client.get(f"{API}/scoring/requirements/{requirement['id']}/history")).json()
        assert len(after) == len(before)

    async def test_an_already_active_version_cannot_be_simulated(self, as_role):
        admin, _ = await as_role(Role.ADMIN)
        configs = (await admin.get(f"{API}/scoring/configurations?kind=OPPORTUNITY_WEIGHTS")).json()
        active = next(config for config in configs if config["is_active"])

        response = await admin.post(f"{API}/scoring/configurations/{active['id']}/simulate")
        assert response.status_code == 422
        assert "already active" in response.text.lower()

    async def test_an_invalid_ruleset_is_rejected_at_creation(self, as_role):
        admin, _ = await as_role(Role.ADMIN)
        response = await admin.post(
            f"{API}/scoring/configurations",
            json={
                "kind": "OPPORTUNITY_WEIGHTS",
                "name": "Broken",
                "payload": {
                    **WEIGHTS,
                    "weights": {
                        "talent_match": 0.9,
                        "addressability": 0.35,
                        "commercial": 0.25,
                    },
                },
            },
        )
        assert response.status_code == 422


class TestScoringAuthorization:
    async def test_management_may_read_but_not_recompute(self, scored, as_role):
        _, requirement, _ = scored
        client, _ = await as_role(Role.MANAGEMENT)

        assert (
            await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")
        ).status_code == 403
        assert (
            await client.get(f"{API}/scoring/requirements/{requirement['id']}/explain")
        ).status_code == 200

    async def test_a_role_without_margin_sight_gets_the_score_but_not_the_money(
        self, scored, as_role
    ):
        client, requirement, _ = scored
        await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")

        hr, _ = await as_role(Role.HR_RESOURCING)
        body = (await hr.get(f"{API}/scoring/requirements/{requirement['id']}/explain")).json()

        # The score and its reasoning are not themselves commercial secrets.
        assert body["score"] is not None
        assert body["factors"]
        assert body["commercial"] is None
        assert "gross_profit" in body["restricted_fields"]

    async def test_sales_sees_the_commercial_figures(self, scored):
        client, requirement, _ = scored
        body = (
            await client.post(f"{API}/scoring/requirements/{requirement['id']}/recompute")
        ).json()

        assert body["commercial"] is not None
        assert body["commercial"]["monthly_revenue"] is not None
