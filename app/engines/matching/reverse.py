"""Reverse matching: Resource → Demand.

The forward engine answers "who can fill this seat?". This one answers the
question that actually protects revenue: **"where does this person go next?"** —
asked before the current assignment ends, not after the bench starts costing
money (MATCHING.md section 2).

Three things make it different from the forward direction:

1. **Requirement-side filters apply.** A seat Sales cannot reach is not a
   suggestion, it is noise.
2. **The route is named.** "SAP FICO Consultant — Milaha, via Prime X" is
   actionable; "SAP FICO Consultant" is a hint.
3. **Ranking folds in reachability**, not match quality alone. The best match at
   an account with no way in loses to a good match at an existing customer.

The scoring itself is the same seven components in the same direction — a
requirement-resource pair scores identically whichever way it was discovered.
Two different numbers for one pair would be indefensible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from app.engines.matching.engine import (
    MatchResult,
    RequirementView,
    ResourceView,
    apply_hard_filters,
    score_match,
)


class RouteType(StrEnum):
    """How Glimmora reaches the account holding this requirement."""

    DIRECT = "DIRECT"
    VIA_PARTNER = "VIA_PARTNER"
    VIA_PRIME = "VIA_PRIME"
    VIA_VENDOR = "VIA_VENDOR"
    NO_KNOWN_ROUTE = "NO_KNOWN_ROUTE"
    UNKNOWN = "UNKNOWN"


ROUTE_LABELS: dict[RouteType, str] = {
    RouteType.DIRECT: "Direct",
    RouteType.VIA_PARTNER: "Via partner",
    RouteType.VIA_PRIME: "Via prime contractor",
    RouteType.VIA_VENDOR: "Via vendor / MSP",
    RouteType.NO_KNOWN_ROUTE: "No known route",
    RouteType.UNKNOWN: "Account not recorded",
}


@dataclass(slots=True)
class AccountView:
    """The account facts reverse matching needs, already loaded."""

    id: Any
    name: str
    account_type: str
    relationship_status: str
    is_existing_customer: bool
    is_existing_partner: bool
    is_approved_vendor: bool
    has_msa: bool
    contract_outsourcing_friendly: bool


@dataclass(slots=True)
class RouteResolution:
    """How we reach this seat, and how confident we are that we can."""

    route_type: RouteType
    #: The account we go *through*, when that differs from the end customer.
    via_account_id: Any = None
    via_account_name: str | None = None
    #: 0–1 multiplier on the match score, or None when the account is unknown.
    reachability: float | None = None
    label: str = ""
    evidence: str | None = None

    @property
    def is_blocked(self) -> bool:
        return self.route_type is RouteType.NO_KNOWN_ROUTE


def resolve_route(
    *,
    account: AccountView | None,
    via: AccountView | None,
    via_is_preferred: bool,
    thresholds: dict[str, Any],
) -> RouteResolution:
    """Name the route into an account and score how reachable it is.

    Reachability is a *multiplier*, not a component score, because it does not
    describe the person — it describes whether the seat is winnable at all. A
    94% match at an account nobody can reach is not a 94% opportunity.

    Returns `reachability=None` when the requirement names no account. That is
    unknown, not unreachable, and it is reported rather than assumed — the same
    rule the component scorers follow (SCORING.md section 1).
    """
    if account is None:
        return RouteResolution(
            RouteType.UNKNOWN,
            reachability=None,
            label=ROUTE_LABELS[RouteType.UNKNOWN],
            evidence="No account recorded on the requirement",
        )

    if account.relationship_status == "BLOCKED":
        return RouteResolution(
            RouteType.NO_KNOWN_ROUTE,
            reachability=float(thresholds["reachability_blocked"]),
            label=f"{account.name} — blocked",
            evidence="This account is marked blocked",
        )

    # A direct relationship beats any intermediary, so it is tested first.
    if account.is_existing_customer and account.has_msa:
        return RouteResolution(
            RouteType.DIRECT,
            reachability=float(thresholds["reachability_direct_msa"]),
            label=f"{account.name} (direct)",
            evidence="Existing customer with an MSA in place",
        )
    if account.is_existing_customer:
        return RouteResolution(
            RouteType.DIRECT,
            reachability=float(thresholds["reachability_direct_customer"]),
            label=f"{account.name} (direct)",
            evidence="Existing customer",
        )
    if account.is_approved_vendor:
        return RouteResolution(
            RouteType.DIRECT,
            reachability=float(thresholds["reachability_approved_vendor"]),
            label=f"{account.name} (approved vendor)",
            evidence="Glimmora is an approved vendor",
        )

    if via is not None:
        route_type = {
            "PRIME_CONTRACTOR": RouteType.VIA_PRIME,
            "VENDOR_MSP": RouteType.VIA_VENDOR,
        }.get(via.account_type, RouteType.VIA_PARTNER)
        score = (
            thresholds["reachability_preferred_route"]
            if via_is_preferred
            else thresholds["reachability_known_route"]
        )
        return RouteResolution(
            route_type,
            via_account_id=via.id,
            via_account_name=via.name,
            reachability=float(score),
            label=f"{account.name} (via {via.name})",
            evidence=("Preferred route" if via_is_preferred else "Known route")
            + f" via {via.name}",
        )

    return RouteResolution(
        RouteType.NO_KNOWN_ROUTE,
        reachability=float(thresholds["reachability_no_route"]),
        label=f"{account.name} — no route recorded",
        evidence="Neither a direct relationship nor a partner route is recorded",
    )


# ---------------------------------------------------------------- suggestions


@dataclass(slots=True)
class RedeploymentSuggestion:
    """One ranked next-assignment option for a resource."""

    requirement_id: Any
    requirement_title: str
    account_name: str | None
    match: MatchResult
    route: RouteResolution
    #: match score x reachability. Named for what it is — this is *not* the
    #: Opportunity Score, which composes talent, addressability and commercial
    #: and arrives in Phase 9.
    priority_score: float
    missing_information: list[str] = field(default_factory=list)

    @property
    def overall_score(self) -> float:
        return self.match.overall_score


def priority_for(match_score: float, reachability: float | None) -> float:
    """Rank position: match quality discounted by whether the seat is winnable.

    An unknown route does not discount anything. Guessing that an unrecorded
    account is unreachable would bury real opportunities for a data-entry gap.
    """
    if reachability is None:
        return round(match_score, 1)
    return round(match_score * reachability, 1)


def requirement_is_open_for(
    requirement: RequirementView, *, is_open: bool, awaiting_review: bool
) -> tuple[bool, str | None]:
    """Requirement-side filters, mirroring `apply_hard_filters` on the other side."""
    if not is_open:
        return False, "Requirement is closed"
    if awaiting_review:
        # Symmetry with AD-7: an unreviewed requirement is not business data
        # either, so it must not generate redeployment advice.
        return False, "Requirement is still awaiting review"
    return True, None


def rank_suggestions(
    resource: ResourceView,
    candidates: list[tuple[RequirementView, RouteResolution, str | None, bool, bool]],
    *,
    weights: dict[str, float],
    thresholds: dict[str, Any],
    today: date | None = None,
    limit: int = 10,
) -> list[RedeploymentSuggestion]:
    """Score every open requirement against one resource and rank the result.

    `candidates` carries (requirement, route, account_name, is_open,
    awaiting_review) so this function stays pure — all loading happens in the
    service layer, in batched queries.
    """
    reference = today or date.today()
    suggestions: list[RedeploymentSuggestion] = []

    for requirement, route, account_name, is_open, awaiting_review in candidates:
        included, _ = requirement_is_open_for(
            requirement, is_open=is_open, awaiting_review=awaiting_review
        )
        if not included:
            continue

        # The person-side filters are identical to forward matching, so a pair
        # excluded one way is excluded the other.
        outcome = apply_hard_filters(requirement, resource, thresholds)
        if not outcome.included:
            continue

        # A blocked account is never a suggestion, however well the person fits.
        if route.route_type is RouteType.NO_KNOWN_ROUTE and thresholds.get(
            "exclude_unreachable_accounts", False
        ):
            continue

        match = score_match(
            requirement, resource, weights=weights, thresholds=thresholds, today=reference
        )

        missing = list(match.missing_information)
        if route.reachability is None:
            missing.append("Account reachability")

        suggestions.append(
            RedeploymentSuggestion(
                requirement_id=requirement.id,
                requirement_title=requirement.title,
                account_name=account_name,
                match=match,
                route=route,
                priority_score=priority_for(match.overall_score, route.reachability),
                missing_information=missing,
            )
        )

    suggestions.sort(key=lambda item: (item.priority_score, item.overall_score), reverse=True)
    return suggestions[:limit]


__all__ = [
    "ROUTE_LABELS",
    "AccountView",
    "RedeploymentSuggestion",
    "RouteResolution",
    "RouteType",
    "priority_for",
    "rank_suggestions",
    "requirement_is_open_for",
    "resolve_route",
]
