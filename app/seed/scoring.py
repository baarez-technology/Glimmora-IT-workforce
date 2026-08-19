"""Seed the v1 scoring configuration.

The engine seeds this lazily on first use, but seeding it here means a fresh
development database shows the rule set in Admin > Scoring before anybody has
run a match — an empty configuration screen reads like a broken feature.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.engines.matching.config import default_payload
from app.models.matching import ScoringConfigKind, ScoringConfiguration

logger = get_logger("seed")


async def seed_scoring_configurations(session: AsyncSession) -> int:
    """Create MATCH_WEIGHTS v1 if no configuration of that kind exists."""
    existing = (
        (
            await session.execute(
                select(ScoringConfiguration).where(
                    ScoringConfiguration.kind == ScoringConfigKind.MATCH_WEIGHTS
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return 0

    session.add(
        ScoringConfiguration(
            kind=ScoringConfigKind.MATCH_WEIGHTS,
            name="Baseline matching weights",
            version=1,
            is_active=True,
            payload=default_payload(),
            notes=(
                "Documented defaults from SCORING.md section 3. Create a new "
                "version rather than editing this one — historical match scores "
                "reference the version that produced them."
            ),
        )
    )
    logger.info("seed.scoring_configuration.created", kind="MATCH_WEIGHTS", version=1)
    return 1


__all__ = ["seed_scoring_configurations"]
