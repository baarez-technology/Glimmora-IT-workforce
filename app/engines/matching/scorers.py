"""The seven component scorers (MATCHING.md section 3).

Every scorer returns 0–100 plus its own evidence, and returns `None` when the
inputs are unknown rather than scoring zero. That distinction is the whole
point: a component nobody has filled in must not look like a component that
was checked and failed (SCORING.md section 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

# --------------------------------------------------------------------- types


@dataclass(slots=True)
class ComponentScore:
    """One component of a match."""

    key: str
    label: str
    score: float | None
    weight: float
    evidence: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_known(self) -> bool:
        return self.score is not None

    @property
    def contribution(self) -> float:
        return 0.0 if self.score is None else round(self.score * self.weight / 100, 2)


def _band(value: float, bands: list[list[float]], floor: float) -> float:
    """First band whose threshold the value meets, else the floor."""
    for threshold, score in bands:
        if value <= threshold:
            return float(score)
    return float(floor)


def _descending_band(value: float, bands: list[list[float]], floor: float) -> float:
    for threshold, score in bands:
        if value >= threshold:
            return float(score)
    return float(floor)


# -------------------------------------------------------------------- skills


def score_skills(
    *,
    mandatory: list[str],
    preferred: list[str],
    resource_skills: dict[str, float | None],
    required_years: dict[str, int | None],
    last_used: dict[str, int | None],
    semantic: float | None,
    thresholds: dict[str, Any],
    today: date,
) -> tuple[ComponentScore, list[str]]:
    """Return (score, gaps).

    Gaps name every mandatory skill the resource lacks — the single most useful
    thing a recruiter can be told about a near-miss.
    """
    held = set(resource_skills)
    gaps = [name for name in mandatory if name not in held]

    if not mandatory and not preferred:
        return (
            ComponentScore("skills", "Skills", None, 0, "No skills recorded on the requirement"),
            gaps,
        )

    mandatory_ratio = (
        len([name for name in mandatory if name in held]) / len(mandatory) if mandatory else 1.0
    )
    preferred_ratio = (
        len([name for name in preferred if name in held]) / len(preferred) if preferred else 1.0
    )

    # Depth: how far the resource's years cover what the requirement asked for.
    depth_values: list[float] = []
    for name in mandatory:
        if name not in held:
            continue
        needed = required_years.get(name)
        have = resource_skills.get(name)
        if not needed or have is None:
            continue
        depth_values.append(min(have / needed, 1.25))
    depth = min(sum(depth_values) / len(depth_values), 1.0) if depth_values else 1.0

    raw = 100 * (
        thresholds["mandatory_weight"] * mandatory_ratio
        + thresholds["preferred_weight"] * preferred_ratio
        + thresholds["depth_weight"] * depth
    )

    # Recency: a skill last used six years ago is weaker than a current one.
    years_since: list[int] = []
    for name in mandatory:
        year = last_used.get(name)
        if year:
            years_since.append(today.year - year)
    if years_since:
        worst = max(years_since)
        if worst <= thresholds["recency_recent_years"]:
            recency = thresholds["recency_recent_factor"]
        elif worst <= thresholds["recency_mid_years"]:
            recency = thresholds["recency_mid_factor"]
        else:
            recency = thresholds["recency_stale_factor"]
    else:
        recency = 1.0

    score = raw * recency

    # A semantic hit can rescue differently-tagged skills, but capped so it
    # never outranks somebody who literally holds them.
    if semantic is not None:
        floor = thresholds["semantic_floor_factor"] * semantic * 100
        score = max(score, floor)

    matched = len(mandatory) - len(gaps)
    evidence = (
        f"{matched}/{len(mandatory)} mandatory"
        + (
            f", {len([n for n in preferred if n in held])}/{len(preferred)} preferred"
            if preferred
            else ""
        )
        + (f", recency x{recency:g}" if recency < 1 else "")
    )

    return (
        ComponentScore(
            "skills",
            "Skills",
            round(min(score, 100), 1),
            0,
            evidence,
            {
                "mandatory_matched": matched,
                "mandatory_total": len(mandatory),
                "preferred_matched": len([n for n in preferred if n in held]),
                "preferred_total": len(preferred),
                "recency_factor": recency,
                "depth_factor": round(depth, 2),
            },
        ),
        gaps,
    )


# ---------------------------------------------------------------- experience


def score_experience(
    *,
    required_min: int | None,
    resource_years: float | None,
    thresholds: dict[str, Any],
) -> ComponentScore:
    if resource_years is None:
        return ComponentScore(
            "experience", "Experience", None, 0, "Resource experience not recorded"
        )
    if not required_min:
        return ComponentScore(
            "experience", "Experience", None, 0, "Requirement states no minimum experience"
        )

    ratio = resource_years / required_min
    score = min(ratio, 1.0) * 100

    # An architect on a junior seat is a commercial mismatch and usually
    # declines the rate, so heavy over-qualification is tapered.
    if ratio > thresholds["over_qualification_ratio"]:
        excess = ratio - thresholds["over_qualification_ratio"]
        score *= max(1 - thresholds["over_qualification_penalty"] * min(excess, 2), 0.6)

    return ComponentScore(
        "experience",
        "Experience",
        round(score, 1),
        0,
        f"{resource_years:g} years against {required_min} required",
        {"ratio": round(ratio, 2)},
    )


# ---------------------------------------------------------------- technology


def score_technology(
    *, required: set[str], resource: set[str], primary: set[str]
) -> ComponentScore:
    if not required:
        return ComponentScore(
            "technology", "Technology", None, 0, "No technology stack on the requirement"
        )
    if not resource:
        return ComponentScore("technology", "Technology", 0.0, 0, "No technologies recorded")

    overlap = required & resource
    base = len(overlap) / len(required) * 100
    # Working in a technology as your primary discipline counts for more.
    bonus = 10 if overlap & primary else 0

    return ComponentScore(
        "technology",
        "Technology",
        round(min(base + bonus, 100), 1),
        0,
        f"{len(overlap)}/{len(required)} matched" + (" (primary)" if bonus else ""),
        {"matched": sorted(overlap), "missing": sorted(required - resource)},
    )


# -------------------------------------------------------------- availability


def score_availability(
    *, ready_from: date | None, needed_by: date | None, thresholds: dict[str, Any], today: date
) -> ComponentScore:
    if ready_from is None:
        return ComponentScore(
            "availability", "Availability", None, 0, "Resource availability not recorded"
        )

    target = needed_by or today
    gap_days = max((ready_from - target).days, 0)
    score = _band(gap_days, thresholds["availability_bands"], thresholds["availability_floor"])

    if gap_days == 0:
        evidence = "Available on or before the start date"
    else:
        evidence = f"{gap_days} days after the requested start"

    return ComponentScore(
        "availability", "Availability", float(score), 0, evidence, {"gap_days": gap_days}
    )


# ------------------------------------------------------------------ location


def score_location(
    *,
    requirement_country: str | None,
    requirement_city: str | None,
    resource_country: str | None,
    resource_city: str | None,
    work_mode: str | None,
    willing_to_relocate: bool,
    has_work_authorisation: bool,
    thresholds: dict[str, Any],
) -> ComponentScore:
    if work_mode == "REMOTE":
        return ComponentScore(
            "location",
            "Location",
            float(thresholds["location_remote_role"]),
            0,
            "Remote role — location is not a constraint",
        )
    if not requirement_country:
        return ComponentScore("location", "Location", None, 0, "Requirement location not recorded")
    if not resource_country:
        return ComponentScore("location", "Location", None, 0, "Resource location not recorded")

    same_country = requirement_country.upper() == resource_country.upper()
    same_city = bool(
        same_country
        and requirement_city
        and resource_city
        and resource_city.lower() in requirement_city.lower()
    )

    if same_city:
        return ComponentScore(
            "location",
            "Location",
            float(thresholds["location_same_city"]),
            0,
            "Already in the city",
        )
    if same_country:
        return ComponentScore(
            "location",
            "Location",
            float(thresholds["location_same_country"]),
            0,
            "Already in the country",
        )
    if willing_to_relocate:
        score = (
            thresholds["location_relocatable_with_permit"]
            if has_work_authorisation
            else thresholds["location_relocatable_needs_permit"]
        )
        return ComponentScore(
            "location",
            "Location",
            float(score),
            0,
            "Willing to relocate"
            + (" with valid authorisation" if has_work_authorisation else " — needs a work permit"),
        )

    return ComponentScore(
        "location",
        "Location",
        float(thresholds["location_mismatch"]),
        0,
        f"In {resource_country}, requirement is in {requirement_country}",
    )


# ---------------------------------------------------------------------- cost


def margin_for(*, cost: Decimal | None, rate: Decimal | None) -> float | None:
    """Gross margin at the client rate, or None when either side is unknown."""
    if cost is None or rate is None or rate <= 0:
        return None
    return float((rate - cost) / rate)


def score_cost(*, margin: float | None, thresholds: dict[str, Any]) -> ComponentScore:
    if margin is None:
        return ComponentScore("cost", "Cost fit", None, 0, "Cost or client rate not recorded")

    score = _descending_band(margin, thresholds["cost_bands"], 0)
    return ComponentScore(
        "cost",
        "Cost fit",
        float(score),
        0,
        f"{margin * 100:.0f}% margin at the client rate",
        {"margin": round(margin, 4)},
    )


def score_commercial(*, margin: float | None, thresholds: dict[str, Any]) -> ComponentScore:
    if margin is None:
        return ComponentScore(
            "commercial", "Commercial fit", None, 0, "Commercial inputs not recorded"
        )

    score = _descending_band(margin, thresholds["commercial_bands"], 0)
    return ComponentScore(
        "commercial", "Commercial fit", float(score), 0, None, {"margin": round(margin, 4)}
    )


__all__ = [
    "ComponentScore",
    "margin_for",
    "score_availability",
    "score_commercial",
    "score_cost",
    "score_experience",
    "score_location",
    "score_skills",
    "score_technology",
]
