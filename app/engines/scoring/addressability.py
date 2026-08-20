"""Staffing Addressability: *can Glimmora actually pursue and supply this?*

Off-the-shelf staffing tools measure whether you have somebody good. This
measures whether you are allowed anywhere near the client — which is the half
that decides whether a 98% match is worth a phone call (SCORING.md section 2).

Purely rule-based. Every input is a fact a human confirmed, never an inference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.engines.scoring.config import FACTOR_LABELS, FACTOR_ORDER, Factor


class FactorState(StrEnum):
    """Why a factor scored what it did.

    The distinction between NOT_MET and UNKNOWN is the difference between a
    useful score and a misleading one: "we checked and the answer is no" is
    actionable, "nobody has filled this in" is a data-entry task. Collapsing
    them into a single zero destroys that.
    """

    MET = "MET"
    NOT_MET = "NOT_MET"
    #: Scored zero, and that is correct — e.g. no partner route is needed when
    #: the relationship is direct. Never rendered as a deficiency.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


class AddressabilityBand(StrEnum):
    HIGHLY_ADDRESSABLE = "HIGHLY_ADDRESSABLE"
    ADDRESSABLE = "ADDRESSABLE"
    CONDITIONAL = "CONDITIONAL"
    NOT_ADDRESSABLE = "NOT_ADDRESSABLE"


BAND_ACTIONS: dict[AddressabilityBand, str] = {
    AddressabilityBand.HIGHLY_ADDRESSABLE: "Route is proven — pursue immediately",
    AddressabilityBand.ADDRESSABLE: "Pursue; one or two gaps to close",
    AddressabilityBand.CONDITIONAL: "Needs a route, an approval or an introduction first",
    AddressabilityBand.NOT_ADDRESSABLE: "Do not spend sales time yet",
}


@dataclass(slots=True)
class FactorResult:
    key: str
    label: str
    state: FactorState
    points: float
    max_points: float
    evidence: str | None = None

    @property
    def is_gap(self) -> bool:
        """A closeable gap. NOT_APPLICABLE is not a gap; UNKNOWN is."""
        return self.state in {FactorState.NOT_MET, FactorState.UNKNOWN}


@dataclass(slots=True)
class AddressabilityInput:
    """Facts the score reads. All optional — absent means UNKNOWN, not False."""

    account_known: bool = False
    contract_outsourcing_friendly: bool | None = None
    is_existing_customer: bool | None = None
    is_existing_partner: bool | None = None
    has_preferred_route: bool | None = None
    is_approved_vendor: bool | None = None
    has_msa: bool | None = None
    has_decision_maker: bool | None = None
    relationship_status: str | None = None

    requirement_is_open: bool = True
    response_deadline_at: datetime | None = None
    monthly_rate: Decimal | None = None

    #: Best talent match for this requirement, 0-100, or None if never matched.
    best_talent_match: float | None = None


@dataclass(slots=True)
class AddressabilityResult:
    score: float
    raw_total: float
    supply_gate: float
    band: AddressabilityBand
    confidence: float
    factors: list[FactorResult] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    recommended_action: str = ""

    def factor(self, key: str) -> FactorResult | None:
        return next((item for item in self.factors if item.key == key), None)


def _resolve(
    key: str,
    max_points: float,
    *,
    value: bool | None,
    met_evidence: str,
    not_met_evidence: str,
    unknown_evidence: str,
) -> FactorResult:
    label = FACTOR_LABELS[key]
    if value is None:
        return FactorResult(key, label, FactorState.UNKNOWN, 0.0, max_points, unknown_evidence)
    if value:
        return FactorResult(key, label, FactorState.MET, max_points, max_points, met_evidence)
    return FactorResult(key, label, FactorState.NOT_MET, 0.0, max_points, not_met_evidence)


def supply_gate_for(best_match: float | None, rules: dict[str, Any]) -> tuple[float, str]:
    """Multiplier from the best available candidate, plus its evidence."""
    if best_match is None:
        return float(rules["supply_gate_floor"]), "No matching has been run for this requirement"

    for threshold, factor in rules["supply_gate"]:
        if best_match >= threshold:
            return float(factor), f"Best talent match {best_match:g}%"
    return float(rules["supply_gate_floor"]), f"Best talent match only {best_match:g}%"


def band_for(score: float, rules: dict[str, Any]) -> AddressabilityBand:
    if score >= rules["band_highly_addressable"]:
        return AddressabilityBand.HIGHLY_ADDRESSABLE
    if score >= rules["band_addressable"]:
        return AddressabilityBand.ADDRESSABLE
    if score >= rules["band_conditional"]:
        return AddressabilityBand.CONDITIONAL
    return AddressabilityBand.NOT_ADDRESSABLE


def score_addressability(
    data: AddressabilityInput, *, rules: dict[str, Any], now: datetime
) -> AddressabilityResult:
    points: dict[str, float] = rules["factors"]
    factors: list[FactorResult] = []

    # 1 — contract / outsourcing friendly
    factors.append(
        _resolve(
            Factor.OUTSOURCING_FRIENDLY,
            points[Factor.OUTSOURCING_FRIENDLY],
            value=data.contract_outsourcing_friendly,
            met_evidence="Account contracts are outsourcing friendly",
            not_met_evidence="Account contracting does not permit outsourced staff",
            unknown_evidence="Nobody has recorded whether this account outsources",
        )
    )

    # 2 — existing customer
    factors.append(
        _resolve(
            Factor.EXISTING_CUSTOMER,
            points[Factor.EXISTING_CUSTOMER],
            value=data.is_existing_customer,
            met_evidence="Existing Glimmora customer",
            not_met_evidence="Not yet a customer",
            unknown_evidence="Account relationship not recorded",
        )
    )

    # 3 — partner or prime route.
    #
    # Zero here is *correct* when the relationship is direct, and must read as
    # "no route needed" rather than as a deficiency (SCORING.md section 2).
    partner_max = points[Factor.PARTNER_ROUTE]
    has_route = bool(data.is_existing_partner) or bool(data.has_preferred_route)
    if has_route:
        factors.append(
            FactorResult(
                Factor.PARTNER_ROUTE,
                FACTOR_LABELS[Factor.PARTNER_ROUTE],
                FactorState.MET,
                partner_max,
                partner_max,
                "A partner or prime route into this account is recorded",
            )
        )
    elif data.is_existing_customer:
        factors.append(
            FactorResult(
                Factor.PARTNER_ROUTE,
                FACTOR_LABELS[Factor.PARTNER_ROUTE],
                FactorState.NOT_APPLICABLE,
                0.0,
                partner_max,
                "Direct relationship — no partner route required",
            )
        )
    elif data.is_existing_partner is None and data.has_preferred_route is None:
        factors.append(
            FactorResult(
                Factor.PARTNER_ROUTE,
                FACTOR_LABELS[Factor.PARTNER_ROUTE],
                FactorState.UNKNOWN,
                0.0,
                partner_max,
                "No routing recorded for this account",
            )
        )
    else:
        factors.append(
            FactorResult(
                Factor.PARTNER_ROUTE,
                FACTOR_LABELS[Factor.PARTNER_ROUTE],
                FactorState.NOT_MET,
                0.0,
                partner_max,
                "No partner or prime route into this account",
            )
        )

    # 4 — approved vendor. Two separate facts: we may know a partner well and
    # still not be on their approved vendor list.
    vendor_max = points[Factor.APPROVED_VENDOR]
    if data.is_approved_vendor is None and data.has_msa is None:
        factors.append(
            FactorResult(
                Factor.APPROVED_VENDOR,
                FACTOR_LABELS[Factor.APPROVED_VENDOR],
                FactorState.UNKNOWN,
                0.0,
                vendor_max,
                "Vendor registration status not recorded",
            )
        )
    elif data.is_approved_vendor or data.has_msa:
        evidence = "MSA in place" if data.has_msa else "Registered as an approved vendor"
        factors.append(
            FactorResult(
                Factor.APPROVED_VENDOR,
                FACTOR_LABELS[Factor.APPROVED_VENDOR],
                FactorState.MET,
                vendor_max,
                vendor_max,
                evidence,
            )
        )
    else:
        factors.append(
            FactorResult(
                Factor.APPROVED_VENDOR,
                FACTOR_LABELS[Factor.APPROVED_VENDOR],
                FactorState.NOT_MET,
                0.0,
                vendor_max,
                "Not an approved vendor and no MSA — procurement will block a submission",
            )
        )

    # 5 — decision maker
    factors.append(
        _resolve(
            Factor.DECISION_MAKER,
            points[Factor.DECISION_MAKER],
            value=data.has_decision_maker,
            met_evidence="A decision maker is recorded on this account",
            not_met_evidence="No decision maker identified — submissions may stall",
            unknown_evidence="No contacts recorded for this account",
        )
    )

    # 6 — requirement still live
    active_max = points[Factor.REQUIREMENT_ACTIVE]
    factors.append(
        FactorResult(
            Factor.REQUIREMENT_ACTIVE,
            FACTOR_LABELS[Factor.REQUIREMENT_ACTIVE],
            FactorState.MET if data.requirement_is_open else FactorState.NOT_MET,
            active_max if data.requirement_is_open else 0.0,
            active_max,
            "Requirement is open" if data.requirement_is_open else "Requirement is closed",
        )
    )

    # 7 — submission window. A null deadline is not a missed one: plenty of
    # requirements legitimately have no SLA (ASSUMPTIONS.md A12).
    submit_max = points[Factor.CAN_SUBMIT]
    if data.response_deadline_at is None:
        factors.append(
            FactorResult(
                Factor.CAN_SUBMIT,
                FACTOR_LABELS[Factor.CAN_SUBMIT],
                FactorState.MET,
                submit_max,
                submit_max,
                "No submission deadline set — the window is open",
            )
        )
    elif data.response_deadline_at > now:
        factors.append(
            FactorResult(
                Factor.CAN_SUBMIT,
                FACTOR_LABELS[Factor.CAN_SUBMIT],
                FactorState.MET,
                submit_max,
                submit_max,
                f"Submission window open until {data.response_deadline_at:%d %b %Y %H:%M}",
            )
        )
    else:
        factors.append(
            FactorResult(
                Factor.CAN_SUBMIT,
                FACTOR_LABELS[Factor.CAN_SUBMIT],
                FactorState.NOT_MET,
                0.0,
                submit_max,
                f"Submission deadline passed on {data.response_deadline_at:%d %b %Y %H:%M}",
            )
        )

    # 8 — viable rate
    rate_max = points[Factor.VIABLE_RATE]
    floor = Decimal(str(rules["rate_floor_monthly"]))
    if data.monthly_rate is None:
        factors.append(
            FactorResult(
                Factor.VIABLE_RATE,
                FACTOR_LABELS[Factor.VIABLE_RATE],
                FactorState.UNKNOWN,
                0.0,
                rate_max,
                "Client rate not recorded",
            )
        )
    elif data.monthly_rate >= floor:
        factors.append(
            FactorResult(
                Factor.VIABLE_RATE,
                FACTOR_LABELS[Factor.VIABLE_RATE],
                FactorState.MET,
                rate_max,
                rate_max,
                f"Rate of {data.monthly_rate:,.0f}/month is above the viability floor",
            )
        )
    else:
        factors.append(
            FactorResult(
                Factor.VIABLE_RATE,
                FACTOR_LABELS[Factor.VIABLE_RATE],
                FactorState.NOT_MET,
                0.0,
                rate_max,
                f"Rate of {data.monthly_rate:,.0f}/month is below the {floor:,.0f} floor",
            )
        )

    factors.sort(key=lambda item: FACTOR_ORDER.index(item.key))

    raw_total = sum(item.points for item in factors)
    gate, gate_evidence = supply_gate_for(data.best_talent_match, rules)
    score = max(0.0, min(round(raw_total * gate), 100))

    # Confidence is the share of weight actually answered. NOT_APPLICABLE counts
    # as answered — "no route needed" is knowledge, not a blank.
    answered = sum(item.max_points for item in factors if item.state is not FactorState.UNKNOWN)
    total_weight = sum(item.max_points for item in factors)
    confidence = round(answered / total_weight, 3) if total_weight else 0.0

    band = band_for(score, rules)

    result = AddressabilityResult(
        score=float(score),
        raw_total=float(raw_total),
        supply_gate=gate,
        band=band,
        confidence=confidence,
        factors=factors,
        recommended_action=BAND_ACTIONS[band],
    )

    result.positives = [
        f"{item.label}: {item.evidence}" if item.evidence else item.label
        for item in factors
        if item.state is FactorState.MET
    ]
    result.risks = [
        item.evidence or item.label for item in factors if item.state is FactorState.NOT_MET
    ]
    result.missing_information = [
        item.label for item in factors if item.state is FactorState.UNKNOWN
    ]

    if gate < 1.0:
        result.risks.append(f"Supply gate applied ({gate:g}x) — {gate_evidence.lower()}")

    return result


__all__ = [
    "BAND_ACTIONS",
    "AddressabilityBand",
    "AddressabilityInput",
    "AddressabilityResult",
    "FactorResult",
    "FactorState",
    "band_for",
    "score_addressability",
    "supply_gate_for",
]
