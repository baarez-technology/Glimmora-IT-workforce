"""Match snapshots and versioned scoring configuration.

Two architecture decisions live here:

* **AD-2 — scoring rules are data, not code.** Weights are rows in
  `scoring_configurations`, so changing a business rule is an Admin action
  rather than a deploy. Every score records the config version that produced
  it, so historical scores stay explainable after the rules change.
* **AD-4 — matches are persisted snapshots.** Matching is expensive (vector
  recall plus rule scoring plus a narrative), and the dashboard funnel has to
  be countable. Recompute is explicit and timestamped.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import BaseEntity
from app.db.types import GUID, JSONType, ScoreType, StrEnumType, UTCDateTime


class MatchDirection(StrEnum):
    DEMAND_TO_RESOURCE = "DEMAND_TO_RESOURCE"
    RESOURCE_TO_DEMAND = "RESOURCE_TO_DEMAND"


class ScoringConfigKind(StrEnum):
    MATCH_WEIGHTS = "MATCH_WEIGHTS"
    ADDRESSABILITY_RULES = "ADDRESSABILITY_RULES"
    COMMERCIAL_BANDS = "COMMERCIAL_BANDS"
    OPPORTUNITY_WEIGHTS = "OPPORTUNITY_WEIGHTS"


class MatchBand(StrEnum):
    """How a match reads at a glance. Bands are advice, never a decision."""

    STRONG = "STRONG"
    GOOD = "GOOD"
    POSSIBLE = "POSSIBLE"
    WEAK = "WEAK"


class ScoringConfiguration(BaseEntity):
    __tablename__ = "scoring_configurations"

    kind: Mapped[ScoringConfigKind] = mapped_column(
        StrEnumType(ScoringConfigKind), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(default=False, nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("kind", "version", name="scoring_configuration_version"),
        Index("ix_scoring_configurations_active", "kind", "is_active"),
    )

    @property
    def label(self) -> str:
        return f"{self.kind.value} v{self.version}"


class Match(BaseEntity):
    """One requirement-resource pairing, with every number that produced it.

    A match is never shown as a bare percentage: the component scores, gaps,
    reasons and warnings stored here are what the UI is required to render
    alongside the total (MATCHING.md section 5).
    """

    __tablename__ = "matches"

    requirement_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("resources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direction: Mapped[MatchDirection] = mapped_column(
        StrEnumType(MatchDirection),
        nullable=False,
        default=MatchDirection.DEMAND_TO_RESOURCE,
        index=True,
    )

    overall_score: Mapped[Decimal] = mapped_column(ScoreType, nullable=False, index=True)
    band: Mapped[MatchBand] = mapped_column(StrEnumType(MatchBand), nullable=False, index=True)

    skill_score: Mapped[Decimal | None] = mapped_column(ScoreType, nullable=True)
    experience_score: Mapped[Decimal | None] = mapped_column(ScoreType, nullable=True)
    technology_score: Mapped[Decimal | None] = mapped_column(ScoreType, nullable=True)
    availability_score: Mapped[Decimal | None] = mapped_column(ScoreType, nullable=True)
    location_score: Mapped[Decimal | None] = mapped_column(ScoreType, nullable=True)
    cost_score: Mapped[Decimal | None] = mapped_column(ScoreType, nullable=True)
    commercial_score: Mapped[Decimal | None] = mapped_column(ScoreType, nullable=True)
    semantic_score: Mapped[Decimal | None] = mapped_column(ScoreType, nullable=True)

    #: Share of the weight that was answered by known data. A 72 at confidence
    #: 0.45 is a different thing from a 72 at confidence 0.95.
    confidence: Mapped[float] = mapped_column(nullable=False, default=1.0)

    components: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    gaps: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)
    reasons: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)
    warnings: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)
    missing_information: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)

    weights_version: Mapped[int | None] = mapped_column(nullable=True)
    engine_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0.0")
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    computed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    requirement: Mapped[Any] = relationship("Requirement", lazy="raise")
    resource: Mapped[Any] = relationship("Resource", lazy="raise")

    __table_args__ = (
        UniqueConstraint("requirement_id", "resource_id", "direction", name="match_unique"),
        Index("ix_matches_requirement_rank", "requirement_id", "overall_score"),
        Index("ix_matches_resource_rank", "resource_id", "overall_score"),
    )


__all__ = [
    "Match",
    "MatchBand",
    "MatchDirection",
    "ScoringConfigKind",
    "ScoringConfiguration",
]
