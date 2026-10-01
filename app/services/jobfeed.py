"""The individual job feed.

**Every read and every write in this module is scoped to one user**, by row,
in the query itself. There is no permission that grants access to another
person's feed — not Admin's, not Management's. The notification inbox already
works this way; this follows it deliberately.

A missing item and someone else's item are indistinguishable from outside:
both raise `NotFoundError`. Returning 403 for the second case would confirm
the item exists, which is itself a disclosure.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import NotFoundError
from app.models.identity import User
from app.models.jobfeed import JobFeedItem, JobPosting, JobSource, WorkplaceType


def fingerprint_for(title: str, company: str | None, location: str | None) -> str:
    """Stable identity for a vacancy across repeated alerts.

    Company, title and location, lowercased and stripped. Deliberately not the
    URL: the same job carries different tracking parameters in every alert
    email, so URLs would make every re-send look like a new vacancy.
    """
    parts = [
        (company or "").strip().lower(),
        title.strip().lower(),
        (location or "").strip().lower(),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


#: Words a source might use for each arrangement. Anything unrecognised stays
#: UNKNOWN rather than being guessed into a bucket.
_WORKPLACE_WORDS: dict[WorkplaceType, tuple[str, ...]] = {
    WorkplaceType.REMOTE: ("remote", "work from home", "wfh", "telecommute", "anywhere"),
    WorkplaceType.HYBRID: ("hybrid", "flexible", "partially remote", "part remote"),
    WorkplaceType.ONSITE: ("on-site", "onsite", "on site", "in office", "in-office"),
}


def classify_workplace(text: str | None) -> WorkplaceType:
    """Read the arrangement out of free text, or admit we could not.

    Hybrid is checked before remote: "hybrid remote" is hybrid, and matching
    remote first would mislabel it.
    """
    if not text:
        return WorkplaceType.UNKNOWN
    lowered = text.lower()
    for workplace in (WorkplaceType.HYBRID, WorkplaceType.REMOTE, WorkplaceType.ONSITE):
        if any(word in lowered for word in _WORKPLACE_WORDS[workplace]):
            return workplace
    return WorkplaceType.UNKNOWN


class JobFeedService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # ------------------------------------------------------------ reading

    async def list_items(
        self,
        *,
        actor: User,
        workplace_type: WorkplaceType | None = None,
        unread_only: bool = False,
        saved_only: bool = False,
        q: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[JobFeedItem], int]:
        """This person's feed, newest first.

        The `user_id` filter is not optional and not a parameter -- it is taken
        from the authenticated actor every time.
        """
        conditions = [JobFeedItem.user_id == actor.id]
        if unread_only:
            conditions.append(JobFeedItem.is_read.is_(False))
        if saved_only:
            conditions.append(JobFeedItem.is_saved.is_(True))

        if workplace_type is not None:
            conditions.append(JobPosting.workplace_type == workplace_type)
        if q:
            needle = f"%{q.strip().lower()}%"
            conditions.append(
                func.lower(JobPosting.title).like(needle)
                | func.lower(func.coalesce(JobPosting.company_name, "")).like(needle)
            )

        base = (
            select(JobFeedItem)
            .join(JobPosting, JobPosting.id == JobFeedItem.posting_id)
            .where(*conditions)
        )

        total = (
            await self.session.execute(select(func.count()).select_from(base.subquery()))
        ).scalar_one()

        rows = (
            (
                await self.session.execute(
                    base.options(selectinload(JobFeedItem.posting))
                    .order_by(JobFeedItem.received_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), int(total)

    async def counts(self, *, actor: User) -> dict[str, int]:
        """Totals for the filter chips, so a count never contradicts a list."""
        rows = await self.session.execute(
            select(JobPosting.workplace_type, func.count())
            .join(JobFeedItem, JobFeedItem.posting_id == JobPosting.id)
            .where(JobFeedItem.user_id == actor.id)
            .group_by(JobPosting.workplace_type)
        )
        by_workplace = {row[0].value: int(row[1]) for row in rows}

        unread = (
            await self.session.execute(
                select(func.count())
                .select_from(JobFeedItem)
                .where(JobFeedItem.user_id == actor.id, JobFeedItem.is_read.is_(False))
            )
        ).scalar_one()
        saved = (
            await self.session.execute(
                select(func.count())
                .select_from(JobFeedItem)
                .where(JobFeedItem.user_id == actor.id, JobFeedItem.is_saved.is_(True))
            )
        ).scalar_one()

        return {
            "total": sum(by_workplace.values()),
            "unread": int(unread),
            "saved": int(saved),
            **{
                workplace.value: by_workplace.get(workplace.value, 0) for workplace in WorkplaceType
            },
        }

    async def get_item(self, item_id: uuid.UUID, *, actor: User) -> JobFeedItem:
        """One item, if it is this person's.

        Someone else's item raises NotFoundError, not a permission error: a 403
        would confirm the item exists.
        """
        item = (
            await self.session.execute(
                select(JobFeedItem)
                .options(selectinload(JobFeedItem.posting))
                .where(JobFeedItem.id == item_id, JobFeedItem.user_id == actor.id)
            )
        ).scalar_one_or_none()
        if item is None:
            raise NotFoundError("job feed item", item_id)
        return item

    # ------------------------------------------------------------ writing

    async def set_state(
        self,
        item_id: uuid.UUID,
        *,
        actor: User,
        is_read: bool | None = None,
        is_saved: bool | None = None,
    ) -> JobFeedItem:
        item = await self.get_item(item_id, actor=actor)
        if is_read is not None:
            item.is_read = is_read
        if is_saved is not None:
            item.is_saved = is_saved
            # Saving something implies having seen it.
            if is_saved:
                item.is_read = True
        await self.session.flush()
        return item

    async def mark_all_read(self, *, actor: User) -> int:
        rows = (
            (
                await self.session.execute(
                    select(JobFeedItem).where(
                        JobFeedItem.user_id == actor.id, JobFeedItem.is_read.is_(False)
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.is_read = True
        await self.session.flush()
        return len(rows)

    # ------------------------------------------------------------ writing in

    async def deliver(
        self,
        *,
        actor: User,
        title: str,
        company_name: str | None = None,
        location: str | None = None,
        country: str | None = None,
        workplace_type: WorkplaceType | None = None,
        description: str | None = None,
        url: str | None = None,
        source: JobSource = JobSource.MANUAL,
        source_name: str | None = None,
        posted_at: date | None = None,
        raw_excerpt: str | None = None,
        received_at: datetime | None = None,
    ) -> JobFeedItem:
        """Put a posting into one person's feed.

        The posting is shared and deduplicated by fingerprint; the feed item is
        private. Delivering the same job to the same person twice updates the
        existing item rather than creating a second one.

        This is the seam the email capture and the search providers will both
        use, so they inherit deduplication for free.
        """
        if workplace_type is None:
            workplace_type = classify_workplace(" ".join(filter(None, [description, location])))

        key = fingerprint_for(title, company_name, location)
        posting = (
            await self.session.execute(select(JobPosting).where(JobPosting.fingerprint == key))
        ).scalar_one_or_none()

        if posting is None:
            posting = JobPosting(
                fingerprint=key,
                title=title.strip(),
                company_name=(company_name or "").strip() or None,
                location=(location or "").strip() or None,
                country=(country or "").strip().upper()[:2] or None,
                workplace_type=workplace_type,
                description=description,
                url=url,
                source=source,
                source_name=source_name,
                posted_at=posted_at,
                raw_excerpt=raw_excerpt,
            )
            self.session.add(posting)
            await self.session.flush()
        elif posting.workplace_type is WorkplaceType.UNKNOWN and workplace_type is not (
            WorkplaceType.UNKNOWN
        ):
            # A later sighting can fill in what the first one did not say. It
            # may never overwrite a known value with a different guess.
            posting.workplace_type = workplace_type

        existing = (
            await self.session.execute(
                select(JobFeedItem).where(
                    JobFeedItem.user_id == actor.id, JobFeedItem.posting_id == posting.id
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            existing.received_at = received_at or datetime.now(UTC)
            await self.session.flush()
            return existing

        item = JobFeedItem(
            user_id=actor.id,
            posting_id=posting.id,
            received_at=received_at or datetime.now(UTC),
        )
        self.session.add(item)
        await self.session.flush()
        return item


def serialise(item: JobFeedItem) -> dict[str, Any]:
    """Flatten an item and its posting into one object for the client."""
    posting = item.posting
    return {
        "id": item.id,
        "received_at": item.received_at,
        "is_read": item.is_read,
        "is_saved": item.is_saved,
        "posting_id": posting.id,
        "title": posting.title,
        "company_name": posting.company_name,
        "location": posting.location,
        "country": posting.country,
        "workplace_type": posting.workplace_type,
        "description": posting.description,
        "url": posting.url,
        "source": posting.source,
        "source_name": posting.source_name,
        "posted_at": posting.posted_at,
    }
