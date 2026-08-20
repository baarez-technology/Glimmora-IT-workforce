"""Default matching weights and thresholds.

These are **seed defaults only**. Live values come from the active
`MATCH_WEIGHTS` scoring configuration, so a business rule change is an Admin
action rather than a deploy (AD-2). Nothing in the engine may hard-code a
number that appears here.
"""

from __future__ import annotations

from typing import Any

#: MATCHING.md section 3 / SCORING.md section 3. Must sum to 100.
DEFAULT_MATCH_WEIGHTS: dict[str, int] = {
    "skills": 30,
    "experience": 20,
    "technology": 15,
    "availability": 10,
    "location": 10,
    "cost": 10,
    "commercial": 5,
}

DEFAULT_THRESHOLDS: dict[str, Any] = {
    # Skills
    "mandatory_weight": 0.70,
    "preferred_weight": 0.20,
    "depth_weight": 0.10,
    "recency_recent_years": 2,
    "recency_recent_factor": 1.0,
    "recency_mid_years": 4,
    "recency_mid_factor": 0.9,
    "recency_stale_factor": 0.75,
    #: A semantic hit can rescue a candidate whose skills are tagged
    #: differently, but must not outrank someone who literally holds them.
    "semantic_floor_factor": 0.55,
    # Experience
    "over_qualification_ratio": 2.0,
    "over_qualification_penalty": 0.15,
    "experience_grace_years": 1,
    # Availability: days of gap -> score
    "availability_bands": [[0, 100], [7, 85], [15, 70], [30, 50], [60, 25]],
    "availability_floor": 5,
    # Location
    "location_same_city": 100,
    "location_same_country": 90,
    "location_remote_role": 90,
    "location_relocatable_with_permit": 75,
    "location_relocatable_needs_permit": 55,
    "location_mismatch": 20,
    # Cost: margin achieved against the requirement's rate ceiling
    "cost_target_margin": 0.30,
    "cost_bands": [[0.30, 100], [0.25, 85], [0.20, 70], [0.15, 50], [0.10, 30], [0.0, 10]],
    # Commercial fit reuses the cost margin, banded more coarsely
    "commercial_bands": [[0.30, 100], [0.20, 75], [0.10, 45], [0.0, 15]],
    # Match bands
    "band_strong": 80,
    "band_good": 65,
    "band_possible": 50,
    # Hard filters
    "require_all_mandatory_skills": False,
    "exclude_unavailable": True,
    "exclude_expired_work_authorisation": False,
    # Recall
    "max_candidates": 200,
    # --- reverse matching (Phase 8) ------------------------------------
    #: Reachability multiplies the match score to give redeployment priority.
    #: It describes whether the *seat* is winnable, not whether the person
    #: fits, which is why it is a multiplier and not an eighth component.
    #:
    #: Phase 9 replaces these with the full eight-factor addressability score
    #: and its supply gate; the shape of the calculation does not change.
    "reachability_direct_msa": 1.00,
    "reachability_direct_customer": 0.95,
    "reachability_approved_vendor": 0.90,
    "reachability_preferred_route": 0.85,
    "reachability_known_route": 0.70,
    "reachability_no_route": 0.45,
    "reachability_blocked": 0.05,
    #: A blocked or routeless account still appears, greyed out, unless this is
    #: turned on. Sales would rather see it and open the route than never know
    #: the seat existed.
    "exclude_unreachable_accounts": False,
    "reverse_match_limit": 10,
    #: Days before availability at which the bench sweep raises an alert.
    "bench_milestones": [90, 60, 30, 15, 7],
    #: Below this priority a suggestion is not worth alerting anyone about.
    "bench_alert_min_priority": 45,
}

#: Warnings a recruiter must see before putting somebody forward.
WARNING_CODES = {
    "NOTICE_AFTER_START": "Notice period runs past the requested start date",
    "WORK_AUTH_EXPIRED": "Work authorisation has expired — cannot be deployed",
    "WORK_AUTH_EXPIRING": "Work authorisation expires within 90 days",
    "COST_ABOVE_RATE": "Expected cost is above the client rate",
    "THIN_MARGIN": "Margin is below the target at this rate",
    "UNREVIEWED_PROFILE": "Profile came from a parsed CV and has not been reviewed",
    "NO_SKILLS_RECORDED": "No skills recorded against this resource",
    "RELOCATION_REQUIRED": "Requires relocation and a new work permit",
}


def default_payload() -> dict[str, Any]:
    return {"weights": dict(DEFAULT_MATCH_WEIGHTS), "thresholds": dict(DEFAULT_THRESHOLDS)}


def validate_weights(weights: dict[str, Any]) -> None:
    """Weights must cover every component and sum to 100."""
    missing = set(DEFAULT_MATCH_WEIGHTS) - set(weights)
    if missing:
        raise ValueError(f"Missing weights for: {', '.join(sorted(missing))}")

    unknown = set(weights) - set(DEFAULT_MATCH_WEIGHTS)
    if unknown:
        raise ValueError(f"Unknown weights: {', '.join(sorted(unknown))}")

    total = sum(float(value) for value in weights.values())
    if abs(total - 100) > 0.01:
        raise ValueError(f"Weights must sum to 100, got {total:g}")


__all__ = [
    "DEFAULT_MATCH_WEIGHTS",
    "DEFAULT_THRESHOLDS",
    "WARNING_CODES",
    "default_payload",
    "validate_weights",
]
