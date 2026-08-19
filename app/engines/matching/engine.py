"""The matching engine: hard filters, component scoring, explanation.

The rule that shapes every line below: **the final number is never
LLM-generated**. The engine computes a score from rules and configurable
weights; a narrative may later be written *about* that score, but it can never
change it (AD-1).

A component whose inputs are unknown is excluded from both the numerator and
the denominator, and reported under `missing_information`. Scoring it as zero
would make an unpriced requirement look actively bad rather than unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from app.engines.matching.config import WARNING_CODES
from app.engines.matching.scorers import (
    ComponentScore,
    margin_for,
    score_availability,
    score_commercial,
    score_cost,
    score_experience,
    score_location,
    score_skills,
    score_technology,
)
from app.models.matching import MatchBand

ENGINE_VERSION = "1.0.0"

#: Conditions that cap the band however well the arithmetic scores.
#:
#: A consultant with an expired work permit cannot legally be deployed, so
#: presenting them as a "strong match" is worse than useless — a recruiter
#: reading a ranked list acts on the band before the warnings. The numeric score
#: is left untouched and explainable; only the headline verdict is capped
#: (SCORING.md section 5).
SUPPRESSORS: dict[str, tuple[str, MatchBand]] = {
    "WORK_AUTH_EXPIRED": (
        "Work authorisation has expired — cannot be deployed until renewed",
        MatchBand.POSSIBLE,
    ),
    "NEGATIVE_MARGIN": (
        "Cost exceeds the client rate — the placement would lose money",
        MatchBand.POSSIBLE,
    ),
    "MISSING_MANDATORY_SKILL": (
        "Missing a mandatory skill",
        MatchBand.GOOD,
    ),
}

#: A suppressor and a warning may describe the same fact. Listing both reads as
#: two separate problems, so the suppressor wins and the warning is dropped.
SUPPRESSOR_RESTATES: dict[str, str] = {
    "WORK_AUTH_EXPIRED": "WORK_AUTH_EXPIRED",
    "NEGATIVE_MARGIN": "COST_ABOVE_RATE",
    # MISSING_MANDATORY_SKILL has no twin: the skills themselves are named
    # under `gaps`, which is a list, not a warning.
}

_BAND_ORDER = [MatchBand.WEAK, MatchBand.POSSIBLE, MatchBand.GOOD, MatchBand.STRONG]


def suppressor_reasons(codes: list[str]) -> list[str]:
    """Human text for each applied suppressor, in the order they applied."""
    return [SUPPRESSORS[code][0] for code in codes if code in SUPPRESSORS]


def apply_suppressors(
    band: MatchBand, *, gaps: list[str], work_authorisation_state: str, margin: float | None
) -> tuple[MatchBand, list[str]]:
    """Cap the band on any hard blocker, and return the codes that applied."""
    capped = band

    triggered: list[str] = []
    if work_authorisation_state == "EXPIRED":
        triggered.append("WORK_AUTH_EXPIRED")
    if margin is not None and margin <= 0:
        triggered.append("NEGATIVE_MARGIN")
    if gaps:
        triggered.append("MISSING_MANDATORY_SKILL")

    for code in triggered:
        ceiling = SUPPRESSORS[code][1]
        if _BAND_ORDER.index(capped) > _BAND_ORDER.index(ceiling):
            capped = ceiling

    return capped, triggered


#: Normalise every rate onto a monthly basis before comparing cost with price.
_UNIT_TO_MONTHLY = {
    "HOURLY": lambda value, days, hours: value * days * hours,
    "DAILY": lambda value, days, hours: value * days,
    "MONTHLY": lambda value, days, hours: value,
    "ANNUAL": lambda value, days, hours: value / 12,
}


def to_monthly(
    amount: Decimal | None, unit: str | None, *, working_days: int = 22, hours_per_day: int = 8
) -> Decimal | None:
    if amount is None or not unit:
        return None
    converter = _UNIT_TO_MONTHLY.get(str(unit).upper())
    if converter is None:
        return None
    return Decimal(str(converter(float(amount), working_days, hours_per_day))).quantize(
        Decimal("0.01")
    )


@dataclass(slots=True)
class RequirementView:
    """Everything the engine needs from a requirement, already loaded."""

    id: Any
    title: str
    mandatory_skills: list[str]
    preferred_skills: list[str]
    required_years: dict[str, int | None]
    technologies: set[str]
    experience_min_years: int | None
    country: str | None
    location: str | None
    work_mode: str | None
    start_by_date: date | None
    rate_max: Decimal | None
    rate_unit: str | None
    positions: int = 1


@dataclass(slots=True)
class ResourceView:
    """Everything the engine needs from a resource, already loaded."""

    id: Any
    full_name: str
    skills: dict[str, float | None]
    skill_last_used: dict[str, int | None]
    primary_technologies: set[str]
    technologies: set[str]
    total_experience_years: float | None
    country: str | None
    city: str | None
    willing_to_relocate: bool
    ready_from: date | None
    notice_period_days: int
    available_from: date | None
    expected_cost: Decimal | None
    expected_cost_unit: str | None
    work_authorisation_state: str
    work_authorisation_days: int | None
    needs_review: bool
    availability_status: str


@dataclass(slots=True)
class MatchResult:
    resource_id: Any
    requirement_id: Any
    overall_score: float
    band: MatchBand
    confidence: float
    components: list[ComponentScore]
    gaps: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    suppressors: list[str] = field(default_factory=list)
    semantic_score: float | None = None
    narrative: str | None = None

    def component(self, key: str) -> ComponentScore | None:
        return next((item for item in self.components if item.key == key), None)


# ------------------------------------------------------------- hard filters


@dataclass(slots=True)
class FilterOutcome:
    included: bool
    reason: str | None = None


def apply_hard_filters(
    requirement: RequirementView, resource: ResourceView, thresholds: dict[str, Any]
) -> FilterOutcome:
    """Remove candidates rather than penalise them — sparingly.

    The default posture is permissive filtering and honest scoring: a recruiter
    would rather see a 58% with a named gap than an empty result list
    (MATCHING.md section 1).
    """
    if (
        thresholds.get("exclude_unavailable", True)
        and resource.availability_status == "NOT_AVAILABLE"
    ):
        return FilterOutcome(False, "Marked not available")

    if thresholds.get("exclude_expired_work_authorisation") and (
        resource.work_authorisation_state == "EXPIRED"
    ):
        return FilterOutcome(False, "Work authorisation has expired")

    if thresholds.get("require_all_mandatory_skills"):
        missing = [name for name in requirement.mandatory_skills if name not in resource.skills]
        if missing:
            return FilterOutcome(False, f"Missing mandatory skills: {', '.join(missing)}")

    if requirement.experience_min_years and resource.total_experience_years is not None:
        grace = thresholds.get("experience_grace_years", 1)
        if resource.total_experience_years < requirement.experience_min_years - grace:
            return FilterOutcome(
                False,
                f"{resource.total_experience_years:g} years against "
                f"{requirement.experience_min_years} required",
            )

    return FilterOutcome(True)


# ------------------------------------------------------------------- engine


def score_match(
    requirement: RequirementView,
    resource: ResourceView,
    *,
    weights: dict[str, float],
    thresholds: dict[str, Any],
    semantic: float | None = None,
    today: date | None = None,
) -> MatchResult:
    """Score one requirement-resource pairing, with its full explanation."""
    reference = today or date.today()

    skills, gaps = score_skills(
        mandatory=requirement.mandatory_skills,
        preferred=requirement.preferred_skills,
        resource_skills=resource.skills,
        required_years=requirement.required_years,
        last_used=resource.skill_last_used,
        semantic=semantic,
        thresholds=thresholds,
        today=reference,
    )

    monthly_cost = to_monthly(resource.expected_cost, resource.expected_cost_unit)
    monthly_rate = to_monthly(requirement.rate_max, requirement.rate_unit)
    margin = margin_for(cost=monthly_cost, rate=monthly_rate)

    components = [
        skills,
        score_experience(
            required_min=requirement.experience_min_years,
            resource_years=resource.total_experience_years,
            thresholds=thresholds,
        ),
        score_technology(
            required=requirement.technologies,
            resource=resource.technologies,
            primary=resource.primary_technologies,
        ),
        score_availability(
            ready_from=resource.ready_from,
            needed_by=requirement.start_by_date,
            thresholds=thresholds,
            today=reference,
        ),
        score_location(
            requirement_country=requirement.country,
            requirement_city=requirement.location,
            resource_country=resource.country,
            resource_city=resource.city,
            work_mode=requirement.work_mode,
            willing_to_relocate=resource.willing_to_relocate,
            has_work_authorisation=resource.work_authorisation_state in {"VALID", "EXPIRING_SOON"},
            thresholds=thresholds,
        ),
        score_cost(margin=margin, thresholds=thresholds),
        score_commercial(margin=margin, thresholds=thresholds),
    ]

    for component in components:
        component.weight = float(weights.get(component.key, 0))

    known = [component for component in components if component.is_known and component.weight > 0]
    known_weight = sum(component.weight for component in known)

    if known_weight == 0:
        overall = 0.0
        confidence = 0.0
    else:
        # Unknown components leave both numerator and denominator, so the score
        # reflects what is actually known rather than punishing missing data.
        overall = sum(c.score * c.weight for c in known if c.score is not None) / known_weight
        confidence = round(known_weight / sum(weights.values()), 3)

    missing = [
        component.label
        for component in components
        if not component.is_known and component.weight > 0
    ]

    band, suppressors = apply_suppressors(
        band_for(overall, thresholds),
        gaps=gaps,
        work_authorisation_state=resource.work_authorisation_state,
        margin=margin,
    )

    result = MatchResult(
        resource_id=resource.id,
        requirement_id=requirement.id,
        overall_score=round(overall, 1),
        band=band,
        confidence=confidence,
        suppressors=suppressors,
        components=components,
        gaps=gaps,
        missing_information=missing,
        semantic_score=round(semantic * 100, 1) if semantic is not None else None,
    )
    result.reasons = build_reasons(requirement, resource, result)
    result.warnings = build_warnings(requirement, resource, result, margin, thresholds, reference)
    result.narrative = build_narrative(resource, result)
    return result


def band_for(score: float, thresholds: dict[str, Any]) -> MatchBand:
    if score >= thresholds["band_strong"]:
        return MatchBand.STRONG
    if score >= thresholds["band_good"]:
        return MatchBand.GOOD
    if score >= thresholds["band_possible"]:
        return MatchBand.POSSIBLE
    return MatchBand.WEAK


def build_reasons(
    requirement: RequirementView, resource: ResourceView, result: MatchResult
) -> list[str]:
    """The top positive contributors, phrased as evidence rather than adjectives."""
    reasons: list[str] = []

    strong = sorted(
        (c for c in result.components if c.is_known and c.score is not None and c.score >= 75),
        key=lambda c: (c.score or 0) * c.weight,
        reverse=True,
    )
    for component in strong[:4]:
        reasons.append(f"{component.label}: {component.evidence or f'{component.score:g}%'}")

    if (
        resource.total_experience_years
        and requirement.experience_min_years
        and resource.total_experience_years >= requirement.experience_min_years
    ):
        reasons.append(f"Meets the {requirement.experience_min_years}-year experience bar")

    if resource.availability_status == "AVAILABLE":
        reasons.append("Available now")

    return reasons[:6]


def build_warnings(
    requirement: RequirementView,
    resource: ResourceView,
    result: MatchResult,
    margin: float | None,
    thresholds: dict[str, Any],
    today: date,
) -> list[str]:
    """Everything a recruiter must know before putting this person forward.

    Suppressor text leads the list; a warning that only restates a suppressor is
    dropped, because two phrasings of one problem read as two problems.
    """
    restated = {
        SUPPRESSOR_RESTATES[code] for code in result.suppressors if code in SUPPRESSOR_RESTATES
    }
    warnings: list[str] = suppressor_reasons(result.suppressors)

    if resource.work_authorisation_state == "EXPIRED":
        if "WORK_AUTH_EXPIRED" not in restated:
            warnings.append(WARNING_CODES["WORK_AUTH_EXPIRED"])
    elif (
        resource.work_authorisation_days is not None and 0 <= resource.work_authorisation_days <= 90
    ):
        warnings.append(WARNING_CODES["WORK_AUTH_EXPIRING"])

    # Use the same readiness date the availability component scored, so the
    # warning can never contradict the number sitting next to it.
    if requirement.start_by_date:
        earliest = resource.ready_from
        if earliest is None and resource.notice_period_days:
            earliest = today + timedelta(days=resource.notice_period_days)
        if earliest is not None and earliest > requirement.start_by_date:
            late_by = (earliest - requirement.start_by_date).days
            warnings.append(
                f"{WARNING_CODES['NOTICE_AFTER_START']} "
                f"(free from {earliest:%d %b %Y}, {late_by} days late)"
            )

    if margin is not None:
        if margin <= 0:
            if "COST_ABOVE_RATE" not in restated:
                warnings.append(WARNING_CODES["COST_ABOVE_RATE"])
        elif margin < thresholds["cost_target_margin"]:
            warnings.append(f"{WARNING_CODES['THIN_MARGIN']} ({margin * 100:.0f}%)")

    if resource.needs_review:
        warnings.append(WARNING_CODES["UNREVIEWED_PROFILE"])

    if not resource.skills:
        warnings.append(WARNING_CODES["NO_SKILLS_RECORDED"])

    location = result.component("location")
    if location and location.evidence and "needs a work permit" in location.evidence:
        warnings.append(WARNING_CODES["RELOCATION_REQUIRED"])

    return warnings


def build_narrative(resource: ResourceView, result: MatchResult) -> str:
    """A deterministic summary.

    An LLM may replace this later with better prose, but it can only describe
    numbers the engine already produced — and this template is what the UI falls
    back to whenever the model is unavailable (AI_ARCHITECTURE.md section 6).
    """
    verdict = {
        MatchBand.STRONG: "a strong match",
        MatchBand.GOOD: "a good match",
        MatchBand.POSSIBLE: "a possible match",
        MatchBand.WEAK: "a weak match",
    }[result.band]

    sentences = [f"{resource.full_name} is {verdict} at {result.overall_score:g}%."]

    capped = suppressor_reasons(result.suppressors)
    if capped:
        sentences.append(f"Capped because: {capped[0].lower()}.")

    if result.gaps:
        sentences.append(
            f"Missing {len(result.gaps)} mandatory skill"
            f"{'' if len(result.gaps) == 1 else 's'}: {', '.join(result.gaps[:3])}."
        )

    # `warnings` already leads with the suppressor text, so skip what was said.
    unsaid = [warning for warning in result.warnings if warning not in capped]
    if unsaid:
        sentences.append(unsaid[0] + ".")
    if result.missing_information:
        sentences.append(
            "Scored on partial information — "
            + ", ".join(item.lower() for item in result.missing_information[:3])
            + " not recorded."
        )

    return " ".join(sentences)


__all__ = [
    "ENGINE_VERSION",
    "SUPPRESSORS",
    "SUPPRESSOR_RESTATES",
    "FilterOutcome",
    "MatchResult",
    "RequirementView",
    "ResourceView",
    "apply_hard_filters",
    "apply_suppressors",
    "band_for",
    "build_narrative",
    "build_reasons",
    "build_warnings",
    "score_match",
    "suppressor_reasons",
    "to_monthly",
]
