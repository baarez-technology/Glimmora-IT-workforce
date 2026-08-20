"""Phase 10: the sales, submission and interview pipeline.

The definition of done, restated as tests:

* the full stage flow works,
* submitting an already-submitted candidate returns the duplicate warning **with
  who submitted, when and the current status**,
* scheduling an interview creates a reminder.

The duplicate guard carries the most weight. Sending a client the same CV from
two recruiters is the classic staffing embarrassment, and it is prevented at the
database level rather than merely checked in a service.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.permissions import Role
from app.engines.pipeline.stages import (
    STAGE_ORDER,
    TERMINAL_STAGES,
    OpportunityStage,
    can_transition,
    is_forward,
    is_terminal,
    next_suggested,
    stage_index,
)
from app.models.pipeline import BLOCKING_SUBMISSION_STATUSES, SubmissionStatus

API = "/api/v1"


# ----------------------------------------------------------- the stage machine


class TestStageMachine:
    def test_the_ladder_is_twelve_stages_plus_two_outcomes(self):
        assert len(STAGE_ORDER) == 12
        assert len(TERMINAL_STAGES) == 2
        assert len(list(OpportunityStage)) == 14

    def test_forward_moves_may_skip_intermediate_stages(self):
        # Real deals do: a client who already knows the consultant goes
        # straight from contact to interview.
        allowed, _ = can_transition(OpportunityStage.CONTACTED, OpportunityStage.INTERVIEW)
        assert allowed

    def test_backward_moves_are_allowed_because_they_really_happen(self):
        allowed, _ = can_transition(OpportunityStage.INTERVIEW, OpportunityStage.CV_SUBMITTED)
        assert allowed
        assert not is_forward(OpportunityStage.INTERVIEW, OpportunityStage.CV_SUBMITTED)

    def test_moving_to_the_current_stage_is_rejected(self):
        allowed, reason = can_transition(OpportunityStage.MATCHED, OpportunityStage.MATCHED)
        assert not allowed
        assert "already" in (reason or "").lower()

    def test_a_closed_opportunity_cannot_be_closed_again(self):
        allowed, reason = can_transition(OpportunityStage.LOST, OpportunityStage.DROPPED)
        assert not allowed
        assert "closed" in (reason or "").lower()

    def test_a_closed_opportunity_can_be_reopened(self):
        allowed, _ = can_transition(OpportunityStage.LOST, OpportunityStage.QUALIFIED)
        assert allowed

    def test_terminal_stages_sit_past_the_end_of_the_ladder(self):
        assert stage_index(OpportunityStage.LOST) == len(STAGE_ORDER)
        assert is_terminal(OpportunityStage.DROPPED)
        assert not is_terminal(OpportunityStage.BILLING)

    def test_the_suggested_next_stage_follows_the_ladder(self):
        assert next_suggested(OpportunityStage.MATCHED) is OpportunityStage.QUALIFIED
        assert next_suggested(OpportunityStage.EXTENSION_REDEPLOYMENT) is None
        assert next_suggested(OpportunityStage.LOST) is None

    def test_only_live_statuses_block_a_resubmission(self):
        assert SubmissionStatus.SUBMITTED in BLOCKING_SUBMISSION_STATUSES
        assert SubmissionStatus.INTERVIEW in BLOCKING_SUBMISSION_STATUSES
        # Circumstances change; a rejected or withdrawn candidate may come back.
        assert SubmissionStatus.REJECTED not in BLOCKING_SUBMISSION_STATUSES
        assert SubmissionStatus.WITHDRAWN not in BLOCKING_SUBMISSION_STATUSES


# ------------------------------------------------------------------ fixtures


async def make_account(client):
    response = await client.post(
        f"{API}/accounts",
        json={
            "name": f"Pipeline Client {uuid.uuid4().hex[:6]}",
            "account_type": "CUSTOMER",
            "country": "QA",
            "relationship_status": "ACTIVE",
            "is_existing_customer": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def make_requirement(client, account_id: str):
    response = await client.post(
        f"{API}/requirements",
        json={
            "title": f"Pipeline Requirement {uuid.uuid4().hex[:6]}",
            "role": "SAP FICO Consultant",
            "positions": 1,
            "priority_source": "P1_EXISTING_CUSTOMER",
            "account_id": account_id,
            "country": "QA",
            "rate_max": "22000",
            "rate_currency": "QAR",
            "rate_unit": "MONTHLY",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def make_resource(client, **overrides):
    payload = {
        "full_name": f"Pipeline Candidate {uuid.uuid4().hex[:6]}",
        "resource_type": "CONSULTANT",
        "availability_status": "AVAILABLE",
        "total_experience_years": 8,
        "current_location_country": "QA",
    }
    payload.update(overrides)
    response = await client.post(f"{API}/resources", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
async def pipeline(as_role):
    """A requirement with two reviewed consultants ready to put forward."""
    hr, _ = await as_role(Role.HR_RESOURCING)
    first = await make_resource(hr)
    second = await make_resource(hr)

    sales, sales_user = await as_role(Role.SALES)
    account = await make_account(sales)
    requirement = await make_requirement(sales, account["id"])

    return sales, sales_user, requirement, first, second


# --------------------------------------------------------------- submissions


class TestSubmissions:
    async def test_submitting_creates_the_opportunity_automatically(self, pipeline):
        client, _, requirement, resource, _ = pipeline

        response = await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
        )
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["opportunity_id"] is not None
        assert body["resource_name"] == resource["full_name"]
        assert body["status"] == "SUBMITTED"
        assert body["submitted_at"] is not None

    async def test_the_submission_pulls_the_opportunity_to_cv_submitted(self, pipeline):
        client, _, requirement, resource, _ = pipeline
        submission = (
            await client.post(
                f"{API}/submissions",
                json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
            )
        ).json()

        opportunity = (
            await client.get(f"{API}/opportunities/{submission['opportunity_id']}")
        ).json()
        assert opportunity["stage"] == "CV_SUBMITTED"

    async def test_a_draft_submission_does_not_claim_to_have_been_sent(self, pipeline):
        client, _, requirement, resource, _ = pipeline
        body = (
            await client.post(
                f"{API}/submissions",
                json={
                    "requirement_id": requirement["id"],
                    "resource_id": resource["id"],
                    "status": "DRAFT",
                },
            )
        ).json()

        assert body["status"] == "DRAFT"
        assert body["submitted_at"] is None

    async def test_an_unreviewed_profile_cannot_be_submitted(self, pipeline, as_role):
        _, _, requirement, _, _ = pipeline

        hr, _ = await as_role(Role.HR_RESOURCING)
        parse = await hr.post(
            f"{API}/resources/parse-cv",
            files={
                "file": (
                    "cv.txt",
                    b"Nadia Haddad\nSAP FICO Consultant\nnadia@example.com | Doha, Qatar\n\n"
                    b"PROFESSIONAL SUMMARY\nSAP FICO consultant with S/4HANA experience across\n"
                    b"Gulf logistics clients over several delivery programmes.\n\n"
                    b"EXPERIENCE\n\nSAP FICO Consultant, Northline\nJan 2019 - Present\n\n"
                    b"SKILLS\nSAP FICO, SAP MM\n\nNotice Period: 30 days\n",
                    "text/plain",
                )
            },
        )
        assert parse.status_code == 201, parse.text
        pending_id = parse.json()["resource_id"]

        sales, _ = await as_role(Role.SALES)
        response = await sales.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": pending_id},
        )
        assert response.status_code == 422
        assert "review" in response.text.lower()

    async def test_an_unknown_resource_is_a_404(self, pipeline):
        client, _, requirement, _, _ = pipeline
        response = await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": str(uuid.uuid4())},
        )
        assert response.status_code == 404


class TestDuplicateProtection:
    """The Phase 10 definition of done."""

    async def test_a_second_submission_is_refused_with_the_detail_that_matters(self, pipeline):
        client, sales_user, requirement, resource, _ = pipeline
        first = await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
        )
        assert first.status_code == 201

        second = await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
        )
        assert second.status_code == 409, second.text
        error = second.json()["error"]

        assert error["code"] == "DUPLICATE_SUBMISSION"
        fields = {detail["field"]: detail["message"] for detail in error["details"]}
        assert fields["current_status"] == "SUBMITTED"
        assert fields["submitted_by"] == sales_user.full_name
        assert fields["submitted_at"], "the recruiter needs to know when"

    async def test_the_duplicate_can_be_checked_before_committing(self, pipeline):
        client, _, requirement, resource, _ = pipeline

        clean = (
            await client.get(
                f"{API}/submissions/check-duplicate",
                params={"requirement_id": requirement["id"], "resource_id": resource["id"]},
            )
        ).json()
        assert clean["is_duplicate"] is False

        await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
        )

        flagged = (
            await client.get(
                f"{API}/submissions/check-duplicate",
                params={"requirement_id": requirement["id"], "resource_id": resource["id"]},
            )
        ).json()
        assert flagged["is_duplicate"] is True
        assert flagged["status"] == "SUBMITTED"
        assert flagged["submitted_by"]

    async def test_a_withdrawn_candidate_may_be_resubmitted(self, pipeline):
        """Circumstances change. Only a live submission blocks the seat."""
        client, _, requirement, resource, _ = pipeline
        first = (
            await client.post(
                f"{API}/submissions",
                json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
            )
        ).json()

        withdrawn = await client.post(
            f"{API}/submissions/{first['id']}/status",
            json={"status": "WITHDRAWN", "note": "Client paused the role"},
        )
        assert withdrawn.status_code == 200

        again = await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
        )
        assert again.status_code == 201, again.text

    async def test_a_rejected_candidate_may_be_resubmitted(self, pipeline):
        client, _, requirement, resource, _ = pipeline
        first = (
            await client.post(
                f"{API}/submissions",
                json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
            )
        ).json()
        await client.post(
            f"{API}/submissions/{first['id']}/status",
            json={"status": "REJECTED", "rejection_reason": "Rate too high for this budget"},
        )

        again = await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
        )
        assert again.status_code == 201

    async def test_a_different_consultant_on_the_same_requirement_is_fine(self, pipeline):
        client, _, requirement, first, second = pipeline
        await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": first["id"]},
        )
        response = await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": second["id"]},
        )
        assert response.status_code == 201

    async def test_the_database_enforces_it_not_just_the_service(self, pipeline, session):
        """A guarantee this important must not depend on a code path."""
        from sqlalchemy.exc import IntegrityError

        from app.models.pipeline import Submission

        client, _, requirement, resource, _ = pipeline
        await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
        )

        session.add(
            Submission(
                requirement_id=uuid.UUID(requirement["id"]),
                resource_id=uuid.UUID(resource["id"]),
                status=SubmissionStatus.SUBMITTED,
                blocks_resubmission=True,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


class TestSubmissionLifecycle:
    async def test_status_changes_are_recorded_in_history(self, pipeline):
        client, _, requirement, resource, _ = pipeline
        submission = (
            await client.post(
                f"{API}/submissions",
                json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
            )
        ).json()

        await client.post(
            f"{API}/submissions/{submission['id']}/status",
            json={"status": "SHORTLISTED", "note": "Client liked the profile"},
        )

        history = (await client.get(f"{API}/submissions/{submission['id']}/history")).json()
        assert len(history) == 2
        assert history[0]["to_status"] == "SUBMITTED"
        assert history[1]["from_status"] == "SUBMITTED"
        assert history[1]["to_status"] == "SHORTLISTED"

    async def test_a_rejection_requires_a_reason(self, pipeline):
        client, _, requirement, resource, _ = pipeline
        submission = (
            await client.post(
                f"{API}/submissions",
                json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
            )
        ).json()

        response = await client.post(
            f"{API}/submissions/{submission['id']}/status", json={"status": "REJECTED"}
        )
        assert response.status_code == 422
        assert "reason" in response.text.lower()

    async def test_the_same_status_twice_is_rejected(self, pipeline):
        client, _, requirement, resource, _ = pipeline
        submission = (
            await client.post(
                f"{API}/submissions",
                json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
            )
        ).json()

        response = await client.post(
            f"{API}/submissions/{submission['id']}/status", json={"status": "SUBMITTED"}
        )
        assert response.status_code == 422

    async def test_client_feedback_is_kept(self, pipeline):
        client, _, requirement, resource, _ = pipeline
        submission = (
            await client.post(
                f"{API}/submissions",
                json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
            )
        ).json()

        body = (
            await client.post(
                f"{API}/submissions/{submission['id']}/status",
                json={"status": "SHORTLISTED", "client_feedback": "Strong S/4HANA background"},
            )
        ).json()
        assert body["client_feedback"] == "Strong S/4HANA background"


# ------------------------------------------------------------- opportunities


class TestOpportunityStages:
    async def test_the_stage_list_is_exposed_for_the_board(self, pipeline):
        client, _, _, _, _ = pipeline
        stages = (await client.get(f"{API}/opportunities/stages")).json()

        assert len(stages) == 14
        assert stages[0]["value"] == "REQUIREMENT_IDENTIFIED"
        assert [stage for stage in stages if stage["is_terminal"]]

    async def test_an_opportunity_can_be_opened_explicitly(self, pipeline):
        client, _, requirement, _, _ = pipeline
        response = await client.post(
            f"{API}/opportunities", json={"requirement_id": requirement["id"]}
        )

        assert response.status_code == 201
        body = response.json()
        assert body["stage"] == "REQUIREMENT_IDENTIFIED"
        assert body["next_stage"] == "MATCHED"
        assert body["is_open"] is True

    async def test_opening_twice_returns_the_same_opportunity(self, pipeline):
        client, _, requirement, _, _ = pipeline
        first = (
            await client.post(f"{API}/opportunities", json={"requirement_id": requirement["id"]})
        ).json()
        second = (
            await client.post(f"{API}/opportunities", json={"requirement_id": requirement["id"]})
        ).json()

        assert first["id"] == second["id"], "one requirement, one opportunity"

    async def test_the_full_forward_flow_works(self, pipeline):
        client, _, requirement, resource, _ = pipeline
        submission = (
            await client.post(
                f"{API}/submissions",
                json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
            )
        ).json()
        opportunity_id = submission["opportunity_id"]

        for stage in [
            "INTERVIEW",
            "COMMERCIAL_NEGOTIATION",
            "SELECTED",
            "PO_CONTRACT",
            "DEPLOYED",
            "BILLING",
        ]:
            response = await client.post(
                f"{API}/opportunities/{opportunity_id}/stage", json={"stage": stage}
            )
            assert response.status_code == 200, f"{stage}: {response.text}"
            assert response.json()["stage"] == stage

    async def test_a_stage_needing_a_submission_is_blocked_without_one(self, pipeline):
        client, _, requirement, _, _ = pipeline
        opportunity = (
            await client.post(f"{API}/opportunities", json={"requirement_id": requirement["id"]})
        ).json()

        response = await client.post(
            f"{API}/opportunities/{opportunity['id']}/stage", json={"stage": "CV_SUBMITTED"}
        )
        assert response.status_code == 422
        assert "submission" in response.text.lower()

    async def test_closing_requires_a_reason(self, pipeline):
        client, _, requirement, _, _ = pipeline
        opportunity = (
            await client.post(f"{API}/opportunities", json={"requirement_id": requirement["id"]})
        ).json()

        without = await client.post(
            f"{API}/opportunities/{opportunity['id']}/stage", json={"stage": "LOST"}
        )
        assert without.status_code == 422

        withreason = await client.post(
            f"{API}/opportunities/{opportunity['id']}/stage",
            json={"stage": "LOST", "note": "Client awarded it to an incumbent"},
        )
        assert withreason.status_code == 200
        assert withreason.json()["closed_reason"] == "Client awarded it to an incumbent"
        assert withreason.json()["is_open"] is False

    async def test_reopening_clears_the_closure(self, pipeline):
        client, _, requirement, _, _ = pipeline
        opportunity = (
            await client.post(f"{API}/opportunities", json={"requirement_id": requirement["id"]})
        ).json()
        await client.post(
            f"{API}/opportunities/{opportunity['id']}/stage",
            json={"stage": "DROPPED", "note": "Paused"},
        )

        reopened = (
            await client.post(
                f"{API}/opportunities/{opportunity['id']}/stage",
                json={"stage": "QUALIFIED", "note": "Client restarted it"},
            )
        ).json()
        assert reopened["is_open"] is True
        assert reopened["closed_reason"] is None
        assert reopened["closed_at"] is None

    async def test_every_stage_move_is_recorded(self, pipeline):
        client, _, requirement, _, _ = pipeline
        opportunity = (
            await client.post(f"{API}/opportunities", json={"requirement_id": requirement["id"]})
        ).json()
        await client.post(
            f"{API}/opportunities/{opportunity['id']}/stage", json={"stage": "QUALIFIED"}
        )

        history = (await client.get(f"{API}/opportunities/{opportunity['id']}/history")).json()
        assert len(history) == 2
        assert history[0]["to_stage"] == "REQUIREMENT_IDENTIFIED"
        assert history[1]["to_stage"] == "QUALIFIED"

    async def test_a_submission_never_drags_a_closed_opportunity_back(self, pipeline):
        client, _, requirement, first, second = pipeline
        submission = (
            await client.post(
                f"{API}/submissions",
                json={"requirement_id": requirement["id"], "resource_id": first["id"]},
            )
        ).json()
        await client.post(
            f"{API}/opportunities/{submission['opportunity_id']}/stage",
            json={"stage": "LOST", "note": "Client cancelled"},
        )

        await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": second["id"]},
        )
        opportunity = (
            await client.get(f"{API}/opportunities/{submission['opportunity_id']}")
        ).json()
        assert opportunity["stage"] == "LOST"

    async def test_a_submission_never_moves_the_opportunity_backwards(self, pipeline):
        client, _, requirement, first, second = pipeline
        submission = (
            await client.post(
                f"{API}/submissions",
                json={"requirement_id": requirement["id"], "resource_id": first["id"]},
            )
        ).json()
        await client.post(
            f"{API}/opportunities/{submission['opportunity_id']}/stage",
            json={"stage": "COMMERCIAL_NEGOTIATION"},
        )

        # A second candidate entering at CV_SUBMITTED must not undo the
        # progress the first one made.
        await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": second["id"]},
        )
        opportunity = (
            await client.get(f"{API}/opportunities/{submission['opportunity_id']}")
        ).json()
        assert opportunity["stage"] == "COMMERCIAL_NEGOTIATION"


class TestOpportunityDecision:
    async def test_a_decision_is_separate_from_the_stage(self, pipeline):
        client, _, requirement, _, _ = pipeline
        opportunity = (
            await client.post(f"{API}/opportunities", json={"requirement_id": requirement["id"]})
        ).json()

        body = (
            await client.post(
                f"{API}/opportunities/{opportunity['id']}/decision",
                json={"decision": "PURSUE", "reason": "Strong margin and a direct route"},
            )
        ).json()

        assert body["decision"] == "PURSUE"
        assert body["decided_at"] is not None
        assert body["stage"] == "REQUIREMENT_IDENTIFIED", "a decision is not a stage move"

    async def test_declining_requires_a_reason(self, pipeline):
        client, _, requirement, _, _ = pipeline
        opportunity = (
            await client.post(f"{API}/opportunities", json={"requirement_id": requirement["id"]})
        ).json()

        response = await client.post(
            f"{API}/opportunities/{opportunity['id']}/decision", json={"decision": "DECLINE"}
        )
        assert response.status_code == 422

    async def test_the_board_lists_opportunities(self, pipeline):
        client, _, requirement, _, _ = pipeline
        await client.post(f"{API}/opportunities", json={"requirement_id": requirement["id"]})

        board = (await client.get(f"{API}/opportunities")).json()
        assert any(item["requirement_id"] == requirement["id"] for item in board)

    async def test_a_next_action_and_due_date_can_be_set(self, pipeline):
        client, _, requirement, _, _ = pipeline
        opportunity = (
            await client.post(f"{API}/opportunities", json={"requirement_id": requirement["id"]})
        ).json()

        due = (datetime.now(UTC) + timedelta(days=2)).isoformat()
        body = (
            await client.patch(
                f"{API}/opportunities/{opportunity['id']}",
                json={"next_action": "Call procurement", "next_action_due_at": due},
            )
        ).json()
        assert body["next_action"] == "Call procurement"
        assert body["next_action_due_at"] is not None


# ---------------------------------------------------------------- interviews


@pytest.fixture
async def submitted(pipeline):
    client, sales_user, requirement, resource, _ = pipeline
    submission = (
        await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
        )
    ).json()
    return client, sales_user, submission, resource


class TestInterviews:
    async def test_scheduling_moves_the_submission_and_the_opportunity(self, submitted):
        client, _, submission, _ = submitted
        when = (datetime.now(UTC) + timedelta(days=3)).isoformat()

        response = await client.post(
            f"{API}/interviews",
            json={
                "submission_id": submission["id"],
                "scheduled_at": when,
                "mode": "VIDEO",
                "interviewer_name": "Procurement Lead",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["round_number"] == 1

        updated = (await client.get(f"{API}/submissions/{submission['id']}")).json()
        assert updated["status"] == "INTERVIEW"

        opportunity = (
            await client.get(f"{API}/opportunities/{submission['opportunity_id']}")
        ).json()
        assert opportunity["stage"] == "INTERVIEW"

    async def test_scheduling_creates_a_reminder(self, submitted, as_role):
        """The third Phase 10 definition-of-done clause."""
        client, _, submission, resource = submitted
        when = (datetime.now(UTC) + timedelta(days=3)).isoformat()

        body = (
            await client.post(
                f"{API}/interviews",
                json={"submission_id": submission["id"], "scheduled_at": when},
            )
        ).json()
        assert body["reminder_sent_at"] is not None

        from sqlalchemy import select

        from app.db.session import SessionFactory
        from app.models.notifications import Notification, NotificationCategory

        async with SessionFactory() as db:
            rows = (
                (
                    await db.execute(
                        select(Notification).where(
                            Notification.category == NotificationCategory.INTERVIEW_REMINDER,
                            Notification.entity_id == uuid.UUID(body["id"]),
                        )
                    )
                )
                .scalars()
                .all()
            )

        assert len(rows) == 1
        assert resource["full_name"] in rows[0].title
        assert rows[0].dedupe_key == f"interview:{body['id']}"

    async def test_an_interview_in_the_past_is_rejected(self, submitted):
        client, _, submission, _ = submitted
        when = (datetime.now(UTC) - timedelta(days=1)).isoformat()

        response = await client.post(
            f"{API}/interviews",
            json={"submission_id": submission["id"], "scheduled_at": when},
        )
        assert response.status_code == 422
        assert "past" in response.text.lower()

    async def test_rounds_increment(self, submitted):
        client, _, submission, _ = submitted
        for expected in (1, 2, 3):
            when = (datetime.now(UTC) + timedelta(days=3 + expected)).isoformat()
            body = (
                await client.post(
                    f"{API}/interviews",
                    json={"submission_id": submission["id"], "scheduled_at": when},
                )
            ).json()
            assert body["round_number"] == expected

    async def test_passing_an_interview_selects_the_candidate(self, submitted):
        client, _, submission, _ = submitted
        when = (datetime.now(UTC) + timedelta(days=3)).isoformat()
        interview = (
            await client.post(
                f"{API}/interviews",
                json={"submission_id": submission["id"], "scheduled_at": when},
            )
        ).json()

        await client.post(
            f"{API}/interviews/{interview['id']}/outcome",
            json={"outcome": "PASSED", "feedback": "Strong technical round"},
        )

        updated = (await client.get(f"{API}/submissions/{submission['id']}")).json()
        assert updated["status"] == "SELECTED"

    async def test_a_failed_interview_does_not_silently_reject_the_candidate(self, submitted):
        """A human decides whether to reject or hold."""
        client, _, submission, _ = submitted
        when = (datetime.now(UTC) + timedelta(days=3)).isoformat()
        interview = (
            await client.post(
                f"{API}/interviews",
                json={"submission_id": submission["id"], "scheduled_at": when},
            )
        ).json()

        await client.post(f"{API}/interviews/{interview['id']}/outcome", json={"outcome": "FAILED"})

        updated = (await client.get(f"{API}/submissions/{submission['id']}")).json()
        assert updated["status"] == "INTERVIEW"

    async def test_upcoming_interviews_are_listed_soonest_first(self, submitted):
        client, _, submission, _ = submitted
        for days in (10, 3, 7):
            await client.post(
                f"{API}/interviews",
                json={
                    "submission_id": submission["id"],
                    "scheduled_at": (datetime.now(UTC) + timedelta(days=days)).isoformat(),
                },
            )

        rows = (await client.get(f"{API}/interviews?days_ahead=30")).json()
        mine = [row for row in rows if row["submission_id"] == submission["id"]]
        dates = [row["scheduled_at"] for row in mine]
        assert dates == sorted(dates)

    async def test_an_unknown_interviewer_contact_is_a_404(self, submitted):
        client, _, submission, _ = submitted
        response = await client.post(
            f"{API}/interviews",
            json={
                "submission_id": submission["id"],
                "scheduled_at": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
                "interviewer_contact_id": str(uuid.uuid4()),
            },
        )
        assert response.status_code == 404


# ------------------------------------------------------------ communications


class TestCommunications:
    async def test_a_note_can_be_logged_against_an_opportunity(self, submitted):
        client, _, submission, _ = submitted

        response = await client.post(
            f"{API}/communications",
            json={
                "channel": "NOTE",
                "subject": "Client call",
                "body": "Procurement confirmed the budget.",
                "opportunity_id": submission["opportunity_id"],
            },
        )
        assert response.status_code == 201
        assert response.json()["status"] == "LOGGED"

    async def test_the_timeline_returns_newest_first(self, submitted):
        client, _, submission, _ = submitted
        for subject in ("First", "Second"):
            await client.post(
                f"{API}/communications",
                json={
                    "channel": "NOTE",
                    "subject": subject,
                    "opportunity_id": submission["opportunity_id"],
                },
            )

        timeline = (
            await client.get(
                f"{API}/communications", params={"opportunity_id": submission["opportunity_id"]}
            )
        ).json()
        assert [item["subject"] for item in timeline][:2] == ["Second", "First"]

    async def test_an_email_with_no_recipient_is_rejected(self, submitted):
        client, _, submission, _ = submitted
        response = await client.post(
            f"{API}/communications",
            json={
                "channel": "EMAIL",
                "subject": "Candidate profile",
                "send": True,
                "opportunity_id": submission["opportunity_id"],
            },
        )
        assert response.status_code == 422

    async def test_the_log_is_honest_about_the_email_fallback(self, submitted):
        """With EMAIL_TRANSPORT=log nothing is transmitted, and it says so."""
        client, _, submission, _ = submitted
        body = (
            await client.post(
                f"{API}/communications",
                json={
                    "channel": "EMAIL",
                    "subject": "Candidate profile",
                    "body": "Please find the CV attached.",
                    "to_addresses": ["client@example.com"],
                    "send": True,
                    "opportunity_id": submission["opportunity_id"],
                },
            )
        ).json()

        assert body["status"] == "LOGGED", "never claim a send that did not happen"
        assert body["sent_at"] is None


# ------------------------------------------------------------ authorization


class TestPipelineAuthorization:
    async def test_management_may_read_but_not_write(self, pipeline, as_role):
        _, _, requirement, _, _ = pipeline
        client, _ = await as_role(Role.MANAGEMENT)

        assert (await client.get(f"{API}/opportunities")).status_code == 200
        assert (
            await client.post(f"{API}/opportunities", json={"requirement_id": requirement["id"]})
        ).status_code == 403

    async def test_resourcing_may_submit_candidates(self, pipeline, as_role):
        _, _, requirement, _, second = pipeline
        client, _ = await as_role(Role.HR_RESOURCING)

        response = await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": second["id"]},
        )
        assert response.status_code == 201

    async def test_a_role_without_rate_sight_is_told_what_was_withheld(self, pipeline, as_role):
        client, _, requirement, resource, _ = pipeline
        await client.post(
            f"{API}/submissions",
            json={
                "requirement_id": requirement["id"],
                "resource_id": resource["id"],
                "proposed_bill_rate": "21000",
                "proposed_bill_currency": "QAR",
                "proposed_bill_unit": "MONTHLY",
            },
        )

        hr, _ = await as_role(Role.HR_RESOURCING)
        rows = (await hr.get(f"{API}/submissions?requirement_id={requirement['id']}")).json()
        mine = next(row for row in rows if row["resource_id"] == resource["id"])

        assert mine["proposed_bill_rate"] is None
        assert "proposed_bill_rate" in mine["restricted_fields"]

    async def test_sales_sees_the_proposed_rate(self, pipeline):
        client, _, requirement, resource, _ = pipeline
        body = (
            await client.post(
                f"{API}/submissions",
                json={
                    "requirement_id": requirement["id"],
                    "resource_id": resource["id"],
                    "proposed_bill_rate": "21000",
                    "proposed_bill_currency": "QAR",
                    "proposed_bill_unit": "MONTHLY",
                },
            )
        ).json()
        assert body["proposed_bill_rate"] is not None
        assert body["restricted_fields"] == []

    async def test_submitting_is_audited(self, pipeline, as_role):
        client, _, requirement, resource, _ = pipeline
        await client.post(
            f"{API}/submissions",
            json={"requirement_id": requirement["id"], "resource_id": resource["id"]},
        )

        admin, _ = await as_role(Role.ADMIN)
        entries = (await admin.get(f"{API}/audit?action=CV_SUBMITTED")).json()
        assert entries["items"]
