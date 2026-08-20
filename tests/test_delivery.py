"""Phase 11: deployments, billing and management intelligence.

The definition of done, restated:

* selecting a candidate creates a deployment,
* billing projections generate correct revenue / GP / margin,
* the management dashboard's monthly billable revenue **reconciles with the
  underlying records**,
* each role sees only its own view.

The reconciliation tests carry the most weight. A dashboard that disagrees with
the records it summarises is worse than no dashboard, and a projection presented
as billed revenue would make the platform's headline metric a lie.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.core.permissions import Role
from app.engines.billing.periods import (
    Period,
    amounts_for,
    coverage,
    period_of,
    periods_between,
    working_days,
)

API = "/api/v1"


# --------------------------------------------------------- period arithmetic


class TestPeriods:
    def test_a_deployment_touches_every_month_it_spans(self):
        labels = [p.label for p in periods_between(date(2026, 1, 20), date(2026, 3, 10))]
        assert labels == ["2026-01", "2026-02", "2026-03"]

    def test_a_single_month_deployment_produces_one_period(self):
        assert len(periods_between(date(2026, 5, 3), date(2026, 5, 20))) == 1

    def test_an_inverted_range_produces_nothing(self):
        assert periods_between(date(2026, 5, 20), date(2026, 5, 3)) == []

    def test_periods_roll_over_the_year_boundary(self):
        labels = [p.label for p in periods_between(date(2026, 11, 1), date(2027, 2, 1))]
        assert labels == ["2026-11", "2026-12", "2027-01", "2027-02"]

    def test_working_days_excludes_weekends(self):
        # 1-28 Feb 2026 contains exactly four full weeks.
        assert working_days(date(2026, 2, 1), date(2026, 2, 28)) == 20

    def test_a_month_outside_the_deployment_has_no_coverage(self):
        assert coverage(Period(2026, 6), start=date(2026, 1, 1), end=date(2026, 3, 1)) is None

    def test_an_open_ended_deployment_covers_the_whole_month(self):
        cover = coverage(Period(2026, 1), start=date(2026, 1, 1), end=None)
        assert cover is not None
        assert not cover.is_partial

    def test_period_of_reads_a_date(self):
        assert period_of(date(2026, 7, 15)).label == "2026-07"


class TestProRating:
    def test_a_partial_month_bills_less_than_a_full_one(self):
        full = coverage(Period(2026, 2), start=date(2026, 1, 1), end=date(2026, 12, 31))
        partial = coverage(Period(2026, 1), start=date(2026, 1, 20), end=date(2026, 12, 31))
        assert full is not None and partial is not None

        full_amounts = amounts_for(
            full, monthly_revenue=Decimal("22000"), monthly_cost=Decimal("14000")
        )
        partial_amounts = amounts_for(
            partial, monthly_revenue=Decimal("22000"), monthly_cost=Decimal("14000")
        )

        assert full_amounts.revenue == Decimal("22000.00")
        assert partial_amounts.revenue < full_amounts.revenue
        assert partial_amounts.is_partial is True
        assert full_amounts.is_partial is False

    def test_pro_rating_both_sides_keeps_the_margin_constant(self):
        """Charging full cost against partial revenue would invent a loss."""
        margins = []
        for period in periods_between(date(2026, 1, 20), date(2026, 3, 10)):
            cover = coverage(period, start=date(2026, 1, 20), end=date(2026, 3, 10))
            assert cover is not None
            amounts = amounts_for(
                cover, monthly_revenue=Decimal("22000"), monthly_cost=Decimal("14000")
            )
            margins.append(amounts.margin_percent)

        assert len(set(margins)) == 1, f"margin drifted across periods: {margins}"

    def test_the_months_sum_to_the_engagement(self):
        total_revenue = Decimal("0")
        for period in periods_between(date(2026, 1, 1), date(2026, 3, 31)):
            cover = coverage(period, start=date(2026, 1, 1), end=date(2026, 3, 31))
            assert cover is not None
            total_revenue += amounts_for(
                cover, monthly_revenue=Decimal("20000"), monthly_cost=Decimal("0")
            ).revenue

        # Three whole months at 20,000 each.
        assert total_revenue == Decimal("60000.00")

    def test_zero_revenue_does_not_divide_by_zero(self):
        cover = coverage(Period(2026, 2), start=date(2026, 2, 1), end=date(2026, 2, 28))
        assert cover is not None
        amounts = amounts_for(cover, monthly_revenue=Decimal("0"), monthly_cost=Decimal("100"))
        assert amounts.margin_percent is None


# ------------------------------------------------------------------ fixtures


async def _selected_submission(as_role):
    """A candidate carried all the way to SELECTED, ready to deploy."""
    hr, _ = await as_role(Role.HR_RESOURCING)
    resource = (
        await hr.post(
            f"{API}/resources",
            json={
                "full_name": f"Deployed Consultant {uuid.uuid4().hex[:6]}",
                "resource_type": "CONSULTANT",
                "availability_status": "AVAILABLE",
                "total_experience_years": 9,
                "current_location_country": "QA",
                "expected_cost_amount": "14000",
                "expected_cost_currency": "QAR",
                "expected_cost_unit": "MONTHLY",
            },
        )
    ).json()

    sales, _ = await as_role(Role.SALES)
    account = (
        await sales.post(
            f"{API}/accounts",
            json={
                "name": f"Delivery Client {uuid.uuid4().hex[:6]}",
                "account_type": "CUSTOMER",
                "country": "QA",
                "relationship_status": "ACTIVE",
                "is_existing_customer": True,
            },
        )
    ).json()
    requirement = (
        await sales.post(
            f"{API}/requirements",
            json={
                "title": f"Delivery Requirement {uuid.uuid4().hex[:6]}",
                "role": "SAP FICO Consultant",
                "positions": 1,
                "priority_source": "P1_EXISTING_CUSTOMER",
                "account_id": account["id"],
                "country": "QA",
                "duration_months": 12,
                "rate_max": "22000",
                "rate_currency": "QAR",
                "rate_unit": "MONTHLY",
            },
        )
    ).json()

    submission = (
        await sales.post(
            f"{API}/submissions",
            json={
                "requirement_id": requirement["id"],
                "resource_id": resource["id"],
                "proposed_bill_rate": "22000",
                "proposed_bill_currency": "QAR",
                "proposed_bill_unit": "MONTHLY",
            },
        )
    ).json()
    await sales.post(
        f"{API}/submissions/{submission['id']}/status",
        json={"status": "SELECTED", "note": "Client confirmed"},
    )
    return sales, submission, resource, requirement


@pytest.fixture
async def selected(as_role):
    return await _selected_submission(as_role)


@pytest.fixture
async def deployed(selected):
    """An active deployment starting on the first of last month."""
    client, submission, resource, requirement = selected
    start = (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)

    response = await client.post(
        f"{API}/deployments",
        json={
            "submission_id": submission["id"],
            "start_date": start.isoformat(),
            "end_date": (start + timedelta(days=200)).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return client, response.json(), resource, requirement


# --------------------------------------------------------------- deployments


class TestDeployments:
    async def test_selecting_a_candidate_creates_a_deployment(self, deployed):
        _, deployment, resource, _ = deployed

        assert deployment["resource_id"] == resource["id"]
        assert deployment["status"] == "ACTIVE"
        assert deployment["submission_id"]

    async def test_the_rates_are_copied_not_referenced(self, deployed, as_role):
        """A renegotiation in June must not rewrite March's billing."""
        _, deployment, _, _ = deployed

        # Read as Management, the only role that sees both sides of the money.
        client, _ = await as_role(Role.MANAGEMENT)
        stored = (await client.get(f"{API}/deployments/{deployment['id']}")).json()

        assert Decimal(stored["bill_rate"]) == Decimal("22000.00")
        assert Decimal(stored["cost_rate"]) == Decimal("14000.00")
        assert stored["bill_currency"] == "QAR"

    async def test_only_a_selected_candidate_can_be_deployed(self, as_role):
        hr, _ = await as_role(Role.HR_RESOURCING)
        resource = (
            await hr.post(
                f"{API}/resources",
                json={
                    "full_name": f"Not Selected {uuid.uuid4().hex[:6]}",
                    "resource_type": "CONSULTANT",
                    "availability_status": "AVAILABLE",
                },
            )
        ).json()

        sales, _ = await as_role(Role.SALES)
        requirement = (
            await sales.post(
                f"{API}/requirements",
                json={
                    "title": f"Undeployed {uuid.uuid4().hex[:6]}",
                    "role": "Developer",
                    "positions": 1,
                    "priority_source": "P1_EXISTING_CUSTOMER",
                },
            )
        ).json()
        submission = (
            await sales.post(
                f"{API}/submissions",
                json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
            )
        ).json()

        response = await sales.post(
            f"{API}/deployments",
            json={"submission_id": submission["id"], "start_date": date.today().isoformat()},
        )
        assert response.status_code == 422
        assert "selected" in response.text.lower()

    async def test_a_submission_cannot_be_deployed_twice(self, deployed, selected):
        client, _, _, _ = deployed
        _, submission, _, _ = selected

        response = await client.post(
            f"{API}/deployments",
            json={"submission_id": submission["id"], "start_date": date.today().isoformat()},
        )
        assert response.status_code == 409

    async def test_a_deployed_submission_reports_its_deployment(self, deployed, selected):
        """The submission must say it has been deployed.

        A submission stays SELECTED after the handover, so status alone cannot
        tell the UI whether Deploy is still possible. Without this the button is
        offered forever on a consultant already placed, and can only return 409.
        """
        client, deployment, _, _ = deployed
        _, submission, _, _ = selected

        listed = (await client.get(f"{API}/submissions")).json()
        row = next(item for item in listed if item["id"] == submission["id"])

        assert row["status"] == "SELECTED"
        assert row["deployment_id"] == deployment["id"]

    async def test_an_undeployed_submission_reports_no_deployment(self, selected):
        client, submission, _, _ = selected

        listed = (await client.get(f"{API}/submissions")).json()
        row = next(item for item in listed if item["id"] == submission["id"])

        assert row["deployment_id"] is None

    async def test_deploying_marks_the_consultant_as_deployed(self, deployed, as_role):
        _, deployment, resource, _ = deployed

        hr, _ = await as_role(Role.HR_RESOURCING)
        updated = (await hr.get(f"{API}/resources/{resource['id']}")).json()

        # Without this the bench radar would keep offering somebody already placed.
        assert updated["availability_status"] == "DEPLOYED"
        assert updated["available_from"] == deployment["end_date"]

    async def test_ending_a_deployment_frees_the_consultant(self, deployed, as_role):
        client, deployment, resource, _ = deployed
        ended_on = date.today()

        response = await client.post(
            f"{API}/deployments/{deployment['id']}/end",
            json={"actual_end_date": ended_on.isoformat(), "reason": "Project completed early"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ENDED"
        assert response.json()["end_reason"] == "Project completed early"

        hr, _ = await as_role(Role.HR_RESOURCING)
        updated = (await hr.get(f"{API}/resources/{resource['id']}")).json()
        assert updated["availability_status"] == "AVAILABLE"

    async def test_a_deployment_cannot_end_before_it_started(self, deployed):
        client, deployment, _, _ = deployed
        response = await client.post(
            f"{API}/deployments/{deployment['id']}/end",
            json={"actual_end_date": "2020-01-01"},
        )
        assert response.status_code == 422

    async def test_extending_creates_a_linked_successor(self, deployed):
        client, deployment, _, _ = deployed
        after = date.fromisoformat(deployment["end_date"]) + timedelta(days=1)

        response = await client.post(
            f"{API}/deployments/{deployment['id']}/extend",
            json={
                "start_date": after.isoformat(),
                "end_date": (after + timedelta(days=180)).isoformat(),
                "bill_rate": "24000",
            },
        )
        assert response.status_code == 201, response.text
        successor = response.json()

        # A new row, not an edit — the original's billing history stays intact.
        assert successor["id"] != deployment["id"]
        assert successor["extension_of_deployment_id"] == deployment["id"]
        assert Decimal(successor["bill_rate"]) == Decimal("24000.00")

    async def test_an_extension_cannot_overlap_the_original(self, deployed):
        client, deployment, _, _ = deployed
        response = await client.post(
            f"{API}/deployments/{deployment['id']}/extend",
            json={"start_date": deployment["start_date"]},
        )
        assert response.status_code == 422
        assert "overlap" in response.text.lower()

    async def test_ending_soon_is_ordered_by_urgency(self, deployed):
        client, _, _, _ = deployed
        rows = (await client.get(f"{API}/deployments/ending-soon?days_ahead=365")).json()
        days = [row["days_remaining"] for row in rows]
        assert days == sorted(days)

    async def test_deploying_is_audited(self, deployed, as_role):
        admin, _ = await as_role(Role.ADMIN)
        entries = (await admin.get(f"{API}/audit?action=DEPLOYMENT_CREATED")).json()
        assert entries["items"]


# ------------------------------------------------------------------ billing


class TestProjections:
    async def test_projections_cover_every_month_of_the_engagement(self, deployed):
        client, deployment, _, _ = deployed

        result = (
            await client.post(
                f"{API}/billing/generate-projections?deployment_id={deployment['id']}"
            )
        ).json()
        assert result["created"] > 0

        records = (
            await client.get(f"{API}/billing/records?deployment_id={deployment['id']}")
        ).json()
        expected = len(
            periods_between(
                date.fromisoformat(deployment["start_date"]),
                date.fromisoformat(deployment["end_date"]),
            )
        )
        assert len(records) == expected

    async def test_the_arithmetic_is_right(self, deployed):
        client, deployment, _, _ = deployed
        await client.post(f"{API}/billing/generate-projections?deployment_id={deployment['id']}")

        records = (
            await client.get(f"{API}/billing/records?deployment_id={deployment['id']}")
        ).json()
        full_months = [row for row in records if not row["is_estimated"] or row["billable_days"]]

        for row in full_months:
            revenue = Decimal(row["revenue_amount"])
            cost = Decimal(row["cost_amount"])
            assert Decimal(row["gross_profit"]) == revenue - cost
            if revenue > 0:
                assert row["margin_percent"] == pytest.approx(
                    float((revenue - cost) / revenue * 100), abs=0.02
                )

    async def test_regenerating_is_idempotent(self, deployed):
        client, deployment, _, _ = deployed
        first = (
            await client.post(
                f"{API}/billing/generate-projections?deployment_id={deployment['id']}"
            )
        ).json()
        second = (
            await client.post(
                f"{API}/billing/generate-projections?deployment_id={deployment['id']}"
            )
        ).json()

        assert second["created"] == 0
        assert second["updated"] == first["created"]

    async def test_regenerating_never_overwrites_a_confirmed_month(self, deployed):
        client, deployment, _, _ = deployed
        await client.post(f"{API}/billing/generate-projections?deployment_id={deployment['id']}")

        records = (
            await client.get(f"{API}/billing/records?deployment_id={deployment['id']}")
        ).json()
        target = records[-1]
        await client.post(
            f"{API}/billing/records/{target['id']}/confirm",
            json={"revenue_amount": "19500", "notes": "Client withheld two days"},
        )

        result = (
            await client.post(
                f"{API}/billing/generate-projections?deployment_id={deployment['id']}"
            )
        ).json()
        assert result["protected"] >= 1

        after = (await client.get(f"{API}/billing/records?deployment_id={deployment['id']}")).json()
        confirmed = next(row for row in after if row["id"] == target["id"])
        assert Decimal(confirmed["revenue_amount"]) == Decimal("19500.00")
        assert confirmed["status"] == "CONFIRMED"

    async def test_a_deployment_with_no_bill_rate_cannot_be_projected(self, deployed):
        client, deployment, _, _ = deployed
        await client.patch(f"{API}/deployments/{deployment['id']}", json={"bill_rate": None})

        response = await client.post(
            f"{API}/billing/generate-projections?deployment_id={deployment['id']}"
        )
        assert response.status_code == 422
        assert "bill rate" in response.text.lower()

    async def test_ending_early_cancels_the_months_that_never_happened(self, deployed):
        client, deployment, _, _ = deployed
        await client.post(f"{API}/billing/generate-projections?deployment_id={deployment['id']}")

        await client.post(
            f"{API}/deployments/{deployment['id']}/end",
            json={"actual_end_date": date.today().isoformat(), "reason": "Ended early"},
        )

        records = (
            await client.get(f"{API}/billing/records?deployment_id={deployment['id']}")
        ).json()
        cancelled = [row for row in records if row["status"] == "CANCELLED"]
        assert cancelled, "future projections must not survive an early end"


class TestConfirmation:
    async def test_confirming_turns_a_projection_into_a_real_number(self, deployed):
        client, deployment, _, _ = deployed
        await client.post(f"{API}/billing/generate-projections?deployment_id={deployment['id']}")
        record = (
            await client.get(f"{API}/billing/records?deployment_id={deployment['id']}")
        ).json()[0]

        assert record["status"] == "PROJECTED"
        assert record["is_estimated"] is True

        confirmed = (
            await client.post(f"{API}/billing/records/{record['id']}/confirm", json={})
        ).json()
        assert confirmed["status"] == "CONFIRMED"
        assert confirmed["is_estimated"] is False

    async def test_a_correction_recomputes_profit_and_margin(self, deployed):
        client, deployment, _, _ = deployed
        await client.post(f"{API}/billing/generate-projections?deployment_id={deployment['id']}")
        record = (
            await client.get(f"{API}/billing/records?deployment_id={deployment['id']}")
        ).json()[0]

        confirmed = (
            await client.post(
                f"{API}/billing/records/{record['id']}/confirm",
                json={"revenue_amount": "20000", "cost_amount": "15000"},
            )
        ).json()

        assert Decimal(confirmed["gross_profit"]) == Decimal("5000.00")
        assert confirmed["margin_percent"] == pytest.approx(25.0)

    async def test_confirmation_is_audited(self, deployed, as_role):
        client, deployment, _, _ = deployed
        await client.post(f"{API}/billing/generate-projections?deployment_id={deployment['id']}")
        record = (
            await client.get(f"{API}/billing/records?deployment_id={deployment['id']}")
        ).json()[0]
        await client.post(f"{API}/billing/records/{record['id']}/confirm", json={})

        admin, _ = await as_role(Role.ADMIN)
        entries = (await admin.get(f"{API}/audit?action=BILLING_CONFIRMED")).json()
        assert entries["items"]


class TestManualBillingEntry:
    """ASSUMPTIONS.md A15: billing is "manual entry or Excel import"."""

    async def test_a_month_can_be_recorded_by_hand(self, deployed):
        client, deployment, _, _ = deployed

        response = await client.post(
            f"{API}/billing/records",
            json={
                "deployment_id": deployment["id"],
                "period_year": 2025,
                "period_month": 11,
                "revenue_amount": "18000",
                "cost_amount": "12000",
                "notes": "Pre-system month, taken from the invoice",
            },
        )
        assert response.status_code == 201, response.text
        record = response.json()

        # Typed by a human, so not an estimate and confirmed by default.
        assert record["status"] == "CONFIRMED"
        assert record["is_estimated"] is False
        assert Decimal(record["gross_profit"]) == Decimal("6000.00")
        assert record["margin_percent"] == pytest.approx(33.33, abs=0.01)

    async def test_a_hand_entered_month_counts_as_revenue(self, deployed):
        client, deployment, _, _ = deployed
        before = (await client.get(f"{API}/billing/summary?months=36")).json()
        before_total = sum(Decimal(row["confirmed_revenue"]) for row in before)

        await client.post(
            f"{API}/billing/records",
            json={
                "deployment_id": deployment["id"],
                "period_year": 2025,
                "period_month": 10,
                "revenue_amount": "20000",
                "cost_amount": "13000",
            },
        )

        after = (await client.get(f"{API}/billing/summary?months=36")).json()
        after_total = sum(Decimal(row["confirmed_revenue"]) for row in after)
        assert after_total == before_total + Decimal("20000")

    async def test_a_second_row_for_the_same_month_is_refused(self, deployed):
        client, deployment, _, _ = deployed
        payload = {
            "deployment_id": deployment["id"],
            "period_year": 2025,
            "period_month": 9,
            "revenue_amount": "15000",
        }
        assert (await client.post(f"{API}/billing/records", json=payload)).status_code == 201
        duplicate = await client.post(f"{API}/billing/records", json=payload)
        assert duplicate.status_code == 409
        assert "already has a billing row" in duplicate.text

    async def test_an_unknown_deployment_is_a_404(self, deployed):
        client, _, _, _ = deployed
        response = await client.post(
            f"{API}/billing/records",
            json={
                "deployment_id": str(uuid.uuid4()),
                "period_year": 2025,
                "period_month": 8,
                "revenue_amount": "1000",
            },
        )
        assert response.status_code == 404

    async def test_a_correction_re_derives_profit_and_margin(self, deployed):
        client, deployment, _, _ = deployed
        record = (
            await client.post(
                f"{API}/billing/records",
                json={
                    "deployment_id": deployment["id"],
                    "period_year": 2025,
                    "period_month": 7,
                    "revenue_amount": "20000",
                    "cost_amount": "14000",
                },
            )
        ).json()

        corrected = (
            await client.patch(
                f"{API}/billing/records/{record['id']}",
                json={"revenue_amount": "18000", "notes": "Client withheld two days"},
            )
        ).json()

        # Profit is never taken from the caller — that is how a total stops
        # reconciling.
        assert Decimal(corrected["gross_profit"]) == Decimal("4000.00")
        assert corrected["margin_percent"] == pytest.approx(22.22, abs=0.01)
        assert corrected["notes"] == "Client withheld two days"

    async def test_manual_entry_is_audited(self, deployed, as_role):
        client, deployment, _, _ = deployed
        await client.post(
            f"{API}/billing/records",
            json={
                "deployment_id": deployment["id"],
                "period_year": 2025,
                "period_month": 6,
                "revenue_amount": "9000",
            },
        )

        admin, _ = await as_role(Role.ADMIN)
        entries = (await admin.get(f"{API}/audit?action=BILLING_CREATED")).json()
        assert entries["items"]

    async def test_resourcing_cannot_record_billing(self, deployed, as_role):
        _, deployment, _, _ = deployed
        client, _ = await as_role(Role.HR_RESOURCING)

        response = await client.post(
            f"{API}/billing/records",
            json={
                "deployment_id": deployment["id"],
                "period_year": 2025,
                "period_month": 5,
                "revenue_amount": "1000",
            },
        )
        assert response.status_code == 403


class TestHeadlineMetric:
    async def test_projected_revenue_is_never_counted_as_confirmed(self, deployed):
        """ASSUMPTIONS.md A15 — the single most misleading thing this could do."""
        client, deployment, _, _ = deployed
        await client.post(f"{API}/billing/generate-projections?deployment_id={deployment['id']}")

        headline = (await client.get(f"{API}/billing/monthly-revenue")).json()
        assert Decimal(headline["projected_revenue"]) > 0
        assert headline["unconfirmed_periods"] > 0

    async def test_confirming_moves_revenue_from_projected_to_confirmed(self, deployed):
        client, deployment, _, _ = deployed
        await client.post(f"{API}/billing/generate-projections?deployment_id={deployment['id']}")

        before = (await client.get(f"{API}/billing/monthly-revenue")).json()
        records = (
            await client.get(f"{API}/billing/records?deployment_id={deployment['id']}")
        ).json()
        current = next(
            row
            for row in records
            if (row["period_year"], row["period_month"]) == (date.today().year, date.today().month)
        )
        await client.post(f"{API}/billing/records/{current['id']}/confirm", json={})

        after = (await client.get(f"{API}/billing/monthly-revenue")).json()
        assert Decimal(after["confirmed_revenue"]) > Decimal(before["confirmed_revenue"])
        assert Decimal(after["projected_revenue"]) < Decimal(before["projected_revenue"])

    async def test_the_summary_reconciles_with_the_records(self, deployed):
        """The Phase 11 definition of done."""
        client, deployment, _, _ = deployed
        await client.post(f"{API}/billing/generate-projections?deployment_id={deployment['id']}")

        records = (
            await client.get(f"{API}/billing/records?deployment_id={deployment['id']}")
        ).json()
        summary = (await client.get(f"{API}/billing/summary?months=36")).json()
        by_period = {row["period"]: row for row in summary}

        for record in records:
            if record["status"] != "PROJECTED":
                continue
            period = f"{record['period_year']:04d}-{record['period_month']:02d}"
            assert period in by_period
            # This deployment's row must be inside the period total.
            assert Decimal(by_period[period]["projected_revenue"]) >= Decimal(
                record["revenue_amount"]
            )


# --------------------------------------------------------------- dashboards


class TestDashboards:
    async def test_the_funnel_reconciles_with_the_pipeline_board(self, deployed):
        client, _, _, _ = deployed

        funnel = (await client.get(f"{API}/dashboard/funnel")).json()
        board = (await client.get(f"{API}/opportunities")).json()

        counted = sum(item["count"] for item in funnel["stages"]) + sum(
            item["count"] for item in funnel["closed"]
        )
        assert counted == len(board), "the funnel must not disagree with the board"

    async def test_management_sees_revenue_and_the_funnel(self, deployed, as_role):
        client, deployment, _, _ = deployed
        await client.post(f"{API}/billing/generate-projections?deployment_id={deployment['id']}")

        management, _ = await as_role(Role.MANAGEMENT)
        body = (await management.get(f"{API}/dashboard/management")).json()

        assert "headline" in body
        assert "trend" in body
        assert body["active_deployments"] >= 1
        assert "funnel" in body

    async def test_the_management_headline_reconciles_with_the_summary(self, deployed, as_role):
        client, deployment, _, _ = deployed
        await client.post(f"{API}/billing/generate-projections?deployment_id={deployment['id']}")
        summary = (await client.get(f"{API}/billing/summary?months=36")).json()

        management, _ = await as_role(Role.MANAGEMENT)
        dashboard = (await management.get(f"{API}/dashboard/management")).json()

        headline = dashboard["headline"]
        if headline["period"]:
            matching = next(row for row in summary if row["period"] == headline["period"])
            assert Decimal(headline["confirmed_revenue"]) == Decimal(matching["confirmed_revenue"])
            assert Decimal(headline["projected_revenue"]) == Decimal(matching["projected_revenue"])

    async def test_sales_sees_its_own_work(self, deployed, as_role):
        client, _ = await as_role(Role.SALES)
        body = (await client.get(f"{API}/dashboard/sales")).json()

        for key in (
            "my_open_opportunities",
            "overdue_next_actions",
            "live_submissions",
            "interviews_next_14_days",
            "top_opportunities",
        ):
            assert key in body

    async def test_resourcing_sees_capacity_and_expiry(self, deployed, as_role):
        client, _ = await as_role(Role.HR_RESOURCING)
        body = (await client.get(f"{API}/dashboard/hr")).json()

        for key in (
            "bench_count",
            "deployed_count",
            "awaiting_review",
            "deployments_ending_30d",
            "bench_without_a_suggestion",
            "documents_expired",
        ):
            assert key in body
        assert body["deployed_count"] >= 1

    async def test_admin_sees_system_health_and_data_gaps(self, as_role):
        client, _ = await as_role(Role.ADMIN)
        body = (await client.get(f"{API}/dashboard/admin")).json()

        assert body["active_users"] >= 1
        assert "top_actions_7d" in body
        # A scoring engine is only as good as what has been recorded.
        assert "active_requirements_unscored" in body
        assert "active_requirements_unpriced" in body


class TestDashboardAuthorization:
    async def test_each_role_reaches_only_its_own_dashboard(self, as_role):
        expected = {
            Role.MANAGEMENT: "management",
            Role.SALES: "sales",
            Role.HR_RESOURCING: "hr",
        }
        for role, allowed in expected.items():
            client, _ = await as_role(role)
            for name in ("management", "sales", "hr", "admin"):
                response = await client.get(f"{API}/dashboard/{name}")
                if name == allowed:
                    assert response.status_code == 200, f"{role.value} -> {name}"
                else:
                    assert response.status_code == 403, f"{role.value} -> {name}"

    async def test_admin_reaches_every_dashboard(self, as_role):
        client, _ = await as_role(Role.ADMIN)
        for name in ("management", "sales", "hr", "admin"):
            assert (await client.get(f"{API}/dashboard/{name}")).status_code == 200


class TestDeliveryAuthorization:
    async def test_resourcing_cannot_read_billing(self, as_role):
        client, _ = await as_role(Role.HR_RESOURCING)
        assert (await client.get(f"{API}/billing/records")).status_code == 403

    async def test_management_may_read_billing_but_not_write(self, deployed, as_role):
        client, _ = await as_role(Role.MANAGEMENT)
        assert (await client.get(f"{API}/billing/records")).status_code == 200
        assert (await client.post(f"{API}/billing/generate-projections")).status_code == 403

    async def test_resourcing_sees_cost_but_not_the_bill_rate(self, deployed, as_role):
        _, deployment, _, _ = deployed
        client, _ = await as_role(Role.HR_RESOURCING)

        body = (await client.get(f"{API}/deployments/{deployment['id']}")).json()
        assert body["cost_rate"] is not None
        assert body["bill_rate"] is None
        assert "bill_rate" in body["restricted_fields"]

    async def test_sales_sees_the_bill_rate_but_not_the_cost(self, deployed, as_role):
        _, deployment, _, _ = deployed
        client, _ = await as_role(Role.SALES)

        body = (await client.get(f"{API}/deployments/{deployment['id']}")).json()
        assert body["bill_rate"] is not None
        assert body["cost_rate"] is None
        assert "cost_rate" in body["restricted_fields"]

    async def test_management_sees_both_sides(self, deployed, as_role):
        _, deployment, _, _ = deployed
        client, _ = await as_role(Role.MANAGEMENT)

        body = (await client.get(f"{API}/deployments/{deployment['id']}")).json()
        assert body["bill_rate"] is not None
        assert body["cost_rate"] is not None
        assert body["restricted_fields"] == []

    async def test_an_unknown_deployment_is_a_404(self, as_role):
        client, _ = await as_role(Role.SALES)
        assert (await client.get(f"{API}/deployments/{uuid.uuid4()}")).status_code == 404
