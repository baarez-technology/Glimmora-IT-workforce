"""Phase 5: requirement lifecycle, the review gate and the SLA board."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.permissions import Role

API = "/api/v1"

JD_TEXT = """Job Title: Senior SAP FICO Consultant
Client: Milaha
Location: Doha, Qatar
Contract Type: Contract
Duration: 18 months contract
No. of Positions: 2

Minimum 8 years of experience in SAP S/4HANA. Must be hands-on with SAP FICO
and SAP MM. Power BI knowledge is desirable.

Rate: QAR 18,000 - 22,000 per month
Please submit profiles within 48 hours.
"""

#: Everything the review gate insists on before a parse can be accepted.
MONEY_AND_DATE_FIELDS = [
    "rate_min",
    "rate_max",
    "rate_currency",
    "rate_unit",
    "response_deadline_at",
]


def requirement_payload(**overrides):
    payload = {
        "title": f"Test Requirement {uuid.uuid4().hex[:8]}",
        "role": "Java Developer",
        "positions": 1,
        "priority_source": "P1_EXISTING_CUSTOMER",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
async def sales(as_role):
    return await as_role(Role.SALES)


@pytest.fixture
async def parsed(sales):
    """A requirement created by parsing a JD, still awaiting review."""
    client, _ = sales
    response = await client.post(f"{API}/requirements/parse-text", json={"text": JD_TEXT})
    assert response.status_code == 201, response.text
    return response.json()


class TestManualCreation:
    async def test_a_typed_requirement_is_business_data_immediately(self, sales):
        """Only AI output needs a review gate; a human typed this."""
        client, user = sales

        response = await client.post(f"{API}/requirements", json=requirement_payload())
        assert response.status_code == 201

        body = response.json()
        assert body["review_status"] == "ACCEPTED"
        assert body["needs_review"] is False
        assert body["status"] == "NEW"
        assert body["source"] == "MANUAL"
        assert body["owner_id"] == str(user.id)

    async def test_skills_are_resolved_onto_the_master(self, sales):
        client, _ = sales

        response = await client.post(
            f"{API}/requirements",
            json=requirement_payload(
                skills=[
                    {"name": "k8s", "importance": "MANDATORY"},
                    {"name": "Java", "importance": "MANDATORY", "min_years": 5},
                ]
            ),
        )
        assert response.status_code == 201

        names = {skill["name"] for skill in response.json()["skills"]}
        # "k8s" is an alias, so it must land on the canonical skill.
        assert names == {"Kubernetes", "Java"}

    async def test_a_rate_without_a_unit_is_rejected(self, sales):
        client, _ = sales
        response = await client.post(
            f"{API}/requirements", json=requirement_payload(rate_min="15000")
        )
        assert response.status_code == 422

    async def test_an_inverted_rate_range_is_rejected(self, sales):
        client, _ = sales
        response = await client.post(
            f"{API}/requirements",
            json=requirement_payload(rate_min="22000", rate_max="18000", rate_unit="MONTHLY"),
        )
        assert response.status_code == 422

    async def test_an_inverted_experience_range_is_rejected(self, sales):
        client, _ = sales
        response = await client.post(
            f"{API}/requirements",
            json=requirement_payload(experience_min_years=8, experience_max_years=5),
        )
        assert response.status_code == 422

    async def test_a_vms_requirement_gets_a_default_submission_window(self, sales):
        """P5 is the one channel that reliably imposes a deadline (A11)."""
        client, _ = sales

        response = await client.post(
            f"{API}/requirements", json=requirement_payload(priority_source="P5_VENDOR_MSP_VMS")
        )
        assert response.json()["response_deadline_at"] is not None

    async def test_other_sources_get_no_invented_deadline(self, sales):
        client, _ = sales
        response = await client.post(
            f"{API}/requirements", json=requirement_payload(priority_source="P1_EXISTING_CUSTOMER")
        )
        assert response.json()["response_deadline_at"] is None
        assert response.json()["deadline"] is None

    async def test_an_unknown_account_is_a_404(self, sales):
        client, _ = sales
        response = await client.post(
            f"{API}/requirements", json=requirement_payload(account_id=str(uuid.uuid4()))
        )
        assert response.status_code == 404


class TestJDParsing:
    async def test_parsing_creates_a_draft_awaiting_review(self, parsed):
        assert parsed["review_status"] == "PENDING_REVIEW"
        assert parsed["needs_review"] is True
        assert parsed["status"] == "PARSED"
        assert parsed["source"] == "JD_PASTE"
        assert parsed["title"] == "Senior SAP FICO Consultant"

    async def test_the_parse_populates_the_structured_fields(self, parsed):
        assert parsed["location"] == "Doha, Qatar"
        assert parsed["country"] == "QA"
        assert parsed["duration_months"] == 18
        assert parsed["positions"] == 2
        assert parsed["experience_min_years"] == 8
        assert parsed["rate_currency"] == "QAR"
        assert parsed["response_deadline_at"] is not None

    async def test_parsed_skills_land_on_the_master(self, parsed):
        names = {skill["name"] for skill in parsed["skills"]}
        assert {"SAP FICO", "SAP S/4HANA", "SAP MM"} <= names

        importance = {s["name"]: s["importance"] for s in parsed["skills"]}
        assert importance["SAP FICO"] == "MANDATORY"
        assert importance.get("Power BI") == "PREFERRED"

    async def test_the_source_text_is_kept_for_the_review_screen(self, parsed):
        assert "Senior SAP FICO Consultant" in parsed["description_raw"]

    async def test_the_parse_result_exposes_confidence_and_evidence(self, sales, parsed):
        client, _ = sales
        response = await client.get(f"{API}/requirements/{parsed['id']}/parse-result")
        assert response.status_code == 200

        body = response.json()
        assert body["provider"] == "null"
        assert body["used_fallback"] is False
        assert body["source_text"].startswith("Job Title:")

        by_field = {field["field"]: field for field in body["fields"]}
        assert by_field["title"]["level"] == "HIGH"
        assert by_field["title"]["evidence"] == "Senior SAP FICO Consultant"
        assert by_field["title"]["evidence_start"] is not None

    async def test_money_and_dates_always_require_confirmation(self, sales, parsed):
        client, _ = sales
        body = (await client.get(f"{API}/requirements/{parsed['id']}/parse-result")).json()

        required = set(body["confirmation_required"])
        for field in MONEY_AND_DATE_FIELDS:
            assert field in required, f"{field} must never auto-accept"

    async def test_a_short_text_is_rejected_before_parsing(self, sales):
        client, _ = sales
        response = await client.post(f"{API}/requirements/parse-text", json={"text": "too short"})
        assert response.status_code == 422

    async def test_parsing_is_audited(self, as_role, parsed):
        admin, _ = await as_role(Role.ADMIN)
        logs = (await admin.get(f"{API}/audit", params={"action": "JD_PARSED"})).json()
        assert any("Senior SAP FICO Consultant" in entry["summary"] for entry in logs["items"])


class TestReviewGate:
    async def test_an_unreviewed_requirement_cannot_be_qualified(self, sales, parsed):
        """AI output is not business data until a human accepts it (AD-7)."""
        client, _ = sales

        response = await client.post(
            f"{API}/requirements/{parsed['id']}/status", json={"status": "QUALIFIED"}
        )
        assert response.status_code == 409
        assert "review" in response.json()["error"]["message"].lower()

    async def test_accepting_without_confirming_money_is_refused(self, sales, parsed):
        client, _ = sales

        response = await client.post(
            f"{API}/requirements/{parsed['id']}/accept-parse", json={"confirmed_fields": []}
        )
        assert response.status_code == 422

        fields = {detail["field"] for detail in response.json()["error"]["details"]}
        assert "rate_min" in fields
        assert "response_deadline_at" in fields

    async def test_accepting_with_confirmations_makes_it_business_data(self, sales, parsed):
        client, _ = sales
        result = (await client.get(f"{API}/requirements/{parsed['id']}/parse-result")).json()

        response = await client.post(
            f"{API}/requirements/{parsed['id']}/accept-parse",
            json={"confirmed_fields": result["confirmation_required"]},
        )
        assert response.status_code == 200

        body = response.json()
        assert body["review_status"] == "ACCEPTED"
        assert body["needs_review"] is False
        assert body["status"] == "UNDER_REVIEW"

    async def test_the_reviewer_can_correct_a_value_while_accepting(self, sales, parsed):
        client, _ = sales
        result = (await client.get(f"{API}/requirements/{parsed['id']}/parse-result")).json()

        response = await client.post(
            f"{API}/requirements/{parsed['id']}/accept-parse",
            json={
                "confirmed_fields": result["confirmation_required"],
                "updates": {"title": "Lead SAP FICO Consultant", "positions": 3},
                "skills": [{"name": "SAP FICO", "importance": "MANDATORY", "min_years": 8}],
            },
        )
        assert response.status_code == 200

        body = response.json()
        assert body["title"] == "Lead SAP FICO Consultant"
        assert body["positions"] == 3
        assert [skill["name"] for skill in body["skills"]] == ["SAP FICO"]

    async def test_accepting_twice_is_refused(self, sales, parsed):
        client, _ = sales
        result = (await client.get(f"{API}/requirements/{parsed['id']}/parse-result")).json()
        body = {"confirmed_fields": result["confirmation_required"]}

        assert (
            await client.post(f"{API}/requirements/{parsed['id']}/accept-parse", json=body)
        ).status_code == 200
        assert (
            await client.post(f"{API}/requirements/{parsed['id']}/accept-parse", json=body)
        ).status_code == 409

    async def test_a_rejected_parse_is_closed_and_deactivated(self, sales, parsed):
        client, _ = sales

        response = await client.post(
            f"{API}/requirements/{parsed['id']}/reject-parse", params={"reason": "Not our market"}
        )
        assert response.status_code == 200

        body = response.json()
        assert body["review_status"] == "REJECTED"
        assert body["is_active"] is False
        assert body["status"] == "CLOSED_LOST"

    async def test_an_accepted_requirement_can_then_be_qualified(self, sales, parsed):
        client, _ = sales
        result = (await client.get(f"{API}/requirements/{parsed['id']}/parse-result")).json()
        await client.post(
            f"{API}/requirements/{parsed['id']}/accept-parse",
            json={"confirmed_fields": result["confirmation_required"]},
        )

        response = await client.post(
            f"{API}/requirements/{parsed['id']}/status", json={"status": "QUALIFIED"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "QUALIFIED"


class TestStatusWorkflow:
    async def test_an_illegal_transition_is_refused_with_the_allowed_set(self, sales):
        client, _ = sales
        requirement = (await client.post(f"{API}/requirements", json=requirement_payload())).json()

        response = await client.post(
            f"{API}/requirements/{requirement['id']}/status", json={"status": "CLOSED_WON"}
        )
        assert response.status_code == 409
        assert "Allowed:" in response.json()["error"]["details"][0]["message"]

    async def test_a_terminal_status_deactivates_the_requirement(self, sales):
        client, _ = sales
        requirement = (await client.post(f"{API}/requirements", json=requirement_payload())).json()

        response = await client.post(
            f"{API}/requirements/{requirement['id']}/status",
            json={"status": "CLOSED_LOST", "reason": "Client cancelled"},
        )
        assert response.json()["is_active"] is False

    async def test_status_changes_are_recorded_as_history(self, sales):
        client, _ = sales
        requirement = (await client.post(f"{API}/requirements", json=requirement_payload())).json()
        await client.post(
            f"{API}/requirements/{requirement['id']}/status",
            json={"status": "QUALIFIED", "reason": "Client confirmed budget"},
        )

        history = (await client.get(f"{API}/requirements/{requirement['id']}/history")).json()
        latest = history[0]
        assert latest["to_status"] == "QUALIFIED"
        assert latest["from_status"] == "NEW"
        assert latest["reason"] == "Client confirmed budget"
        assert latest["user_name"]

    async def test_setting_the_same_status_is_a_no_op(self, sales):
        client, _ = sales
        requirement = (await client.post(f"{API}/requirements", json=requirement_payload())).json()

        response = await client.post(
            f"{API}/requirements/{requirement['id']}/status", json={"status": "NEW"}
        )
        assert response.status_code == 200


class TestDeadlineBoard:
    async def test_requirements_are_grouped_by_how_much_window_is_left(self, sales):
        client, _ = sales
        now = datetime.now(UTC)

        horizons = {
            "urgent": now + timedelta(hours=4),
            "due_soon": now + timedelta(hours=18),
            "safe": now + timedelta(days=5),
            "expired": now - timedelta(hours=3),
        }
        created = {}
        for label, deadline in horizons.items():
            response = await client.post(
                f"{API}/requirements",
                json=requirement_payload(
                    title=f"Deadline {label} {uuid.uuid4().hex[:6]}",
                    response_deadline_at=deadline.isoformat(),
                ),
            )
            created[label] = response.json()["id"]

        board = (await client.get(f"{API}/requirements/deadlines")).json()

        for label, requirement_id in created.items():
            ids = [item["id"] for item in board[label]]
            assert requirement_id in ids, f"{label} bucket is missing its requirement"

    async def test_each_requirement_carries_a_readable_deadline_label(self, sales):
        client, _ = sales
        deadline = datetime.now(UTC) + timedelta(hours=4)

        requirement = (
            await client.post(
                f"{API}/requirements",
                json=requirement_payload(response_deadline_at=deadline.isoformat()),
            )
        ).json()

        assert requirement["deadline"]["state"] == "URGENT"
        assert requirement["deadline"]["is_overdue"] is False
        assert requirement["deadline"]["label"].endswith("left")

    async def test_an_expired_deadline_reads_as_overdue_not_negative_time(self, sales):
        client, _ = sales
        deadline = datetime.now(UTC) - timedelta(hours=5)

        requirement = (
            await client.post(
                f"{API}/requirements",
                json=requirement_payload(response_deadline_at=deadline.isoformat()),
            )
        ).json()

        assert requirement["deadline"]["state"] == "EXPIRED"
        assert requirement["deadline"]["is_overdue"] is True
        assert "ago" in requirement["deadline"]["label"]

    async def test_closed_requirements_leave_the_board(self, sales):
        client, _ = sales
        requirement = (
            await client.post(
                f"{API}/requirements",
                json=requirement_payload(
                    response_deadline_at=(datetime.now(UTC) + timedelta(hours=4)).isoformat()
                ),
            )
        ).json()

        await client.post(
            f"{API}/requirements/{requirement['id']}/status", json={"status": "CLOSED_LOST"}
        )

        board = (await client.get(f"{API}/requirements/deadlines")).json()
        every_id = [
            item["id"]
            for bucket in ("urgent", "due_soon", "safe", "expired")
            for item in board[bucket]
        ]
        assert requirement["id"] not in every_id


class TestFilteringAndSearch:
    async def test_filter_by_status_and_review_state(self, sales, parsed):
        client, _ = sales

        pending = (
            await client.get(f"{API}/requirements", params={"review_status": "PENDING_REVIEW"})
        ).json()
        assert all(item["needs_review"] for item in pending["items"])
        assert parsed["id"] in [item["id"] for item in pending["items"]]

    async def test_search_matches_title_and_description(self, sales, parsed):
        client, _ = sales
        found = (await client.get(f"{API}/requirements", params={"q": "sap fico"})).json()
        assert parsed["id"] in [item["id"] for item in found["items"]]

    async def test_filter_by_skill(self, sales):
        client, _ = sales
        skills = (await client.get(f"{API}/skills", params={"q": "kubernetes"})).json()
        kubernetes = next(skill for skill in skills if skill["name"] == "Kubernetes")

        requirement = (
            await client.post(
                f"{API}/requirements",
                json=requirement_payload(skills=[{"name": "Kubernetes"}]),
            )
        ).json()

        filtered = (
            await client.get(f"{API}/requirements", params={"skill_id": kubernetes["id"]})
        ).json()
        assert requirement["id"] in [item["id"] for item in filtered["items"]]

    async def test_open_only_excludes_closed_requirements(self, sales):
        client, _ = sales
        requirement = (await client.post(f"{API}/requirements", json=requirement_payload())).json()
        await client.post(
            f"{API}/requirements/{requirement['id']}/status", json={"status": "CLOSED_LOST"}
        )

        open_only = (
            await client.get(f"{API}/requirements", params={"open_only": True, "page_size": 100})
        ).json()
        assert requirement["id"] not in [item["id"] for item in open_only["items"]]

    async def test_unknown_sort_field_is_rejected(self, sales):
        client, _ = sales
        response = await client.get(f"{API}/requirements", params={"sort": "-nonsense"})
        assert response.status_code == 422


class TestRequirementAuthorization:
    @pytest.mark.parametrize(
        ("role", "expected"),
        [
            (Role.ADMIN, 201),
            (Role.SALES, 201),
            (Role.MANAGEMENT, 403),
            (Role.HR_RESOURCING, 403),
        ],
    )
    async def test_create_requirement(self, as_role, role, expected):
        client, _ = await as_role(role)
        response = await client.post(f"{API}/requirements", json=requirement_payload())
        assert response.status_code == expected

    @pytest.mark.parametrize("role", list(Role))
    async def test_every_role_can_read_requirements(self, as_role, role):
        client, _ = await as_role(role)
        assert (await client.get(f"{API}/requirements")).status_code == 200

    @pytest.mark.parametrize(
        ("role", "expected"),
        [(Role.SALES, 201), (Role.HR_RESOURCING, 201), (Role.MANAGEMENT, 403)],
    )
    async def test_jd_parsing_permission(self, as_role, role, expected):
        """Resourcing parses JDs too; Management only observes."""
        client, _ = await as_role(role)
        response = await client.post(f"{API}/requirements/parse-text", json={"text": JD_TEXT})
        assert response.status_code == expected

    async def test_anonymous_callers_are_rejected(self, client):
        for path in ("/requirements", "/requirements/deadlines", "/skills"):
            assert (await client.get(f"{API}{path}")).status_code == 401
