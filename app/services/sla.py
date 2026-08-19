"""Submission SLA state (SOW section 5 NEW, ASSUMPTIONS.md A11-A12).

Many requirements — especially those arriving through a VMS or MSP — carry a
strict submission window, commonly 24 to 48 hours. Missing it loses the seat
however good the candidate is, so the deadline is a first-class field and its
state is computed fresh on every read rather than stored and left stale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.models.demand import DeadlineState, PrioritySource


@dataclass(frozen=True, slots=True)
class DeadlineStatus:
    state: DeadlineState
    deadline: datetime | None
    hours_remaining: float | None
    is_overdue: bool

    @property
    def needs_attention(self) -> bool:
        return self.state in {DeadlineState.DUE_SOON, DeadlineState.URGENT, DeadlineState.EXPIRED}


def deadline_status(deadline: datetime | None, *, now: datetime | None = None) -> DeadlineStatus:
    """Classify a submission deadline.

    Thresholds come from settings so an operator can tune urgency without a
    deploy: URGENT under 8 hours, DUE_SOON under 24 (A12).
    """
    if deadline is None:
        return DeadlineStatus(DeadlineState.NONE, None, None, False)

    reference = now or datetime.now(UTC)
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)

    remaining = (deadline - reference).total_seconds() / 3600

    if remaining <= 0:
        return DeadlineStatus(DeadlineState.EXPIRED, deadline, round(remaining, 2), True)
    if remaining < settings.SLA_URGENT_HOURS:
        state = DeadlineState.URGENT
    elif remaining < settings.SLA_DUE_SOON_HOURS:
        state = DeadlineState.DUE_SOON
    else:
        state = DeadlineState.SAFE

    return DeadlineStatus(state, deadline, round(remaining, 2), False)


def default_deadline_for(
    priority_source: PrioritySource, *, now: datetime | None = None
) -> datetime | None:
    """Suggest a deadline when the source implies one but none was stated.

    Only P5 (vendor / MSP / VMS) gets a default, because only that channel
    reliably imposes a window. Inventing urgency for other sources would train
    users to ignore the alerts (A11).
    """
    if priority_source is not PrioritySource.P5_VENDOR_MSP_VMS:
        return None
    reference = now or datetime.now(UTC)
    return reference + timedelta(hours=settings.SLA_DEFAULT_HOURS_P5)


def describe(status: DeadlineStatus) -> str:
    """A short human phrase for a list row or a notification."""
    if status.state is DeadlineState.NONE or status.hours_remaining is None:
        return "No deadline set"
    if status.is_overdue:
        overdue = abs(status.hours_remaining)
        return f"Expired {_humanise(overdue)} ago"
    return f"{_humanise(status.hours_remaining)} left"


def _humanise(hours: float) -> str:
    if hours < 1:
        minutes = max(1, int(hours * 60))
        return f"{minutes}m"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours // 24)}d"


__all__ = [
    "DeadlineStatus",
    "deadline_status",
    "default_deadline_for",
    "describe",
]
