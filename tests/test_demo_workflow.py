"""The end-to-end demo workflow.

One test that walks the whole platform in the order a real engagement happens,
using only the public API and switching roles exactly as the four real users
would. It exists to catch the failures unit tests structurally cannot: a phase
that works alone but does not hand off correctly to the next one.

If this passes, V1 does what the SOW says it does.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.permissions import Role

API = "/api/v1"

JD_TEXT = """Senior SAP FICO Consultant

Location: Doha, Qatar
Contract: 12 months, onsite
Rate: QAR 20,000 - 24,000 per month

We are looking for a Senior SAP FICO Consultant to join a finance
transformation programme for a Qatari logistics operator.

Requirements:
 - 6+ years of SAP FICO experience (mandatory)
 - SAP S/4HANA implementation experience (mandatory)
 - Power BI is nice to have

Notice period: candidates must be available within 45 days.
"""

CV_TEXT = """Imran Qureshi
Senior SAP FICO Consultant
imran.qureshi@example.com | +974 5555 3210 | Doha, Qatar

PROFESSIONAL SUMMARY
SAP FICO consultant with S/4HANA finance rollouts across Gulf logistics and
energy clients, working with onshore and offshore delivery teams.

EXPERIENCE

Senior SAP FICO Consultant, Northline Systems
Mar 2018 - Present
 - Led an S/4HANA finance migration for a Qatari logistics operator.

SAP FICO Consultant, Cedar Technology
Jan 2015 - Feb 2018

SKILLS
SAP FICO, SAP S/4HANA, SAP MM, Power BI

Notice Period: 30 days
"""


class TestDemoWorkflow:
    """The 18 steps, in order, as four different people."""

    async def test_the_full_engagement_runs_end_to_end(self, as_role):
        marker = uuid.uuid4().hex[:8]

        # --- 1. Sales signs in and records the client -------------------
        sales, sales_user = await as_role(Role.SALES)
        account = (
            await sales.post(
                f"{API}/accounts",
                json={
                    "name": f"Milaha Demo {marker}",
                    "account_type": "CUSTOMER",
                    "country": "QA",
                    "city": "Doha",
                    "relationship_status": "ACTIVE",
                    "is_existing_customer": True,
                    "is_approved_vendor": True,
                    "has_msa": True,
                    "contract_outsourcing_friendly": True,
                },
            )
        ).json()
        assert account["id"]

        # --- 2. ...and the decision maker on it -------------------------
        contact = (
            await sales.post(
                f"{API}/contacts",
                json={
                    "account_id": account["id"],
                    "full_name": f"Procurement Lead {marker}",
                    "job_title": "Head of Procurement",
                    "email": f"procurement-{marker}@example.com",
                    "is_decision_maker": True,
                },
            )
        ).json()
        assert contact["is_decision_maker"] is True

        # --- 3. A job description arrives and is parsed -----------------
        # `parse-text` returns the draft requirement; the field-level parse
        # detail (confidence, evidence spans) is a separate read.
        parse_response = await sales.post(f"{API}/requirements/parse-text", json={"text": JD_TEXT})
        assert parse_response.status_code == 201, parse_response.text
        requirement_id = parse_response.json()["id"]

        parsed = (await sales.get(f"{API}/requirements/{requirement_id}/parse-result")).json()
        assert parsed["fields"], "the parser must produce structured fields"

        # --- 4. Parsed AI output is reviewed by a human (AD-7) ----------
        before_review = (await sales.get(f"{API}/requirements/{requirement_id}")).json()
        assert before_review["review_status"] == "PENDING_REVIEW"

        accepted = await sales.post(
            f"{API}/requirements/{requirement_id}/accept-parse",
            json={
                "confirmed_fields": parsed["confirmation_required"],
                "updates": {
                    "account_id": account["id"],
                    "country": "QA",
                    "location": "Doha",
                    "work_mode": "ONSITE",
                    "experience_min_years": 6,
                    "duration_months": 12,
                    "rate_max": "24000",
                    "rate_currency": "QAR",
                    "rate_unit": "MONTHLY",
                    "response_deadline_at": (datetime.now(UTC) + timedelta(hours=30)).isoformat(),
                },
            },
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["review_status"] == "ACCEPTED"

        # --- 5. Resourcing uploads a CV --------------------------------
        hr, _ = await as_role(Role.HR_RESOURCING)
        cv = await hr.post(
            f"{API}/resources/parse-cv",
            files={"file": ("imran.txt", CV_TEXT.encode(), "text/plain")},
        )
        assert cv.status_code == 201, cv.text
        resource_id = cv.json()["resource_id"]

        # --- 6. ...and accepts the parse, making it business data ------
        pending = (await hr.get(f"{API}/resources/{resource_id}")).json()
        assert pending["review_status"] == "PENDING_REVIEW"

        confirmed = await hr.post(
            f"{API}/resources/{resource_id}/accept-parse",
            json={
                "confirmed_fields": cv.json()["confirmation_required"],
                "updates": {
                    "availability_status": "AVAILABLE",
                    "current_location_country": "QA",
                    "current_location_city": "Doha",
                    "total_experience_years": 10,
                    "expected_cost_amount": "15000",
                    "expected_cost_currency": "QAR",
                    "expected_cost_unit": "MONTHLY",
                    "notice_period_days": 0,
                },
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["review_status"] == "ACCEPTED"

        # --- 7. Forward matching finds the candidate -------------------
        sales, _ = await as_role(Role.SALES)
        run = await sales.post(f"{API}/matching/requirements/{requirement_id}/run?limit=25")
        assert run.status_code == 201, run.text
        matches = run.json()["matches"]

        mine = next((m for m in matches if m["resource_id"] == resource_id), None)
        assert mine is not None, "the accepted consultant must be matchable"

        # --- 8. ...and the match explains itself (MATCHING.md section 5)
        assert mine["components"], "a bare percentage is not a match"
        assert mine["narrative"]
        assert mine["band"]

        # --- 9. Reverse matching answers the other question ------------
        hr, _ = await as_role(Role.HR_RESOURCING)
        reverse = await hr.post(f"{API}/reverse-matching/resources/{resource_id}/run?limit=50")
        assert reverse.status_code == 201, reverse.text
        suggestion = next(
            (
                item
                for item in reverse.json()["suggestions"]
                if item["requirement_id"] == requirement_id
            ),
            None,
        )
        assert suggestion is not None
        assert suggestion["route"]["route_type"] == "DIRECT", "MSA customer is direct"

        # --- 10. The same pair scores identically both ways ------------
        assert suggestion["overall_score"] == mine["overall_score"]

        # --- 11. Scoring answers "should we pursue this at all?" -------
        sales, _ = await as_role(Role.SALES)
        score = await sales.post(f"{API}/scoring/requirements/{requirement_id}/recompute")
        assert score.status_code == 201, score.text
        scored = score.json()

        assert scored["components"], "no score is ever a bare number"
        assert len(scored["factors"]) == 8, "all eight addressability factors"
        assert scored["recommended_action"]
        assert scored["commercial"] is not None, "Sales sees the commercial figures"

        # --- 12. The opportunity opens and the candidate goes forward --
        submission = await sales.post(
            f"{API}/submissions",
            json={
                "requirement_id": requirement_id,
                "resource_id": resource_id,
                "proposed_bill_rate": "23000",
                "proposed_bill_currency": "QAR",
                "proposed_bill_unit": "MONTHLY",
            },
        )
        assert submission.status_code == 201, submission.text
        submission_id = submission.json()["id"]
        opportunity_id = submission.json()["opportunity_id"]
        assert opportunity_id, "submitting opens the opportunity"

        # --- 13. A duplicate submission is refused, with the detail ----
        duplicate = await sales.post(
            f"{API}/submissions",
            json={"requirement_id": requirement_id, "resource_id": resource_id},
        )
        assert duplicate.status_code == 409
        fields = {d["field"]: d["message"] for d in duplicate.json()["error"]["details"]}
        assert fields["current_status"] == "SUBMITTED"
        assert fields["submitted_by"] == sales_user.full_name

        # --- 14. An interview is scheduled and raises a reminder -------
        interview = await sales.post(
            f"{API}/interviews",
            json={
                "submission_id": submission_id,
                "scheduled_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
                "mode": "ONSITE",
                "interviewer_name": f"Procurement Lead {marker}",
            },
        )
        assert interview.status_code == 201, interview.text
        assert interview.json()["reminder_sent_at"] is not None

        # --- 15. Passing it selects the candidate ----------------------
        outcome = await sales.post(
            f"{API}/interviews/{interview.json()['id']}/outcome",
            json={"outcome": "PASSED", "feedback": "Strong technical round"},
        )
        assert outcome.status_code == 200
        assert (await sales.get(f"{API}/submissions/{submission_id}")).json()[
            "status"
        ] == "SELECTED"

        # --- 16. The deployment is created from the selection ----------
        start = date.today().replace(day=1)
        deployment = await sales.post(
            f"{API}/deployments",
            json={
                "submission_id": submission_id,
                "start_date": start.isoformat(),
                "end_date": (start + timedelta(days=180)).isoformat(),
            },
        )
        assert deployment.status_code == 201, deployment.text
        deployment_id = deployment.json()["id"]
        assert deployment.json()["status"] == "ACTIVE"

        # ...and the consultant is no longer on the bench
        hr, _ = await as_role(Role.HR_RESOURCING)
        placed = (await hr.get(f"{API}/resources/{resource_id}")).json()
        assert placed["availability_status"] == "DEPLOYED"

        # --- 17. Billing projects, then a human confirms a month -------
        sales, _ = await as_role(Role.SALES)
        projections = await sales.post(
            f"{API}/billing/generate-projections?deployment_id={deployment_id}"
        )
        assert projections.status_code == 200
        assert projections.json()["created"] > 0

        records = (await sales.get(f"{API}/billing/records?deployment_id={deployment_id}")).json()
        assert all(row["status"] == "PROJECTED" for row in records)

        current = next(
            row
            for row in records
            if (row["period_year"], row["period_month"]) == (date.today().year, date.today().month)
        )
        confirmed_row = await sales.post(f"{API}/billing/records/{current['id']}/confirm", json={})
        assert confirmed_row.status_code == 200
        assert confirmed_row.json()["status"] == "CONFIRMED"

        # --- 18. Management sees revenue, and it reconciles ------------
        management, _ = await as_role(Role.MANAGEMENT)
        dashboard = (await management.get(f"{API}/dashboard/management")).json()
        summary = (await management.get(f"{API}/billing/summary?months=36")).json()

        headline = dashboard["headline"]
        period = next(row for row in summary if row["period"] == headline["period"])
        assert Decimal(headline["confirmed_revenue"]) == Decimal(period["confirmed_revenue"])
        assert Decimal(headline["confirmed_revenue"]) > 0
        # A projection is never counted as earned revenue (ASSUMPTIONS.md A15).
        assert Decimal(headline["projected_revenue"]) >= 0
        assert dashboard["active_deployments"] >= 1

        # --- and the whole journey left an audit trail ----------------
        admin, _ = await as_role(Role.ADMIN)
        for action in (
            "ACCOUNT_CREATED",
            "JD_PARSED",
            "CV_PARSED",
            "MATCH_GENERATED",
            "REVERSE_MATCH_GENERATED",
            "SCORE_COMPUTED",
            "OPPORTUNITY_CREATED",
            "CV_SUBMITTED",
            "INTERVIEW_CREATED",
            "DEPLOYMENT_CREATED",
            "BILLING_CONFIRMED",
        ):
            entries = (await admin.get(f"{API}/audit?action={action}")).json()
            assert entries["items"], f"{action} left no audit trail"


class TestDemoWorkflowGuardrails:
    """The refusals the walkthrough depends on. Each one protects a real mistake."""

    async def test_an_unreviewed_consultant_cannot_be_submitted(self, as_role):
        hr, _ = await as_role(Role.HR_RESOURCING)
        cv = await hr.post(
            f"{API}/resources/parse-cv",
            files={"file": ("cv.txt", CV_TEXT.encode(), "text/plain")},
        )
        pending_id = cv.json()["resource_id"]

        sales, _ = await as_role(Role.SALES)
        requirement = (
            await sales.post(
                f"{API}/requirements",
                json={
                    "title": f"Guardrail {uuid.uuid4().hex[:6]}",
                    "role": "Consultant",
                    "positions": 1,
                    "priority_source": "P1_EXISTING_CUSTOMER",
                },
            )
        ).json()

        response = await sales.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": pending_id},
        )
        assert response.status_code == 422
        assert "review" in response.text.lower()

    async def test_a_candidate_who_was_not_selected_cannot_be_deployed(self, as_role):
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
                    "title": f"Guardrail {uuid.uuid4().hex[:6]}",
                    "role": "Consultant",
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

    @pytest.mark.parametrize(
        ("role", "path", "expected"),
        [
            (Role.SALES, "/dashboard/management", 403),
            (Role.HR_RESOURCING, "/billing/records", 403),
            (Role.MANAGEMENT, "/imports/entities", 403),
            (Role.SALES, "/scoring/configurations", 200),
        ],
    )
    async def test_role_boundaries_hold_across_the_journey(self, as_role, role, path, expected):
        client, _ = await as_role(role)
        assert (await client.get(f"{API}{path}")).status_code == expected
