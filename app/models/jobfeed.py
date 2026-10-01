"""The individual job feed.

Two tables, and the split between them is the important decision.

A **posting** is the job itself, deduplicated globally. Ten individuals can be
alerted to the same vacancy at Milaha; that is one `job_postings` row.

A **feed item** is one person's copy of it: when it reached them, whether they
have read it, whether they saved it. This is private data. No role grants
access to another person's items — scoping is by row, enforced in the service,
the same way the notification inbox already works.

Storing the job on each feed item instead would make deduplication impossible
and would copy the same text once per recipient.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseEntity
from app.db.types import StrEnumType, UTCDateTime


class WorkplaceType(StrEnum):
    """Where the work happens.

    `UNKNOWN` is a real answer, not a missing one. Job alerts frequently omit
    the arrangement, and defaulting those to ONSITE would quietly mislabel
    every remote role the source failed to tag — the same reason the scoring
    engine refuses to treat an absent fact as a zero.
    """

    ONSITE = "ONSITE"
    REMOTE = "REMOTE"
    HYBRID = "HYBRID"
    UNKNOWN = "UNKNOWN"


class JobSource(StrEnum):
    """Where the posting reached us from."""

    LINKEDIN_ALERT = "LINKEDIN_ALERT"
    SEARCH = "SEARCH"
    MANUAL = "MANUAL"


class JobPosting(BaseEntity):
    """A vacancy, stored once however many people are alerted to it."""

    __tablename__ = "job_postings"

    #: Stable identity for a vacancy across repeated alerts: a hash of company,
    #: title and location. The same job arrives in alert after alert, day after
    #: day, often to several people -- without this the feed drowns in a week.
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    title: Mapped[str] = mapped_column(String(240), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)

    workplace_type: Mapped[WorkplaceType] = mapped_column(
        StrEnumType(WorkplaceType), default=WorkplaceType.UNKNOWN, nullable=False, index=True
    )

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    source: Mapped[JobSource] = mapped_column(StrEnumType(JobSource), nullable=False, index=True)
    #: Which provider, where there is one: "adzuna", "jooble", "linkedin".
    source_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    posted_at: Mapped[date | None] = mapped_column(nullable=True)
    #: What the source actually said, kept so a parser change can be re-run
    #: over history rather than losing what we could not read at the time.
    raw_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list[JobFeedItem]] = relationship(
        back_populates="posting", cascade="all, delete-orphan", lazy="raise", passive_deletes=True
    )

    __table_args__ = (Index("ix_job_postings_workplace_posted", "workplace_type", "posted_at"),)

    def __repr__(self) -> str:
        return f"<JobPosting {self.title!r} at {self.company_name!r}>"


class JobFeedItem(BaseEntity):
    """One person's copy of a posting. Private to them.

    `shared_by_user_id` is what makes a handover between Sales and Resourcing
    legible. A shared job is not a different kind of row — it is an ordinary
    feed item that happens to record who put it there, so it dedupes, filters
    and saves exactly like one that arrived by alert or search.
    """

    __tablename__ = "job_feed_items"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    posting_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False, index=True
    )

    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    is_read: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)
    is_saved: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)

    # --- handover between colleagues ------------------------------------
    #: Who passed this across, when somebody did. NULL for a job that arrived
    #: by alert or search. SET NULL on delete so the note survives the sender
    #: leaving: "shared by a former colleague" beats losing the row.
    shared_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: What they said when they sent it. Optional: most handovers are obvious
    #: from the job itself.
    share_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    shared_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True, index=True)
    #: Cleared when the recipient opens it, so the nav badge can count
    #: handovers without counting every unread job in the feed.
    share_acknowledged: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)

    posting: Mapped[JobPosting] = relationship(back_populates="items", lazy="raise")
    shared_by: Mapped[Any] = relationship("User", foreign_keys=[shared_by_user_id], lazy="selectin")

    @property
    def is_shared(self) -> bool:
        return self.shared_by_user_id is not None

    __table_args__ = (
        # The same job reaching the same person twice is one item, updated.
        UniqueConstraint("user_id", "posting_id", name="uq_job_feed_items_user_posting"),
        Index("ix_job_feed_items_user_received", "user_id", "received_at"),
    )

    def __repr__(self) -> str:
        return f"<JobFeedItem user={self.user_id} posting={self.posting_id}>"
