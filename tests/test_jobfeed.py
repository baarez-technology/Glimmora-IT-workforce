"""The individual job feed.

The privacy guarantee is the point of this file. An individual's feed is
private by row, not by role, and the tests that matter most are the ones
asserting that nobody — including an administrator — can reach somebody
else's items.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.permissions import Permission, Role, permissions_for
from app.models.jobfeed import WorkplaceType
from app.services.jobfeed import classify_workplace, fingerprint_for
from tests.conftest import TEST_PASSWORD

API = "/api/v1"
STRONG = "Glimmora#Feed2026"


def job(**overrides):
    payload = {
        "title": "Senior Python Developer",
        "company_name": "Ras Laffan Logistics",
        "location": "Doha, Qatar",
        "country": "QA",
        "description": "Backend role, fully remote.",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------- pure logic


class TestWorkplaceClassification:
    """Unknown is an answer. Guessing is not."""

    def test_remote_is_recognised(self):
        assert classify_workplace("This role is fully remote") is WorkplaceType.REMOTE

    def test_hybrid_beats_remote_when_both_appear(self):
        # "hybrid remote" is hybrid. Matching remote first would mislabel it.
        assert classify_workplace("hybrid remote, 3 days in Doha") is WorkplaceType.HYBRID

    def test_onsite_is_recognised(self):
        assert classify_workplace("Strictly on-site in Doha") is WorkplaceType.ONSITE

    def test_silence_is_unknown_not_onsite(self):
        # The failure this prevents: every untagged remote role quietly
        # becoming onsite, and the filter lying to the reader.
        assert classify_workplace("Great opportunity in Qatar") is WorkplaceType.UNKNOWN
        assert classify_workplace(None) is WorkplaceType.UNKNOWN
        assert classify_workplace("") is WorkplaceType.UNKNOWN


class TestFingerprint:
    def test_the_same_job_fingerprints_identically(self):
        a = fingerprint_for("Python Developer", "Milaha", "Doha")
        b = fingerprint_for("  python developer  ", "MILAHA", "doha")
        assert a == b

    def test_a_different_company_is_a_different_job(self):
        assert fingerprint_for("Python Developer", "Milaha", "Doha") != fingerprint_for(
            "Python Developer", "Al Dana", "Doha"
        )


# ------------------------------------------------------------- permissions


class TestIndividualRole:
    def test_an_individual_holds_only_their_own_feed(self):
        granted = permissions_for(Role.INDIVIDUAL)
        assert granted == {Permission.JOB_FEED_READ, Permission.JOB_FEED_WRITE}

    @pytest.mark.parametrize(
        "forbidden",
        [
            Permission.ACCOUNT_READ,
            Permission.REQUIREMENT_READ,
            Permission.RESOURCE_READ,
            Permission.BILLING_READ,
            Permission.AUDIT_VIEW,
            Permission.USER_READ,
            Permission.FIELD_MARGIN,
        ],
    )
    def test_an_individual_sees_no_business_data(self, forbidden):
        assert forbidden not in permissions_for(Role.INDIVIDUAL)


# ----------------------------------------------------------- registration


@pytest.mark.anyio
class TestRegistration:
    async def test_anyone_can_create_an_individual_account(self, client):
        email = f"person-{uuid.uuid4().hex[:8]}@example.com"
        response = await client.post(
            f"{API}/auth/register",
            json={"email": email, "full_name": "Test Person", "password": STRONG},
        )
        assert response.status_code == 201, response.text
        assert response.json()["role"] == "INDIVIDUAL"

    async def test_the_role_cannot_be_chosen_by_the_caller(self, client):
        """The obvious attack: ask to be an administrator on the way in."""
        email = f"sneaky-{uuid.uuid4().hex[:8]}@example.com"
        response = await client.post(
            f"{API}/auth/register",
            json={
                "email": email,
                "full_name": "Sneaky Person",
                "password": STRONG,
                "role": "ADMIN",
            },
        )
        assert response.status_code == 201
        assert response.json()["role"] == "INDIVIDUAL"

    async def test_a_weak_password_is_refused(self, client):
        """All letters breaks the mix rule, even at a legal length."""
        response = await client.post(
            f"{API}/auth/register",
            json={
                "email": f"weak-{uuid.uuid4().hex[:8]}@example.com",
                "full_name": "Weak Password",
                "password": "passwordpassword",
            },
        )
        assert response.status_code == 422, response.text
        assert "mix" in response.text.lower()

    async def test_a_duplicate_email_is_refused(self, client):
        email = f"twice-{uuid.uuid4().hex[:8]}@example.com"
        body = {"email": email, "full_name": "First Time", "password": STRONG}
        assert (await client.post(f"{API}/auth/register", json=body)).status_code == 201
        assert (await client.post(f"{API}/auth/register", json=body)).status_code == 409

    async def test_a_registered_individual_can_sign_in(self, client):
        email = f"signin-{uuid.uuid4().hex[:8]}@example.com"
        await client.post(
            f"{API}/auth/register",
            json={"email": email, "full_name": "Sign In", "password": STRONG},
        )
        response = await client.post(f"{API}/auth/login", json={"email": email, "password": STRONG})
        assert response.status_code == 200
        assert response.json()["user"]["role"] == "INDIVIDUAL"


# ------------------------------------------------------------- the feed


@pytest.fixture
async def individual(client, make_user):
    """A signed-in individual, returning the client and their user."""

    async def _make():
        user = await make_user(Role.INDIVIDUAL)
        response = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        return client, user

    return _make


@pytest.mark.anyio
class TestFeed:
    async def test_a_job_added_appears_in_the_feed(self, individual):
        client, _ = await individual()
        created = await client.post(f"{API}/job-feed/jobs", json=job())
        assert created.status_code == 201, created.text

        listed = (await client.get(f"{API}/job-feed")).json()
        titles = [item["title"] for item in listed["items"]]
        assert "Senior Python Developer" in titles

    async def test_workplace_type_is_read_from_the_description(self, individual):
        client, _ = await individual()
        created = await client.post(f"{API}/job-feed/jobs", json=job())
        assert created.json()["workplace_type"] == "REMOTE"

    async def test_an_untagged_job_is_unknown_not_onsite(self, individual):
        client, _ = await individual()
        created = await client.post(
            f"{API}/job-feed/jobs",
            json=job(title="Untagged Role", description="A good opportunity."),
        )
        assert created.json()["workplace_type"] == "UNKNOWN"

    async def test_the_same_job_twice_is_one_item(self, individual):
        client, _ = await individual()
        marker = uuid.uuid4().hex[:8]
        body = job(title=f"Duplicate Role {marker}")

        first = await client.post(f"{API}/job-feed/jobs", json=body)
        second = await client.post(f"{API}/job-feed/jobs", json=body)

        assert first.json()["id"] == second.json()["id"]
        listed = (await client.get(f"{API}/job-feed", params={"q": marker})).json()
        assert listed["total"] == 1

    async def test_marking_read_and_saving(self, individual):
        client, _ = await individual()
        item_id = (await client.post(f"{API}/job-feed/jobs", json=job())).json()["id"]

        saved = await client.patch(f"{API}/job-feed/{item_id}", json={"is_saved": True})
        assert saved.status_code == 200
        # Saving implies having seen it.
        assert saved.json()["is_saved"] is True
        assert saved.json()["is_read"] is True


@pytest.mark.anyio
class TestFilter:
    async def test_the_filter_returns_only_that_arrangement(self, individual):
        client, _ = await individual()
        marker = uuid.uuid4().hex[:8]
        for title, description in (
            (f"Remote {marker}", "Fully remote position."),
            (f"Onsite {marker}", "Strictly on-site in Doha."),
            (f"Hybrid {marker}", "Hybrid, three days a week."),
            (f"Untagged {marker}", "No arrangement stated."),
        ):
            await client.post(
                f"{API}/job-feed/jobs", json=job(title=title, description=description)
            )

        for workplace, expected in (
            ("REMOTE", f"Remote {marker}"),
            ("ONSITE", f"Onsite {marker}"),
            ("HYBRID", f"Hybrid {marker}"),
            ("UNKNOWN", f"Untagged {marker}"),
        ):
            listed = (
                await client.get(
                    f"{API}/job-feed", params={"workplace_type": workplace, "q": marker}
                )
            ).json()
            titles = [item["title"] for item in listed["items"]]
            assert titles == [expected], f"{workplace} returned {titles}"

    async def test_counts_agree_with_the_list(self, individual):
        client, _ = await individual()
        await client.post(
            f"{API}/job-feed/jobs",
            json=job(title=f"Counted {uuid.uuid4().hex[:8]}", description="Remote role."),
        )
        counts = (await client.get(f"{API}/job-feed/counts")).json()
        listed = (await client.get(f"{API}/job-feed")).json()
        assert counts["total"] == listed["total"]


# ------------------------------------------------------------- privacy


@pytest.mark.anyio
class TestPrivacy:
    """The guarantee: a feed belongs to one person and to nobody else."""

    async def test_one_individual_cannot_see_anothers_feed(self, client, make_user):
        first = await make_user(Role.INDIVIDUAL)
        second = await make_user(Role.INDIVIDUAL)
        password = TEST_PASSWORD

        async def sign_in(user):
            response = await client.post(
                f"{API}/auth/login", json={"email": user.email, "password": password}
            )
            client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"

        await sign_in(first)
        marker = uuid.uuid4().hex[:8]
        item_id = (
            await client.post(f"{API}/job-feed/jobs", json=job(title=f"Private {marker}"))
        ).json()["id"]

        await sign_in(second)
        listed = (await client.get(f"{API}/job-feed")).json()
        assert all(marker not in item["title"] for item in listed["items"])

        # 404, not 403. A 403 would confirm the item exists.
        assert (await client.get(f"{API}/job-feed/{item_id}")).status_code == 404

    async def test_an_individual_cannot_edit_anothers_item(self, client, make_user):
        first = await make_user(Role.INDIVIDUAL)
        second = await make_user(Role.INDIVIDUAL)
        password = TEST_PASSWORD

        async def sign_in(user):
            response = await client.post(
                f"{API}/auth/login", json={"email": user.email, "password": password}
            )
            client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"

        await sign_in(first)
        item_id = (await client.post(f"{API}/job-feed/jobs", json=job())).json()["id"]

        await sign_in(second)
        response = await client.patch(f"{API}/job-feed/{item_id}", json={"is_saved": True})
        assert response.status_code == 404

    async def test_even_an_administrator_cannot_read_an_individuals_feed(
        self, client, make_user, as_role
    ):
        """Admin holds every permission. It still sees only its own rows."""
        person = await make_user(Role.INDIVIDUAL)
        response = await client.post(
            f"{API}/auth/login",
            json={"email": person.email, "password": TEST_PASSWORD},
        )
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"

        marker = uuid.uuid4().hex[:8]
        item_id = (
            await client.post(f"{API}/job-feed/jobs", json=job(title=f"Hidden {marker}"))
        ).json()["id"]

        admin, _ = await as_role(Role.ADMIN)
        assert (await admin.get(f"{API}/job-feed/{item_id}")).status_code == 404

        listed = (await admin.get(f"{API}/job-feed")).json()
        assert all(marker not in item["title"] for item in listed["items"])


@pytest.mark.anyio
class TestStaffBoundary:
    async def test_staff_roles_cannot_reach_the_feed(self, as_role):
        """Sales holds 43 permissions. None of them is job_feed:read."""
        sales, _ = await as_role(Role.SALES)
        assert (await sales.get(f"{API}/job-feed")).status_code == 403

    async def test_an_individual_cannot_reach_the_business(self, client, make_user):
        person = await make_user(Role.INDIVIDUAL)
        response = await client.post(
            f"{API}/auth/login",
            json={"email": person.email, "password": TEST_PASSWORD},
        )
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"

        for path in ("/accounts", "/requirements", "/resources", "/billing/records", "/audit"):
            assert (await client.get(f"{API}{path}")).status_code == 403, path
