"""Inbound LinkedIn job alerts.

Two things carry the weight here. The parser must not invent jobs from an
email that is not an alert, and the webhook must not let anyone write into
somebody else's feed — a forwarded email is attacker-controlled input, and the
only thing deciding whose feed it lands in is the recipient address.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.core.permissions import Role
from app.engines.jobalerts.addressing import address_for, matches, token_from_recipients
from app.engines.jobalerts.linkedin import looks_like_linkedin, parse_alert
from app.models.jobfeed import WorkplaceType
from app.services.jobalerts import extract_recipients, first_item, sender_of
from tests.conftest import TEST_PASSWORD

API = "/api/v1"


def alert_html(*jobs: tuple[str, str, str, str]) -> str:
    """An alert shaped like LinkedIn's, with (id, title, company, location)."""
    blocks = "".join(
        f"""
        <tr><td>
          <a href="https://www.linkedin.com/comm/jobs/view/{job_id}?trk=eml">{title}</a>
          <div>{company}</div>
          <div>{location}</div>
          <div>Actively recruiting</div>
        </td></tr>
        """
        for job_id, title, company, location in jobs
    )
    return f"""<html><body><table>
      <tr><td><p>Your job alert for python developer</p>
      <p>{len(jobs)} new jobs match your preferences.</p></td></tr>
      {blocks}
      <tr><td><p>See all jobs</p><p>Unsubscribe</p><p>LinkedIn Corporation</p></td></tr>
    </table></body></html>"""


SAMPLE = alert_html(
    ("4424248218", "Senior Backend Engineer", "Snoonu", "Doha, Qatar (Remote)"),
    ("4426070689", "Python Developer", "Ras Laffan Logistics", "Doha, Qatar (Hybrid)"),
    ("4421119900", "Lead Platform Engineer", "Qatar Civic Digital Authority", "Doha, Qatar"),
)


class TestRecognition:
    def test_a_linkedin_alert_is_recognised(self):
        assert looks_like_linkedin("jobalerts-noreply@linkedin.com", "Your job alert", SAMPLE)

    def test_a_newsletter_is_not_mangled_into_jobs(self):
        """The failure this prevents: fabricating vacancies from any email."""
        alert = parse_alert(
            body_html="<p>Our weekly newsletter, from LinkedIn fans</p>",
            subject="News",
            sender="news@example.com",
        )
        assert alert.is_linkedin is False
        assert alert.jobs == []

    def test_mentioning_linkedin_is_not_enough_without_job_links(self):
        assert not looks_like_linkedin("a@b.com", "About LinkedIn", "<p>LinkedIn is a website</p>")


class TestParsing:
    def setup_method(self):
        self.alert = parse_alert(
            body_html=SAMPLE, subject="Your job alert", sender="jobalerts-noreply@linkedin.com"
        )

    def test_every_job_is_found(self):
        assert len(self.alert.jobs) == 3
        assert self.alert.unparsed == []

    def test_the_title_comes_from_the_link_not_the_preamble(self):
        """An earlier version ran the email's preamble into the first title.

        The job id lives in the href, which flattening discards, so anchoring
        on text loses the boundary between the header and the first job.
        """
        assert self.alert.jobs[0].title == "Senior Backend Engineer"
        assert "new jobs match" not in self.alert.jobs[0].title

    def test_company_and_location_are_separated(self):
        job = self.alert.jobs[0]
        assert job.company_name == "Snoonu"
        assert job.location == "Doha, Qatar"

    @pytest.mark.parametrize(
        ("index", "expected"),
        [(0, WorkplaceType.REMOTE), (1, WorkplaceType.HYBRID), (2, WorkplaceType.UNKNOWN)],
    )
    def test_the_arrangement_is_read_or_left_unknown(self, index, expected):
        # The third job states no arrangement. UNKNOWN rather than ONSITE:
        # guessing would mislabel every untagged remote role.
        assert self.alert.jobs[index].workplace_type is expected

    def test_the_url_is_rebuilt_without_tracking(self):
        """Every send carries different tracking, which would defeat dedupe."""
        assert self.alert.jobs[0].url == "https://www.linkedin.com/jobs/view/4424248218"
        assert "trk=" not in self.alert.jobs[0].url

    def test_the_same_job_twice_in_one_email_is_counted_once(self):
        doubled = alert_html(
            ("4424248218", "Senior Backend Engineer", "Snoonu", "Doha, Qatar"),
            ("4424248218", "Senior Backend Engineer", "Snoonu", "Doha, Qatar"),
        )
        alert = parse_alert(body_html=doubled, sender="jobs-noreply@linkedin.com")
        assert len(alert.jobs) == 1


class TestAddressing:
    def test_an_address_is_stable_for_a_user(self):
        user_id = uuid.uuid4()
        assert address_for(user_id) == address_for(user_id)

    def test_two_users_get_different_addresses(self):
        assert address_for(uuid.uuid4()) != address_for(uuid.uuid4())

    def test_the_address_carries_the_configured_domain(self):
        assert address_for(uuid.uuid4()).endswith(f"@{settings.BREVO_INBOUND_DOMAIN}")

    def test_a_token_round_trips_from_the_recipient_list(self):
        user_id = uuid.uuid4()
        token = token_from_recipients(["someone@else.com", address_for(user_id)])
        assert token is not None
        assert matches(user_id, token)

    def test_another_users_token_does_not_match(self):
        token = token_from_recipients([address_for(uuid.uuid4())])
        assert not matches(uuid.uuid4(), token or "")

    def test_an_unrelated_recipient_yields_nothing(self):
        assert token_from_recipients(["support@glimmora.ai"]) is None
        assert token_from_recipients([]) is None


class TestBrevoPayload:
    def test_recipients_are_gathered_from_every_field(self):
        payload = {
            "To": [{"Address": "jobs+abc@jobs.glimmora.ai"}],
            "Cc": [{"Address": "someone@else.com"}],
        }
        assert "jobs+abc@jobs.glimmora.ai" in extract_recipients(payload)

    def test_the_first_item_is_taken_from_a_wrapped_payload(self):
        assert first_item({"items": [{"Subject": "Hello"}]})["Subject"] == "Hello"

    def test_a_bare_payload_is_accepted(self):
        assert first_item({"Subject": "Hello"})["Subject"] == "Hello"

    def test_the_sender_is_read_from_either_shape(self):
        assert sender_of({"From": {"Address": "a@b.com"}}) == "a@b.com"
        assert sender_of({"From": "a@b.com"}) == "a@b.com"


@pytest.fixture
async def individual(client, make_user):
    async def _make():
        user = await make_user(Role.INDIVIDUAL)
        response = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
        client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"
        return client, user

    return _make


@pytest.mark.anyio
class TestConnection:
    async def test_the_forwarding_address_is_shown(self, individual):
        client, user = await individual()
        body = (await client.get(f"{API}/job-feed/alerts/connection")).json()
        assert body["forwarding_address"] == address_for(user.id)

    async def test_unverified_until_something_actually_arrives(self, individual):
        """Verified is derived from mail received, never from a flag.

        A screen that claims a working connection before anything has arrived
        is the kind of thing somebody debugs for an hour.
        """
        client, _ = await individual()
        body = (await client.get(f"{API}/job-feed/alerts/connection")).json()
        assert body["verified"] is False
        assert body["count"] == 0


@pytest.mark.anyio
class TestPaste:
    async def test_a_pasted_alert_becomes_feed_items(self, individual):
        client, _ = await individual()
        response = await client.post(
            f"{API}/job-feed/alerts/paste",
            json={"body": SAMPLE, "subject": "Your job alert", "sender": "x@linkedin.com"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "recognised": True,
            "added": 3,
            "unparsed": 0,
            "message": None,
        }

        listed = (await client.get(f"{API}/job-feed")).json()
        assert listed["total"] >= 3

    async def test_pasting_the_same_alert_twice_does_not_duplicate(self, individual):
        client, _ = await individual()
        body = {"body": SAMPLE, "sender": "x@linkedin.com"}
        await client.post(f"{API}/job-feed/alerts/paste", json=body)
        before = (await client.get(f"{API}/job-feed")).json()["total"]
        await client.post(f"{API}/job-feed/alerts/paste", json=body)
        after = (await client.get(f"{API}/job-feed")).json()["total"]
        assert after == before

    async def test_a_newsletter_is_refused_plainly(self, individual):
        client, _ = await individual()
        response = await client.post(
            f"{API}/job-feed/alerts/paste",
            json={"body": "<p>Our weekly newsletter with plenty of text</p>"},
        )
        assert response.json()["recognised"] is False
        assert response.json()["added"] == 0

    async def test_a_paste_lands_in_the_callers_own_feed(self, client, make_user):
        """Headers cannot redirect a paste into somebody else's feed."""
        first = await make_user(Role.INDIVIDUAL)
        second = await make_user(Role.INDIVIDUAL)

        async def sign_in(user):
            response = await client.post(
                f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
            )
            client.headers["Authorization"] = f"Bearer {response.json()['access_token']}"

        await sign_in(first)
        marker = uuid.uuid4().hex[:8]
        await client.post(
            f"{API}/job-feed/alerts/paste",
            json={
                "body": alert_html(("991" + marker[:6], f"Marked {marker}", "Acme", "Doha")),
                "sender": "x@linkedin.com",
            },
        )

        await sign_in(second)
        listed = (await client.get(f"{API}/job-feed")).json()
        assert all(marker not in item["title"] for item in listed["items"])


@pytest.mark.anyio
class TestWebhook:
    async def test_a_wrong_secret_is_not_found_rather_than_forbidden(self, client, monkeypatch):
        """404, not 403. A 403 confirms the endpoint exists."""
        monkeypatch.setattr(settings, "JOB_ALERTS_WEBHOOK_SECRET", "the-real-secret")
        response = await client.post(f"{API}/inbound/brevo/wrong-secret", json={"items": []})
        assert response.status_code == 404

    async def test_nothing_is_accepted_when_no_secret_is_configured(self, client, monkeypatch):
        monkeypatch.setattr(settings, "JOB_ALERTS_WEBHOOK_SECRET", None)
        response = await client.post(f"{API}/inbound/brevo/anything", json={"items": []})
        assert response.status_code == 404

    async def test_an_unknown_recipient_is_rejected(self, client, monkeypatch):
        monkeypatch.setattr(settings, "JOB_ALERTS_WEBHOOK_SECRET", "secret-value")
        response = await client.post(
            f"{API}/inbound/brevo/secret-value",
            json={
                "items": [
                    {
                        "Subject": "Your job alert",
                        "From": {"Address": "jobs-noreply@linkedin.com"},
                        "To": [{"Address": "nobody@jobs.glimmora.ai"}],
                        "RawHtmlBody": SAMPLE,
                    }
                ]
            },
        )
        assert response.status_code == 404

    async def test_a_valid_alert_reaches_the_right_feed(self, client, make_user, monkeypatch):
        monkeypatch.setattr(settings, "JOB_ALERTS_WEBHOOK_SECRET", "secret-value")
        user = await make_user(Role.INDIVIDUAL)

        response = await client.post(
            f"{API}/inbound/brevo/secret-value",
            json={
                "items": [
                    {
                        "Subject": "Your job alert",
                        "From": {"Address": "jobs-noreply@linkedin.com"},
                        "To": [{"Address": address_for(user.id)}],
                        "RawHtmlBody": SAMPLE,
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["added"] == 3

        login = await client.post(
            f"{API}/auth/login", json={"email": user.email, "password": TEST_PASSWORD}
        )
        client.headers["Authorization"] = f"Bearer {login.json()['access_token']}"

        connection = (await client.get(f"{API}/job-feed/alerts/connection")).json()
        assert connection["verified"] is True
        assert connection["count"] == 3
