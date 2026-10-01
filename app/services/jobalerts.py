"""Turning a forwarded LinkedIn alert into feed items.

The delivery path is deliberately the same one the search screen uses, so
alerts inherit deduplication without a second implementation: the same vacancy
arriving by email on Monday and found by search on Tuesday is one posting and
one feed item.

Nothing here trusts the sender. An inbound email names its recipient, and that
recipient's token is the only thing that decides whose feed is written to.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger, log_business_event
from app.engines.jobalerts.addressing import matches, token_from_recipients
from app.engines.jobalerts.linkedin import ParsedAlert, parse_alert
from app.models.identity import User
from app.models.jobfeed import JobSource
from app.services.jobfeed import JobFeedService

logger = get_logger("jobalerts")


class JobAlertService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.feed = JobFeedService(session)

    async def _recipient(self, recipients: list[str] | None) -> User:
        """Whose address this email was sent to.

        An unrecognised address is a 404 rather than an error worth retrying:
        Brevo receives everything on the domain, including mail nobody here
        has an address for.
        """
        token = token_from_recipients(recipients)
        if not token:
            raise NotFoundError("job alert recipient")

        # The token is an HMAC of the user id, so it cannot be looked up
        # directly. The candidate set is individuals, which is small, and the
        # comparison is constant time.
        rows = (
            (await self.session.execute(select(User).where(User.is_active.is_(True))))
            .scalars()
            .all()
        )
        for user in rows:
            if matches(user.id, token):
                return user
        raise NotFoundError("job alert recipient")

    async def ingest(
        self,
        *,
        recipients: list[str] | None,
        subject: str | None = None,
        sender: str | None = None,
        body_html: str | None = None,
        body_text: str | None = None,
        received_at: datetime | None = None,
        actor: User | None = None,
    ) -> dict[str, Any]:
        """One email in, feed items out.

        `actor` short-circuits recipient resolution for the paste-an-email
        path, where somebody is importing into their own feed and there is no
        envelope to read.
        """
        user = actor or await self._recipient(recipients)

        parsed: ParsedAlert = parse_alert(
            body_html=body_html, body_text=body_text, subject=subject, sender=sender
        )

        if not parsed.is_linkedin:
            # Said plainly rather than treated as a failure: somebody
            # forwarded a newsletter, and inventing jobs from it would be
            # worse than refusing.
            return {
                "recognised": False,
                "added": 0,
                "unparsed": 0,
                "message": "That does not look like a LinkedIn job alert.",
            }

        when = received_at or datetime.now(UTC)
        added = 0
        for job in parsed.jobs[: settings.JOB_ALERTS_MAX_PER_EMAIL]:
            await self.feed.deliver(
                actor=user,
                title=job.title,
                company_name=job.company_name,
                location=job.location,
                workplace_type=job.workplace_type,
                url=job.url,
                source=JobSource.LINKEDIN_ALERT,
                source_name="linkedin",
                received_at=when,
                raw_excerpt=None,
            )
            added += 1

        if parsed.unparsed:
            # Logged, not discarded. A parser improvement can be run over the
            # history, and the count tells us when LinkedIn changed its layout.
            logger.warning(
                "job_alert_partially_parsed",
                user_id=str(user.id),
                parsed=added,
                unparsed=len(parsed.unparsed),
            )

        log_business_event(
            "job_alert_ingested",
            user_id=str(user.id),
            jobs=added,
            unparsed=len(parsed.unparsed),
        )
        return {
            "recognised": True,
            "added": added,
            "unparsed": len(parsed.unparsed),
            "message": None,
        }

    async def connection_state(self, *, actor: User) -> dict[str, Any]:
        """What the connect screen needs to tell the truth.

        Verified is derived from whether anything has actually arrived rather
        than from a flag somebody set, so the screen cannot claim a working
        connection that has never delivered.
        """
        from sqlalchemy import func

        from app.models.jobfeed import JobFeedItem, JobPosting

        received = (
            await self.session.execute(
                select(func.count(), func.max(JobFeedItem.received_at))
                .select_from(JobFeedItem)
                .join(JobPosting, JobPosting.id == JobFeedItem.posting_id)
                .where(
                    JobFeedItem.user_id == actor.id,
                    JobPosting.source == JobSource.LINKEDIN_ALERT,
                )
            )
        ).one()

        return {
            "enabled": settings.JOB_ALERTS_ENABLED,
            "count": int(received[0] or 0),
            "last_received_at": received[1],
        }


def verify_webhook_secret(secret: str) -> None:
    """The webhook's only authentication.

    Brevo does not sign inbound payloads, so the path carries a secret. Without
    one configured the endpoint refuses everything rather than accepting
    anonymous posts into people's feeds.
    """
    import hmac

    expected = settings.JOB_ALERTS_WEBHOOK_SECRET
    if not expected:
        raise NotFoundError("webhook")
    if not hmac.compare_digest(secret, expected):
        raise NotFoundError("webhook")


def extract_recipients(payload: dict[str, Any]) -> list[str]:
    """Every address Brevo reports, flattened.

    Brevo gives `To`, `Cc` and `Recipients` as lists of objects with an
    `Address`. A forward can land on any of them.
    """
    found: list[str] = []
    for key in ("Recipients", "To", "Cc"):
        value = payload.get(key) or []
        if isinstance(value, str):
            found.append(value)
            continue
        for entry in value:
            if isinstance(entry, str):
                found.append(entry)
            elif isinstance(entry, dict):
                address = entry.get("Address") or entry.get("address")
                if address:
                    found.append(str(address))
    return found


def first_item(payload: dict[str, Any]) -> dict[str, Any]:
    """Brevo posts `{"items": [...]}`; a bare object is also accepted."""
    items = payload.get("items")
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            return first
    if payload.get("items") is not None and not payload.get("Subject"):
        raise ValidationError("That payload contained no email.")
    return payload


def sender_of(payload: dict[str, Any]) -> str | None:
    value = payload.get("From")
    if isinstance(value, dict):
        return value.get("Address") or value.get("address")
    return value if isinstance(value, str) else None


def received_at_of(payload: dict[str, Any]) -> datetime | None:
    raw = payload.get("SentAtDate") or payload.get("ReceivedAt")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = [
    "JobAlertService",
    "extract_recipients",
    "first_item",
    "received_at_of",
    "sender_of",
    "verify_webhook_secret",
]
