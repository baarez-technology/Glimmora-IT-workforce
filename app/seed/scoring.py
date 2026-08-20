"""Seed the v1 scoring configuration.

The engine seeds this lazily on first use, but seeding it here means a fresh
development database shows the rule set in Admin > Scoring before anybody has
run a match — an empty configuration screen reads like a broken feature.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.matching import ScoringConfigKind, ScoringConfiguration
from app.services.matching import defaults_for

logger = get_logger("seed")


#: One seeded v1 per rule set, with a note explaining what editing it means.
_SEEDS: list[tuple[ScoringConfigKind, str, str]] = [
    (
        ScoringConfigKind.MATCH_WEIGHTS,
        "Baseline matching weights",
        "Documented defaults from SCORING.md section 3.",
    ),
    (
        ScoringConfigKind.ADDRESSABILITY_RULES,
        "Baseline addressability rules",
        "Eight factors summing to 100, plus the supply gate (SCORING.md section 2).",
    ),
    (
        ScoringConfigKind.COMMERCIAL_BANDS,
        "Baseline commercial bands",
        "Margin 60 / contract value 25 / duration 15 (SCORING.md section 4b).",
    ),
    (
        ScoringConfigKind.OPPORTUNITY_WEIGHTS,
        "Baseline opportunity weights",
        (
            "Talent 0.40 / addressability 0.35 / commercial 0.25. These reproduce "
            "the SOW worked example (94/88/91 -> 91), which is why they were chosen."
        ),
    ),
]

_SHARED_NOTE = (
    " Create a new version rather than editing this one — historical scores "
    "reference the version that produced them."
)


async def seed_scoring_configurations(session: AsyncSession) -> int:
    """Create v1 of every rule set that does not have one yet."""
    created = 0
    for kind, name, note in _SEEDS:
        existing = (
            (
                await session.execute(
                    select(ScoringConfiguration).where(ScoringConfiguration.kind == kind)
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            continue

        session.add(
            ScoringConfiguration(
                kind=kind,
                name=name,
                version=1,
                is_active=True,
                payload=defaults_for(kind),
                notes=note + _SHARED_NOTE,
            )
        )
        logger.info("seed.scoring_configuration.created", kind=kind.value, version=1)
        created += 1
    return created


__all__ = ["seed_scoring_configurations"]
