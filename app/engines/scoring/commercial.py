"""The commercial calculator and score: *is this worth the money and effort?*

Two separable things live here, deliberately:

* **The calculator** (section 4a) is pure arithmetic and produces no score. It is
  what a salesperson uses to answer "what would we make on this?", and it must
  be right to the cent.
* **The score** (section 4b) bands that arithmetic into 0-100 so it can compose
  into the Opportunity Score.

Margin dominates the score because Glimmora's business is margin per deployed
head: a large low-margin contract consumes bench capacity a smaller high-margin
one would monetise better.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.engines.scoring.config import BASE_CURRENCY

CENTS = Decimal("0.01")


def _money(value: Decimal | float | int | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value)).quantize(CENTS, rounding=ROUND_HALF_UP)


# ------------------------------------------------------------- conversion


@dataclass(slots=True)
class Money:
    amount: Decimal
    currency: str
    #: True when this figure was converted from another currency. Surfaced in
    #: the API so nobody treats an estimate as a quote (SCORING.md section 4a).
    is_converted: bool = False


def convert(
    amount: Decimal | None,
    currency: str | None,
    *,
    rates: dict[str, float],
    to: str = BASE_CURRENCY,
) -> Money | None:
    """Convert into the base currency, flagging that a conversion happened.

    An unknown currency returns None rather than assuming parity: silently
    treating 100 of an unrecognised unit as 100 QAR would be a wrong number
    presented with full confidence.
    """
    if amount is None:
        return None
    source = (currency or to).upper()
    if source == to:
        return Money(_money(amount) or Decimal("0"), to, is_converted=False)

    rate = rates.get(source)
    if rate is None:
        return None
    converted = _money(Decimal(str(amount)) * Decimal(str(rate)))
    return Money(converted or Decimal("0"), to, is_converted=True)


UNIT_FACTORS = {"HOURLY": "hourly", "DAILY": "daily", "MONTHLY": "monthly", "ANNUAL": "annual"}


def to_monthly(
    amount: Decimal | None, unit: str | None, *, working_days: int, hours_per_day: int
) -> Decimal | None:
    """Normalise any rate unit onto a monthly basis."""
    if amount is None or not unit:
        return None
    value = Decimal(str(amount))
    key = str(unit).upper()
    if key == "HOURLY":
        return _money(value * working_days * hours_per_day)
    if key == "DAILY":
        return _money(value * working_days)
    if key == "MONTHLY":
        return _money(value)
    if key == "ANNUAL":
        return _money(value / 12)
    return None


# ------------------------------------------------------------- calculator


@dataclass(slots=True)
class CommercialInput:
    """Everything the calculator needs. Any of it may be unknown."""

    bill_rate: Decimal | None = None
    bill_unit: str | None = None
    bill_currency: str | None = None

    cost_rate: Decimal | None = None
    cost_unit: str | None = None
    cost_currency: str | None = None

    #: One-off costs for the engagement, in the base currency.
    visa_cost: Decimal | None = None
    insurance_cost: Decimal | None = None
    other_cost: Decimal | None = None

    duration_months: int | None = None
    positions: int = 1


@dataclass(slots=True)
class CommercialCalculation:
    monthly_revenue: Decimal | None
    monthly_cost: Decimal | None
    gross_profit: Decimal | None
    margin_percent: float | None
    contract_value: Decimal | None
    total_profit: Decimal | None
    duration_months: int
    positions: int
    currency: str = BASE_CURRENCY
    is_converted: bool = False
    is_estimated: bool = False
    one_off_total: Decimal = Decimal("0.00")
    one_off_monthly: Decimal = Decimal("0.00")
    missing_information: list[str] = field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return self.margin_percent is not None


def calculate(
    data: CommercialInput, *, bands: dict[str, Any], rates: dict[str, float]
) -> CommercialCalculation:
    """Pure arithmetic. Produces no score and makes no judgement."""
    working_days = int(bands.get("working_days_per_month", 22))
    hours_per_day = int(bands.get("hours_per_day", 8))
    duration = int(data.duration_months or bands.get("default_duration_months", 12))
    positions = max(int(data.positions or 1), 1)

    missing: list[str] = []
    converted = False

    bill = convert(data.bill_rate, data.bill_currency, rates=rates)
    if data.bill_rate is not None and bill is None:
        missing.append(f"Bill rate currency {data.bill_currency} has no configured exchange rate")
    converted = converted or bool(bill and bill.is_converted)

    cost = convert(data.cost_rate, data.cost_currency, rates=rates)
    if data.cost_rate is not None and cost is None:
        missing.append(f"Cost rate currency {data.cost_currency} has no configured exchange rate")
    converted = converted or bool(cost and cost.is_converted)

    monthly_revenue = to_monthly(
        bill.amount if bill else None,
        data.bill_unit,
        working_days=working_days,
        hours_per_day=hours_per_day,
    )
    monthly_base_cost = to_monthly(
        cost.amount if cost else None,
        data.cost_unit,
        working_days=working_days,
        hours_per_day=hours_per_day,
    )

    if monthly_revenue is None:
        missing.append("Client bill rate not confirmed")
    if monthly_base_cost is None:
        missing.append("Consultant cost rate not confirmed")

    one_off_total = (
        (_money(data.visa_cost) or Decimal("0"))
        + (_money(data.insurance_cost) or Decimal("0"))
        + (_money(data.other_cost) or Decimal("0"))
    )
    # Spread across the engagement rather than charged to month one, so the
    # margin shown is the margin of the engagement rather than of one month.
    if bands.get("amortise_one_off_costs", True) and duration > 0:
        one_off_monthly = _money(one_off_total / duration) or Decimal("0")
    else:
        one_off_monthly = one_off_total

    monthly_cost = (
        _money(monthly_base_cost + one_off_monthly) if monthly_base_cost is not None else None
    )

    gross_profit: Decimal | None = None
    margin_percent: float | None = None
    contract_value: Decimal | None = None
    total_profit: Decimal | None = None

    if monthly_revenue is not None:
        contract_value = _money(monthly_revenue * duration * positions)
    if monthly_revenue is not None and monthly_cost is not None:
        gross_profit = _money(monthly_revenue - monthly_cost) or Decimal("0")
        # Guard the divide rather than letting a zero rate produce infinity.
        margin_percent = (
            float((gross_profit / monthly_revenue * 100).quantize(CENTS))
            if monthly_revenue > 0
            else 0.0
        )
        total_profit = _money(gross_profit * duration * positions)

    return CommercialCalculation(
        monthly_revenue=monthly_revenue,
        monthly_cost=monthly_cost,
        gross_profit=gross_profit,
        margin_percent=margin_percent,
        contract_value=contract_value,
        total_profit=total_profit,
        duration_months=duration,
        positions=positions,
        is_converted=converted,
        one_off_total=one_off_total,
        one_off_monthly=one_off_monthly,
        missing_information=missing,
    )


# ------------------------------------------------------------------ score


@dataclass(slots=True)
class CommercialSubScore:
    key: str
    label: str
    points: float
    max_points: float
    evidence: str


@dataclass(slots=True)
class CommercialResult:
    #: None when the rate is unknown. Never zero — an unpriced requirement is
    #: unknown, not bad (SCORING.md section 4b).
    score: float | None
    calculation: CommercialCalculation
    sub_scores: list[CommercialSubScore] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    confidence: float = 1.0


def _band_points(value: float, band_rows: list[list[float]], floor: float) -> float:
    for threshold, points in band_rows:
        if value >= threshold:
            return float(points)
    return float(floor)


def score_commercial(
    data: CommercialInput, *, bands: dict[str, Any], rates: dict[str, float]
) -> CommercialResult:
    calculation = calculate(data, bands=bands, rates=rates)

    if calculation.margin_percent is None:
        return CommercialResult(
            score=None,
            calculation=calculation,
            missing_information=calculation.missing_information,
            confidence=0.0,
            risks=["Cannot assess commercial value until the rates are recorded"],
        )

    margin_ratio = calculation.margin_percent / 100
    margin_points = _band_points(margin_ratio, bands["margin_bands"], 0)
    value_points = _band_points(
        float(calculation.contract_value or 0), bands["value_bands"], bands["value_floor_points"]
    )
    duration_points = _band_points(
        calculation.duration_months, bands["duration_bands"], bands["duration_floor_points"]
    )

    sub_scores = [
        CommercialSubScore(
            "margin",
            "Margin",
            margin_points,
            float(bands["margin_max"]),
            f"{calculation.margin_percent:.1f}% gross margin",
        ),
        CommercialSubScore(
            "contract_value",
            "Contract value",
            value_points,
            float(bands["value_max"]),
            f"{calculation.contract_value:,.0f} {calculation.currency} over the engagement"
            if calculation.contract_value
            else "Contract value unknown",
        ),
        CommercialSubScore(
            "duration",
            "Duration",
            duration_points,
            float(bands["duration_max"]),
            f"{calculation.duration_months} month engagement",
        ),
    ]

    score = min(sum(item.points for item in sub_scores), 100.0)

    result = CommercialResult(
        score=float(score),
        calculation=calculation,
        sub_scores=sub_scores,
        missing_information=calculation.missing_information,
        confidence=1.0,
    )

    if calculation.margin_percent >= 30:
        result.positives.append(f"Healthy {calculation.margin_percent:.0f}% margin")
    elif calculation.margin_percent <= 0:
        result.risks.append(f"Negative margin at current rates ({calculation.margin_percent:.0f}%)")
    elif calculation.margin_percent < 20:
        result.risks.append(f"Thin {calculation.margin_percent:.0f}% margin")

    if calculation.contract_value and calculation.contract_value >= 750_000:
        result.positives.append(
            f"Substantial contract value ({calculation.contract_value:,.0f} {calculation.currency})"
        )
    if calculation.duration_months >= 12:
        result.positives.append(f"{calculation.duration_months}-month engagement")
    elif calculation.duration_months < 3:
        result.risks.append(
            f"Short {calculation.duration_months}-month engagement — "
            "mobilisation cost is hard to recover"
        )

    if calculation.is_converted:
        result.risks.append("Figures converted from another currency — confirm before quoting")
    if calculation.one_off_total > 0:
        result.positives.append(
            f"One-off costs of {calculation.one_off_total:,.0f} amortised over "
            f"{calculation.duration_months} months"
        )

    return result


__all__ = [
    "CommercialCalculation",
    "CommercialInput",
    "CommercialResult",
    "CommercialSubScore",
    "Money",
    "calculate",
    "convert",
    "score_commercial",
    "to_monthly",
]
