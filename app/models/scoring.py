"""Opportunity score snapshots.

Unlike `matches`, which are replaced on every run, scores are **append-only**
(DATABASE.md section 3). A score is a judgement made at a point in time under a
particular rule set; overwriting it would destroy the ability to answer "why did
we decline this in March?" — which is exactly the question a post-mortem asks.

`is_current` marks the newest row per requirement so the common read stays one
indexed lookup rather than a sort over history.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import BaseEntity
from app.db.types import GUID, JSONType, MoneyType, ScoreType, StrEnumType, UTCDateTime


class OpportunityBandEnum(StrEnum):
    PURSUE_NOW = "PURSUE_NOW"
    PURSUE = "PURSUE"
    REVIEW = "REVIEW"
    DEPRIORITIZE = "DEPRIORITIZE"


class AddressabilityBandEnum(StrEnum):
    HIGHLY_ADDRESSABLE = "HIGHLY_ADDRESSABLE"
    ADDRESSABLE = "ADDRESSABLE"
    CONDITIONAL = "CONDITIONAL"
    NOT_ADDRESSABLE = "NOT_ADDRESSABLE"


class OpportunityScore(BaseEntity):
    __tablename__ = "opportunity_scores"

    requirement_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Populated from Phase 10, when `opportunities` becomes the unit of pursuit.
    #: The relationship is 1:1 with the requirement, so scoring one scores both.
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)

    # --- the three components ------------------------------------------
    talent_match_score: Mapped[Decimal | None] = mapped_column(ScoreType, nullable=True)
    addressability_score: Mapped[Decimal | None] = mapped_column(ScoreType, nullable=True)
    commercial_score: Mapped[Decimal | None] = mapped_column(ScoreType, nullable=True)
    opportunity_score: Mapped[Decimal] = mapped_column(ScoreType, nullable=False, index=True)

    band: Mapped[OpportunityBandEnum] = mapped_column(
        StrEnumType(OpportunityBandEnum), nullable=False, index=True
    )
    addressability_band: Mapped[AddressabilityBandEnum | None] = mapped_column(
        StrEnumType(AddressabilityBandEnum), nullable=True, index=True
    )
    #: Share of component weight answered by known data.
    confidence: Mapped[float] = mapped_column(nullable=False, default=1.0)
    #: The multiplier applied because of talent supply, kept so an addressability
    #: score can be re-derived from its raw total.
    supply_gate: Mapped[float | None] = mapped_column(nullable=True)

    # --- commercial snapshot -------------------------------------------
    # Denormalised deliberately: the pipeline board sorts and sums on these, and
    # digging them out of JSON on every row would not survive a real dataset.
    monthly_revenue: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    monthly_cost: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    gross_profit: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    margin_percent: Mapped[float | None] = mapped_column(nullable=True)
    contract_value: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    total_profit: Mapped[Decimal | None] = mapped_column(MoneyType, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="QAR")
    is_converted: Mapped[bool] = mapped_column(default=False, nullable=False)

    # --- the explanation object ----------------------------------------
    components: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    factor_breakdown: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)
    commercial_breakdown: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)
    positives: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)
    risks: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)
    missing_information: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)
    suppressors: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)
    recommended_action: Mapped[str | None] = mapped_column(String(255), nullable=True)
    narrative: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- provenance -----------------------------------------------------
    addressability_config_version: Mapped[int | None] = mapped_column(nullable=True)
    commercial_config_version: Mapped[int | None] = mapped_column(nullable=True)
    opportunity_config_version: Mapped[int | None] = mapped_column(nullable=True)
    match_config_version: Mapped[int | None] = mapped_column(nullable=True)
    engine_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0.0")

    #: Newest row per requirement. History is retained; this keeps the hot read
    #: to a single indexed lookup.
    is_current: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, index=True)
    computed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        Index("ix_opportunity_scores_current", "requirement_id", "is_current"),
        Index("ix_opportunity_scores_rank", "is_current", "opportunity_score"),
        Index("ix_opportunity_scores_history", "requirement_id", "computed_at"),
    )


__all__ = [
    "AddressabilityBandEnum",
    "OpportunityBandEnum",
    "OpportunityScore",
]
