"""Billing period arithmetic.

Pure functions over dates and money, so the one question that matters here —
*does the sum of the monthly rows equal what we actually billed?* — is testable
without a database.

The hard part is partial months. A consultant starting on the 20th does not
earn a full month, and a system that bills one anyway will be corrected by the
client's accounts payable department, which is an expensive way to find a bug.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

CENTS = Decimal("0.01")
_ONE_DAY = timedelta(days=1)


def _money(value: Decimal | float | int) -> Decimal:
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass(slots=True, frozen=True)
class Period:
    year: int
    month: int

    @property
    def label(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def last_day(self) -> date:
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    def next(self) -> Period:
        return Period(self.year + 1, 1) if self.month == 12 else Period(self.year, self.month + 1)

    def __lt__(self, other: Period) -> bool:
        return (self.year, self.month) < (other.year, other.month)


def period_of(value: date) -> Period:
    return Period(value.year, value.month)


def periods_between(start: date, end: date) -> list[Period]:
    """Every calendar month a deployment touches, inclusive.

    An open-ended deployment has no `end`; the caller decides the horizon rather
    than this function inventing one.
    """
    if end < start:
        return []

    periods: list[Period] = []
    current = period_of(start)
    final = period_of(end)
    while current < final or current == final:
        periods.append(current)
        if current == final:
            break
        current = current.next()
    return periods


def working_days(start: date, end: date) -> int:
    """Weekdays in an inclusive range.

    Monday-to-Friday is an approximation — the Gulf working week is commonly
    Sunday to Thursday — but the count is only ever used as a *ratio* against a
    configured `working_days_per_month`, so the shape of the week cancels out.
    """
    if end < start:
        return 0
    total = 0
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            total += 1
        cursor += _ONE_DAY
    return total


@dataclass(slots=True)
class PeriodCoverage:
    """How much of one month a deployment actually covers."""

    period: Period
    covered_from: date
    covered_to: date
    billable_days: int
    full_month_days: int

    @property
    def is_partial(self) -> bool:
        return self.billable_days < self.full_month_days

    @property
    def ratio(self) -> Decimal:
        """Share of the month worked, 0-1."""
        if self.full_month_days <= 0:
            return Decimal("0")
        return Decimal(self.billable_days) / Decimal(self.full_month_days)


def coverage(period: Period, *, start: date, end: date | None) -> PeriodCoverage | None:
    """The overlap between a deployment and one calendar month."""
    period_start = period.first_day
    period_end = period.last_day

    covered_from = max(start, period_start)
    covered_to = min(end, period_end) if end is not None else period_end
    if covered_to < covered_from:
        return None

    return PeriodCoverage(
        period=period,
        covered_from=covered_from,
        covered_to=covered_to,
        billable_days=working_days(covered_from, covered_to),
        full_month_days=working_days(period_start, period_end),
    )


@dataclass(slots=True)
class PeriodAmounts:
    revenue: Decimal
    cost: Decimal
    gross_profit: Decimal
    margin_percent: float | None
    billable_days: int
    is_partial: bool


def amounts_for(
    cover: PeriodCoverage,
    *,
    monthly_revenue: Decimal,
    monthly_cost: Decimal,
) -> PeriodAmounts:
    """Pro-rate a month's revenue and cost by the days actually covered.

    Both sides are pro-rated. Charging a full month of cost against a partial
    month of revenue would show a loss that never happened.
    """
    ratio = cover.ratio
    revenue = _money(Decimal(str(monthly_revenue)) * ratio)
    cost = _money(Decimal(str(monthly_cost)) * ratio)
    profit = _money(revenue - cost)

    margin = float((profit / revenue * 100).quantize(CENTS)) if revenue > 0 else None

    return PeriodAmounts(
        revenue=revenue,
        cost=cost,
        gross_profit=profit,
        margin_percent=margin,
        billable_days=cover.billable_days,
        is_partial=cover.is_partial,
    )


__all__ = [
    "Period",
    "PeriodAmounts",
    "PeriodCoverage",
    "amounts_for",
    "coverage",
    "period_of",
    "periods_between",
    "working_days",
]
