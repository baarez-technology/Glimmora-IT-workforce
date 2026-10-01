"""The job offers workspace.

The privacy guarantee is the point of this file. A feed is private by row, not
by role, and the tests that matter most are the ones asserting that nobody —
including an administrator — can reach somebody else's items.

Sharing is the deliberate exception, and it is tested here too: a handover
writes into a colleague's rows, which is the one thing row-scoping otherwise
forbids, so it must only ever happen through `share` and only to real staff.
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


class TestFeedRoles:
    """Who holds the job offers workspace, now that it is staff tooling."""

    @pytest.mark.parametrize("role", [Role.SALES, Role.HR_RESOURCING, Role.ADMIN])
    def test_sourcing_roles_hold_the_feed(self, role):
        granted = permissions_for(role)
        assert Permission.JOB_FEED_READ in granted
        assert Permission.JOB_FEED_WRITE in granted
        assert Permission.JOB_FEED_SHARE in granted

    def test_management_does_not(self):
        """Management observes the business; it does not source the market."""
        granted = permissions_for(Role.MANAGEMENT)
        assert Permission.JOB_FEED_READ not in granted
        assert Permission.JOB_FEED_SHARE not in granted

    def test_the_individual_role_is_gone(self):
        """The platform has no external users. Nothing may reintroduce one
        by accident, so this asserts the enum itself."""
        assert not hasattr(Role, "INDIVIDUAL")
        assert {r.value for r in Role} == {"ADMIN", "MANAGEMENT", "SALES", "HR_RESOURCING"}


@pytest.mark.anyio
class TestRegistrationIsGone:
    async def test_public_registration_is_not_routed(self, client):
        """Self-service signup existed only to create INDIVIDUAL accounts."""
        response = await client.post(
            f"{API}/auth/register",
            json={
                "email": "outsider@example.com",
                "full_name": "Outsider",
                "password": "Str0ng-Passphrase!",
            },
        )
        assert response.status_code == 404


@pytest.fixture
async def individual(client, make_user):
    """A signed-in member of staff holding the feed, with their user.

    Named `individual` still because every caller reads "one person's own
    feed" — the privacy guarantee it exercises is per user, and that did not
    change when the audience became staff.
    """

    async def _make():
        user = await make_user(Role.SALES)
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
        first = await make_user(Role.SALES)
        second = await make_user(Role.SALES)
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
        first = await make_user(Role.SALES)
        second = await make_user(Role.SALES)
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
        person = await make_user(Role.SALES)
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
class TestRoleBoundary:
    @pytest.mark.parametrize("role", [Role.SALES, Role.HR_RESOURCING])
    async def test_sourcing_roles_reach_the_feed(self, as_role, role):
        caller, _ = await as_role(role)
        assert (await caller.get(f"{API}/job-feed")).status_code == 200

    async def test_management_cannot_reach_the_feed(self, as_role):
        """Granting the feed to Sales and Resourcing must not grant it to all
        staff: Management holds neither read nor share."""
        management, _ = await as_role(Role.MANAGEMENT)
        assert (await management.get(f"{API}/job-feed")).status_code == 403
        assert (await management.get(f"{API}/job-feed/colleagues")).status_code == 403


# ------------------------------------------------------------------ sharing


@pytest.mark.anyio
class TestSharing:
    async def test_a_job_reaches_the_colleague_it_was_sent_to(
        self, app, client, make_user, as_role
    ):
        recipient = await make_user(Role.HR_RESOURCING)
        sales, _ = await as_role(Role.SALES)

        created = await sales.post(f"{API}/job-feed/jobs", json=job())
        assert created.status_code == 201, created.text
        item_id = created.json()["id"]

        shared = await sales.post(
            f"{API}/job-feed/{item_id}/share",
            json={"recipient_ids": [str(recipient.id)], "note": "Fits Priya"},
        )
        assert shared.status_code == 200, shared.text
        assert shared.json()["delivered"] == 1

        # The recipient now holds it, stamped with who sent it.
        response = await client.post(
            f"{API}/auth/login",
            json={"email": recipient.email, "password": TEST_PASSWORD},
        )
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        feed = await client.get(f"{API}/job-feed", params={"shared_only": True})
        assert feed.status_code == 200
        items = feed.json()["items"]
        assert len(items) == 1
        assert items[0]["share_note"] == "Fits Priya"
        assert items[0]["shared_by_role"] == "SALES"
        assert items[0]["share_acknowledged"] is False

    async def test_sharing_does_not_reach_back_into_the_senders_feed(self, as_role, make_user):
        """A handover is one-way: the sender's own copy is untouched."""
        recipient = await make_user(Role.HR_RESOURCING)
        sales, _sender = await as_role(Role.SALES)

        item_id = (await sales.post(f"{API}/job-feed/jobs", json=job())).json()["id"]
        await sales.post(
            f"{API}/job-feed/{item_id}/share", json={"recipient_ids": [str(recipient.id)]}
        )

        mine = (await sales.get(f"{API}/job-feed", params={"shared_only": True})).json()
        assert mine["total"] == 0

    async def test_sending_to_yourself_is_dropped_not_refused(self, as_role):
        sales, sender = await as_role(Role.SALES)
        item_id = (await sales.post(f"{API}/job-feed/jobs", json=job())).json()["id"]

        shared = await sales.post(
            f"{API}/job-feed/{item_id}/share", json={"recipient_ids": [str(sender.id)]}
        )
        assert shared.status_code == 200
        assert shared.json()["delivered"] == 0

    async def test_you_cannot_share_an_item_that_is_not_yours(self, as_role, make_user, client):
        """The share path must not become a way to read somebody else's feed."""
        other = await make_user(Role.SALES)
        recipient = await make_user(Role.HR_RESOURCING)

        # `other` creates an item.
        login = await client.post(
            f"{API}/auth/login", json={"email": other.email, "password": TEST_PASSWORD}
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        foreign_id = (await client.post(f"{API}/job-feed/jobs", json=job())).json()["id"]

        sales, _ = await as_role(Role.SALES)
        stolen = await sales.post(
            f"{API}/job-feed/{foreign_id}/share",
            json={"recipient_ids": [str(recipient.id)]},
        )
        assert stolen.status_code == 404

    async def test_the_colleague_picker_excludes_you_and_anyone_without_the_feed(self, as_role):
        sales, me = await as_role(Role.SALES)
        people = (await sales.get(f"{API}/job-feed/colleagues")).json()

        assert all(person["id"] != str(me.id) for person in people)
        assert all(person["role"] in {"SALES", "HR_RESOURCING", "ADMIN"} for person in people)

    async def test_acknowledging_clears_the_badge_without_marking_jobs_read(
        self, app, client, make_user, as_role
    ):
        recipient = await make_user(Role.HR_RESOURCING)
        sales, _ = await as_role(Role.SALES)
        item_id = (await sales.post(f"{API}/job-feed/jobs", json=job())).json()["id"]
        await sales.post(
            f"{API}/job-feed/{item_id}/share", json={"recipient_ids": [str(recipient.id)]}
        )

        login = await client.post(
            f"{API}/auth/login", json={"email": recipient.email, "password": TEST_PASSWORD}
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        assert (await client.get(f"{API}/job-feed/counts")).json()["shared"] == 1
        assert (await client.post(f"{API}/job-feed/shares/acknowledge")).json()["acknowledged"] == 1

        items = (await client.get(f"{API}/job-feed", params={"shared_only": True})).json()["items"]
        assert items[0]["share_acknowledged"] is True
        # Acknowledging the handover is not the same as reading the job.
        assert items[0]["is_read"] is False


# ------------------------------------------------- counts, scoped and honest


@pytest.mark.anyio
class TestScopedCounts:
    async def test_saved_counts_describe_the_saved_list_not_the_whole_feed(self, individual):
        """"All 2" above an empty Saved list is a chip contradicting itself."""
        client, _ = await individual()
        await client.post(f"{API}/job-feed/jobs", json=job(title="Kept Role"))
        await client.post(f"{API}/job-feed/jobs", json=job(title="Ignored Role"))

        feed_wide = (await client.get(f"{API}/job-feed/counts")).json()
        assert feed_wide["total"] == 2

        saved_view = (
            await client.get(f"{API}/job-feed/counts", params={"saved_only": True})
        ).json()
        assert saved_view["total"] == 0
        assert sum(saved_view[w] for w in ("ONSITE", "REMOTE", "HYBRID", "UNKNOWN")) == 0
        # The badge numbers stay feed-wide wherever you are standing.
        assert saved_view["unread"] == 2

    async def test_the_handover_badge_clears_when_acknowledged(
        self, client, make_user, as_role
    ):
        recipient = await make_user(Role.HR_RESOURCING)
        sales, _ = await as_role(Role.SALES)
        item_id = (await sales.post(f"{API}/job-feed/jobs", json=job())).json()["id"]
        await sales.post(
            f"{API}/job-feed/{item_id}/share", json={"recipient_ids": [str(recipient.id)]}
        )

        login = await client.post(
            f"{API}/auth/login", json={"email": recipient.email, "password": TEST_PASSWORD}
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        before = (await client.get(f"{API}/job-feed/counts")).json()
        assert before["shared_unack"] == 1

        await client.post(f"{API}/job-feed/shares/acknowledge")
        after = (await client.get(f"{API}/job-feed/counts")).json()
        assert after["shared_unack"] == 0
        # Still shared, just seen.
        assert after["shared"] == 1


# ----------------------------------------------------------------- removing


@pytest.mark.anyio
class TestRemoving:
    async def test_a_job_can_be_removed_from_your_feed(self, individual):
        client, _ = await individual()
        item_id = (await client.post(f"{API}/job-feed/jobs", json=job())).json()["id"]

        assert (await client.delete(f"{API}/job-feed/{item_id}")).status_code == 204
        assert (await client.get(f"{API}/job-feed")).json()["total"] == 0

    async def test_removing_your_copy_leaves_a_colleagues_alone(
        self, client, make_user, as_role
    ):
        """The posting is shared; the feed item is not. Removing one must not
        take the job out of everybody else's feed."""
        recipient = await make_user(Role.HR_RESOURCING)
        sales, _ = await as_role(Role.SALES)
        item_id = (await sales.post(f"{API}/job-feed/jobs", json=job())).json()["id"]
        await sales.post(
            f"{API}/job-feed/{item_id}/share", json={"recipient_ids": [str(recipient.id)]}
        )

        assert (await sales.delete(f"{API}/job-feed/{item_id}")).status_code == 204

        login = await client.post(
            f"{API}/auth/login", json={"email": recipient.email, "password": TEST_PASSWORD}
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        assert (await client.get(f"{API}/job-feed")).json()["total"] == 1

    async def test_you_cannot_remove_somebody_elses_item(self, client, make_user, as_role):
        other = await make_user(Role.SALES)
        login = await client.post(
            f"{API}/auth/login", json={"email": other.email, "password": TEST_PASSWORD}
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        foreign_id = (await client.post(f"{API}/job-feed/jobs", json=job())).json()["id"]

        sales, _ = await as_role(Role.SALES)
        assert (await sales.delete(f"{API}/job-feed/{foreign_id}")).status_code == 404


# --------------------------------------------- adding a job is a hand-off


@pytest.mark.anyio
class TestAddAndShare:
    async def test_adding_with_recipients_delivers_in_one_call(
        self, client, make_user, as_role
    ):
        """A job found off-platform is a lead. It reaches the people who act
        on it in the same request, so a failed hand-off cannot leave an orphan
        in the finder's own feed."""
        recipient = await make_user(Role.HR_RESOURCING)
        sales, _ = await as_role(Role.SALES)

        created = await sales.post(
            f"{API}/job-feed/jobs",
            json=job(
                title="10 React Developers",
                recipient_ids=[str(recipient.id)],
                share_note="Client is hiring at scale",
            ),
        )
        assert created.status_code == 201, created.text

        # The finder keeps their own copy, unmarked: they did not receive it.
        mine = (await sales.get(f"{API}/job-feed")).json()
        assert mine["total"] == 1
        assert mine["items"][0]["shared_by_name"] is None

        login = await client.post(
            f"{API}/auth/login", json={"email": recipient.email, "password": TEST_PASSWORD}
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"
        theirs = (await client.get(f"{API}/job-feed", params={"shared_only": True})).json()
        assert theirs["total"] == 1
        assert theirs["items"][0]["share_note"] == "Client is hiring at scale"

    async def test_adding_without_recipients_still_works_for_saving_a_search_hit(
        self, individual
    ):
        """The search screen saves through this path, and keeping something to
        read later is not a hand-off."""
        client, _ = await individual()
        created = await client.post(f"{API}/job-feed/jobs", json=job())
        assert created.status_code == 201
        assert created.json()["shared_by_name"] is None

    async def test_saving_actually_marks_it_saved(self, individual):
        """The search screen's Save goes through this endpoint.

        It shipped setting neither flag, so the button said "Saved to your
        feed", the job landed unsaved, and the Saved tab stayed empty while
        the feed filled up. The two asserts below are the whole bug.
        """
        client, _ = await individual()
        # A title of its own: postings dedupe globally and a posting records
        # where it was *first* seen, so reusing one would assert nothing.
        created = await client.post(
            f"{API}/job-feed/jobs",
            json=job(title="Platform Engineer, Kept", is_saved=True, source="SEARCH"),
        )
        assert created.status_code == 201, created.text

        body = created.json()
        assert body["is_saved"] is True
        assert body["source"] == "SEARCH"

        saved = (await client.get(f"{API}/job-feed", params={"saved_only": True})).json()
        assert saved["total"] == 1
        assert (await client.get(f"{API}/job-feed/counts")).json()["saved"] == 1

    async def test_saving_a_job_already_in_the_feed_keeps_it(self, individual):
        """Dedupe must not swallow the keep: the same job saved after it
        arrived by alert is still a job you kept."""
        client, _ = await individual()
        await client.post(f"{API}/job-feed/jobs", json=job())
        assert (await client.get(f"{API}/job-feed/counts")).json()["saved"] == 0

        await client.post(f"{API}/job-feed/jobs", json=job(is_saved=True))

        counts = (await client.get(f"{API}/job-feed/counts")).json()
        assert counts["saved"] == 1
        # Still one job, not two.
        assert counts["total"] == 1
