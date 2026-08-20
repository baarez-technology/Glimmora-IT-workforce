"""Simulation preview: what would this rule change actually do?

An admin editing a weight is changing how the business prioritises its sales
effort. Doing that blind — activate, then find out — is how a scoring system
loses trust in its first week. This re-scores recent requirements under the
draft rules and reports the delta *before* anything is activated.

Nothing here persists a score. The simulation is a read.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.demand import Requirement
from app.models.identity import User
from app.models.matching import ScoringConfiguration
from app.models.scoring import OpportunityScore
from app.services.scoring import ScoringService, validate_payload

logger = get_logger("simulation")


class SimulationService:
    def __init__(self, session: Any) -> None:
        self.session = session
        self.scoring = ScoringService(session)

    async def simulate(
        self, config_id: uuid.UUID, *, limit: int = 50, actor: User | None = None
    ) -> dict[str, Any]:
        draft = (
            await self.session.execute(
                select(ScoringConfiguration).where(ScoringConfiguration.id == config_id)
            )
        ).scalar_one_or_none()
        if draft is None:
            raise NotFoundError("scoring configuration", config_id)

        # Simulating a rule set that would be rejected at activation wastes the
        # admin's time and produces meaningless numbers.
        validate_payload(draft.kind, draft.payload)
        if draft.is_active:
            raise ValidationError(
                "This version is already active — simulate a draft version instead.",
                details=[{"field": "config_id", "message": "already active"}],
            )

        rows = await self.session.execute(
            select(OpportunityScore)
            .where(OpportunityScore.is_current.is_(True))
            .order_by(OpportunityScore.computed_at.desc())
            .limit(limit)
        )
        current = list(rows.scalars().all())

        titles: dict[uuid.UUID, str] = {}
        if current:
            title_rows = await self.session.execute(
                select(Requirement.id, Requirement.title).where(
                    Requirement.id.in_([item.requirement_id for item in current])
                )
            )
            titles = {row[0]: row[1] for row in title_rows}

        results: list[dict[str, Any]] = []
        before_dist: dict[str, int] = {}
        after_dist: dict[str, int] = {}

        for snapshot in current:
            opportunity, _, _, _ = await self.scoring.score_requirement(
                snapshot.requirement_id,
                persist=False,
                config_overrides={draft.kind: draft},
            )

            before = float(snapshot.opportunity_score)
            after = float(opportunity.score)
            before_band = snapshot.band.value
            after_band = opportunity.band.value

            before_dist[before_band] = before_dist.get(before_band, 0) + 1
            after_dist[after_band] = after_dist.get(after_band, 0) + 1

            results.append(
                {
                    "requirement_id": snapshot.requirement_id,
                    "requirement_title": titles.get(snapshot.requirement_id),
                    "before_score": before,
                    "after_score": after,
                    "delta": round(after - before, 1),
                    "before_band": before_band,
                    "after_band": after_band,
                    "band_changed": before_band != after_band,
                }
            )

        # Biggest movers first: an admin wants to see what breaks, not what
        # stayed the same.
        results.sort(key=lambda row: abs(row["delta"]), reverse=True)

        changed = len([row for row in results if row["delta"] != 0])
        band_changes = len([row for row in results if row["band_changed"]])
        average = round(sum(row["delta"] for row in results) / len(results), 2) if results else 0.0

        logger.info(
            "simulation_complete",
            config_id=str(config_id),
            kind=draft.kind.value,
            evaluated=len(results),
            band_changes=band_changes,
        )

        return {
            "kind": draft.kind,
            "evaluated": len(results),
            "changed": changed,
            "band_changes": band_changes,
            "average_delta": average,
            "distribution_before": before_dist,
            "distribution_after": after_dist,
            "rows": results,
        }


__all__ = ["SimulationService"]
