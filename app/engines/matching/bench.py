"""Zero-bench milestones.

A consultant rolling off in 7 days is a revenue emergency; one rolling off in 90
days is a planning item. Both need an alert, but not the same alert, and neither
needs the same alert every morning for three months.

This module holds the milestone arithmetic on its own so it can be tested
without a database, a scheduler or a clock — the "fires once, not daily"
guarantee in the Phase 8 definition of done is a property of these functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class AlertSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


#: Severity escalates as the runway shortens. The boundaries are days remaining.
SEVERITY_BY_MILESTONE: dict[int, AlertSeverity] = {
    90: AlertSeverity.INFO,
    60: AlertSeverity.INFO,
    30: AlertSeverity.WARNING,
    15: AlertSeverity.WARNING,
    7: AlertSeverity.CRITICAL,
}


@dataclass(slots=True, frozen=True)
class BenchMilestone:
    """One resource, one milestone, one alert."""

    resource_id: object
    milestone_days: int
    available_on: date
    days_remaining: int
    severity: AlertSeverity

    @property
    def dedupe_key(self) -> str:
        """Stable per (resource, milestone).

        The sweep runs daily and a 30-day milestone stays "reached" for several
        runs, so the key deliberately excludes the run date: re-raising it
        tomorrow must collide with what was raised today.
        """
        return f"bench:{self.resource_id}:{self.milestone_days}"

    @property
    def headline(self) -> str:
        if self.days_remaining <= 0:
            return "On the bench now"
        return f"Available in {self.days_remaining} days"


def milestone_for(days_remaining: int, milestones: list[int]) -> int | None:
    """The milestone a runway of `days_remaining` has reached, if any.

    Returns the **tightest** milestone reached, so a resource first seen at 12
    days out gets the 15-day alert rather than silently skipping to 7. A sweep
    that misses a day — a failed worker, a weekend outage — must not lose the
    milestone entirely.
    """
    if days_remaining < 0:
        return None
    reached = [milestone for milestone in sorted(milestones) if days_remaining <= milestone]
    return reached[0] if reached else None


def evaluate_resource(
    *,
    resource_id: object,
    available_from: date | None,
    availability_status: str,
    today: date,
    milestones: list[int],
) -> BenchMilestone | None:
    """Whether this resource has hit a milestone worth alerting on.

    Only a *deployed* consultant with a known end date has a runway. Someone
    already `AVAILABLE` is not approaching the bench — they are on it, which is
    the bench radar's job, not the milestone sweep's.
    """
    if availability_status not in {"DEPLOYED", "AVAILABLE_SOON"}:
        return None
    if available_from is None:
        return None

    days_remaining = (available_from - today).days
    milestone = milestone_for(days_remaining, milestones)
    if milestone is None:
        return None

    return BenchMilestone(
        resource_id=resource_id,
        milestone_days=milestone,
        available_on=available_from,
        days_remaining=days_remaining,
        severity=SEVERITY_BY_MILESTONE.get(milestone, AlertSeverity.INFO),
    )


__all__ = [
    "SEVERITY_BY_MILESTONE",
    "AlertSeverity",
    "BenchMilestone",
    "evaluate_resource",
    "milestone_for",
]
