"""Default rule sets for the scoring layer.

Seed defaults only. Live values come from `scoring_configurations`, so changing
a business rule is an Admin action rather than a deploy (AD-2). Nothing in an
engine may hard-code a number that appears here (SCORING.md section 7).

The numbers below are not arbitrary: they are the ones that reproduce the SOW's
worked example (94 talent / 88 addressability / 91 commercial → 91), which is
the acceptance criterion for this phase.
"""

from __future__ import annotations

from typing import Any

# ------------------------------------------------------- addressability


class Factor:
    """Keys for the eight addressability factors, so nothing is stringly-typed."""

    OUTSOURCING_FRIENDLY = "outsourcing_friendly"
    EXISTING_CUSTOMER = "existing_customer"
    PARTNER_ROUTE = "partner_route"
    APPROVED_VENDOR = "approved_vendor"
    DECISION_MAKER = "decision_maker"
    REQUIREMENT_ACTIVE = "requirement_active"
    CAN_SUBMIT = "can_submit"
    VIABLE_RATE = "viable_rate"


#: SCORING.md section 2. Points must sum to 100.
DEFAULT_ADDRESSABILITY_RULES: dict[str, Any] = {
    "factors": {
        Factor.OUTSOURCING_FRIENDLY: 10,
        Factor.EXISTING_CUSTOMER: 20,
        Factor.PARTNER_ROUTE: 15,
        Factor.APPROVED_VENDOR: 20,
        Factor.DECISION_MAKER: 10,
        Factor.REQUIREMENT_ACTIVE: 10,
        Factor.CAN_SUBMIT: 10,
        Factor.VIABLE_RATE: 5,
    },
    #: Reachability with nobody to send is still not addressable. Multiplicative
    #: rather than additive, because "we cannot supply this" must suppress the
    #: whole score rather than shave a few points off it.
    "supply_gate": [[70, 1.00], [55, 0.85], [40, 0.60]],
    "supply_gate_floor": 0.35,
    #: A rate below this is not commercially viable at any margin (QAR/month).
    "rate_floor_monthly": 8000,
    "band_highly_addressable": 80,
    "band_addressable": 60,
    "band_conditional": 40,
}

FACTOR_LABELS: dict[str, str] = {
    Factor.OUTSOURCING_FRIENDLY: "Contract / outsourcing friendly",
    Factor.EXISTING_CUSTOMER: "Existing Glimmora customer",
    Factor.PARTNER_ROUTE: "Partner / prime route available",
    Factor.APPROVED_VENDOR: "Approved vendor (MSA / vendor registration)",
    Factor.DECISION_MAKER: "Decision maker known",
    Factor.REQUIREMENT_ACTIVE: "Requirement is active",
    Factor.CAN_SUBMIT: "Glimmora can still submit",
    Factor.VIABLE_RATE: "Commercially viable rate",
}

FACTOR_ORDER: list[str] = list(DEFAULT_ADDRESSABILITY_RULES["factors"])


# ------------------------------------------------------------ commercial

#: SCORING.md section 4b. Sub-score maxima sum to 100.
DEFAULT_COMMERCIAL_BANDS: dict[str, Any] = {
    #: [threshold, points] — first band whose threshold the margin meets.
    "margin_bands": [
        [0.35, 60],
        [0.30, 52],
        [0.25, 44],
        [0.20, 34],
        [0.15, 22],
        [0.10, 12],
        [0.0001, 5],
    ],
    "margin_max": 60,
    #: Total contract value, in the base currency.
    "value_bands": [
        [1_500_000, 25],
        [750_000, 20],
        [350_000, 15],
        [120_000, 10],
    ],
    "value_floor_points": 5,
    "value_max": 25,
    "duration_bands": [[24, 15], [12, 13], [6, 10], [3, 6]],
    "duration_floor_points": 3,
    "duration_max": 15,
    # --- calculator ---------------------------------------------------
    "working_days_per_month": 22,
    "hours_per_day": 8,
    #: One-off costs (visa, insurance, mobilisation) spread across the
    #: engagement rather than charged to month one, so the margin shown is the
    #: margin of the engagement rather than of an arbitrary month.
    "amortise_one_off_costs": True,
    "default_duration_months": 12,
}

#: Cross-currency comparison. Any converted figure is flagged `is_converted` so
#: nobody mistakes an estimate for a quote (SCORING.md section 4a).
DEFAULT_CURRENCY_RATES: dict[str, float] = {
    "QAR": 1.0,
    "USD": 3.64,
    "EUR": 3.95,
    "GBP": 4.60,
    "AED": 0.99,
    "SAR": 0.97,
    "INR": 0.044,
}

BASE_CURRENCY = "QAR"


# ----------------------------------------------------------- opportunity

#: SCORING.md section 5. Weights must sum to 1.0.
DEFAULT_OPPORTUNITY_WEIGHTS: dict[str, Any] = {
    "weights": {
        "talent_match": 0.40,
        "addressability": 0.35,
        "commercial": 0.25,
    },
    "band_pursue_now": 80,
    "band_pursue": 65,
    "band_review": 50,
}

OPPORTUNITY_COMPONENT_LABELS: dict[str, str] = {
    "talent_match": "Talent match",
    "addressability": "Addressability",
    "commercial": "Commercial",
}


# ------------------------------------------------------------ validation


def validate_addressability_rules(payload: dict[str, Any]) -> None:
    factors = payload.get("factors", {})
    missing = set(DEFAULT_ADDRESSABILITY_RULES["factors"]) - set(factors)
    if missing:
        raise ValueError(f"Missing factors: {', '.join(sorted(missing))}")

    unknown = set(factors) - set(DEFAULT_ADDRESSABILITY_RULES["factors"])
    if unknown:
        raise ValueError(f"Unknown factors: {', '.join(sorted(unknown))}")

    total = sum(float(value) for value in factors.values())
    if abs(total - 100) > 0.01:
        raise ValueError(f"Factor points must sum to 100, got {total:g}")


def validate_commercial_bands(payload: dict[str, Any]) -> None:
    maxima = (
        float(payload.get("margin_max", 0))
        + float(payload.get("value_max", 0))
        + float(payload.get("duration_max", 0))
    )
    if abs(maxima - 100) > 0.01:
        raise ValueError(f"Sub-score maxima must sum to 100, got {maxima:g}")

    for key in ("margin_bands", "value_bands", "duration_bands"):
        bands = payload.get(key)
        if not bands:
            raise ValueError(f"{key} must not be empty")
        thresholds = [row[0] for row in bands]
        if thresholds != sorted(thresholds, reverse=True):
            raise ValueError(f"{key} must be ordered from highest threshold down")


def validate_opportunity_weights(payload: dict[str, Any]) -> None:
    weights = payload.get("weights", {})
    missing = set(DEFAULT_OPPORTUNITY_WEIGHTS["weights"]) - set(weights)
    if missing:
        raise ValueError(f"Missing components: {', '.join(sorted(missing))}")

    unknown = set(weights) - set(DEFAULT_OPPORTUNITY_WEIGHTS["weights"])
    if unknown:
        raise ValueError(f"Unknown components: {', '.join(sorted(unknown))}")

    total = sum(float(value) for value in weights.values())
    if abs(total - 1.0) > 0.0001:
        raise ValueError(f"Component weights must sum to 1.0, got {total:g}")


__all__ = [
    "BASE_CURRENCY",
    "DEFAULT_ADDRESSABILITY_RULES",
    "DEFAULT_COMMERCIAL_BANDS",
    "DEFAULT_CURRENCY_RATES",
    "DEFAULT_OPPORTUNITY_WEIGHTS",
    "FACTOR_LABELS",
    "FACTOR_ORDER",
    "OPPORTUNITY_COMPONENT_LABELS",
    "Factor",
    "validate_addressability_rules",
    "validate_commercial_bands",
    "validate_opportunity_weights",
]
