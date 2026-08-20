"""The Glimmora Opportunity Score.

```
0.40 * talent  +  0.35 * addressability  +  0.25 * commercial
```

This is the number the SOW says the platform earns its value on. Two rules make
it defensible rather than merely plausible:

* **A missing component is redistributed, never zeroed.** An unpriced
  requirement is unknown, not bad, and substituting zero would make it look
  actively worse than one nobody has assessed at all.
* **Suppressors cap the band without touching the score.** The arithmetic stays
  reproducible from its components; only the headline verdict is capped, because
  a salesperson reads the band before the risks (SCORING.md section 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.engines.scoring.addressability import AddressabilityResult
from app.engines.scoring.commercial import CommercialResult
from app.engines.scoring.config import OPPORTUNITY_COMPONENT_LABELS

ENGINE_VERSION = "1.0.0"


class OpportunityBand(StrEnum):
    PURSUE_NOW = "PURSUE_NOW"
    PURSUE = "PURSUE"
    REVIEW = "REVIEW"
    DEPRIORITIZE = "DEPRIORITIZE"


BAND_ACTIONS: dict[OpportunityBand, str] = {
    OpportunityBand.PURSUE_NOW: "Submit CVs today; assign an owner",
    OpportunityBand.PURSUE: "Qualify and contact the account",
    OpportunityBand.REVIEW: "Close the named gaps first",
    OpportunityBand.DEPRIORITIZE: "Log and monitor; do not spend sales time",
}

_BAND_ORDER = [
    OpportunityBand.DEPRIORITIZE,
    OpportunityBand.REVIEW,
    OpportunityBand.PURSUE,
    OpportunityBand.PURSUE_NOW,
]

#: Conditions that cap the band however well the arithmetic scores, with the
#: risk each one raises. SCORING.md section 5.
SUPPRESSORS: dict[str, tuple[str, OpportunityBand, str]] = {
    "NOT_ADDRESSABLE": (
        "Addressability below 40 — there is no proven way into this account yet",
        OpportunityBand.REVIEW,
        "Open a route or secure vendor approval before spending sales time",
    ),
    "SLA_EXPIRED": (
        "The submission deadline has passed",
        OpportunityBand.DEPRIORITIZE,
        "Confirm with the client whether the requirement is still open",
    ),
    "WORK_AUTH_EXPIRED": (
        "Best-matched consultant has expired work authorisation",
        OpportunityBand.REVIEW,
        "Renew the work permit or identify an alternative consultant",
    ),
    "NEGATIVE_MARGIN": (
        "Negative margin at current rates",
        OpportunityBand.REVIEW,
        "Renegotiate the bill rate or the consultant's cost before proceeding",
    ),
}


@dataclass(slots=True)
class ComponentScore:
    key: str
    label: str
    score: float | None
    weight: float
    contribution: float

    @property
    def is_known(self) -> bool:
        return self.score is not None


@dataclass(slots=True)
class OpportunityResult:
    score: float
    band: OpportunityBand
    confidence: float
    components: list[ComponentScore] = field(default_factory=list)
    factors: list[dict[str, Any]] = field(default_factory=list)
    positives: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    suppressors: list[str] = field(default_factory=list)
    recommended_action: str = ""
    narrative: str = ""
    engine_version: str = ENGINE_VERSION

    def component(self, key: str) -> ComponentScore | None:
        return next((item for item in self.components if item.key == key), None)


def band_for(score: float, weights_config: dict[str, Any]) -> OpportunityBand:
    if score >= weights_config["band_pursue_now"]:
        return OpportunityBand.PURSUE_NOW
    if score >= weights_config["band_pursue"]:
        return OpportunityBand.PURSUE
    if score >= weights_config["band_review"]:
        return OpportunityBand.REVIEW
    return OpportunityBand.DEPRIORITIZE


def apply_suppressors(band: OpportunityBand, codes: list[str]) -> OpportunityBand:
    """Cap the band. Suppressors only ever lower it, never promote."""
    capped = band
    for code in codes:
        ceiling = SUPPRESSORS[code][1]
        if _BAND_ORDER.index(capped) > _BAND_ORDER.index(ceiling):
            capped = ceiling
    return capped


def compose(
    *,
    talent_match: float | None,
    addressability: AddressabilityResult | None,
    commercial: CommercialResult | None,
    weights_config: dict[str, Any],
    sla_expired: bool = False,
    best_match_work_auth_expired: bool = False,
    now: datetime | None = None,
) -> OpportunityResult:
    """Compose the three components into one defensible number."""
    weights: dict[str, float] = weights_config["weights"]

    raw = {
        "talent_match": talent_match,
        "addressability": addressability.score if addressability else None,
        "commercial": commercial.score if commercial and commercial.score is not None else None,
    }

    known_weight = sum(weights[key] for key, value in raw.items() if value is not None)
    total_weight = sum(weights.values())

    components: list[ComponentScore] = []
    if known_weight > 0:
        for key in weights:
            value = raw[key]
            # Redistribute proportionally across what *is* known, so a missing
            # component neither drags the score down nor silently vanishes.
            effective = weights[key] / known_weight if value is not None else 0.0
            components.append(
                ComponentScore(
                    key=key,
                    label=OPPORTUNITY_COMPONENT_LABELS[key],
                    score=value,
                    weight=weights[key],
                    contribution=round((value or 0) * effective, 2),
                )
            )
        score = round(sum(item.contribution for item in components))
    else:
        components = [
            ComponentScore(key, OPPORTUNITY_COMPONENT_LABELS[key], None, weights[key], 0.0)
            for key in weights
        ]
        score = 0

    confidence = round(known_weight / total_weight, 3) if total_weight else 0.0

    # --- suppressors -----------------------------------------------------
    codes: list[str] = []
    if addressability is not None and addressability.score < 40:
        codes.append("NOT_ADDRESSABLE")
    if sla_expired:
        codes.append("SLA_EXPIRED")
    if best_match_work_auth_expired:
        codes.append("WORK_AUTH_EXPIRED")
    if (
        commercial is not None
        and commercial.calculation.margin_percent is not None
        and commercial.calculation.margin_percent <= 0
    ):
        codes.append("NEGATIVE_MARGIN")

    band = apply_suppressors(band_for(float(score), weights_config), codes)

    result = OpportunityResult(
        score=float(score),
        band=band,
        confidence=confidence,
        components=components,
        suppressors=[SUPPRESSORS[code][0] for code in codes],
    )

    # --- explanation -----------------------------------------------------
    if addressability is not None:
        result.factors = [
            {
                "key": factor.key,
                "label": factor.label,
                "state": factor.state.value,
                "points": factor.points,
                "max_points": factor.max_points,
                "evidence": factor.evidence,
            }
            for factor in addressability.factors
        ]
        result.positives.extend(addressability.positives)
        result.risks.extend(addressability.risks)
        result.missing_information.extend(addressability.missing_information)
    else:
        result.missing_information.append("Addressability has not been assessed")

    if commercial is not None:
        result.positives.extend(commercial.positives)
        result.risks.extend(commercial.risks)
        result.missing_information.extend(commercial.missing_information)
    else:
        result.missing_information.append("Commercial value has not been assessed")

    if talent_match is None:
        result.missing_information.append("No matching has been run for this requirement")
    elif talent_match >= 80:
        result.positives.insert(0, f"Strong talent match available ({talent_match:g}%)")
    elif talent_match < 50:
        result.risks.insert(0, f"No strong candidate identified (best match {talent_match:g}%)")

    # A suppressor's own action outranks the band's generic one: "renew the work
    # permit" is more useful than "close the named gaps first".
    if codes:
        result.risks = [SUPPRESSORS[code][0] for code in codes] + [
            risk for risk in result.risks if risk not in {SUPPRESSORS[c][0] for c in codes}
        ]
        result.recommended_action = SUPPRESSORS[codes[0]][2]
    else:
        result.recommended_action = BAND_ACTIONS[band]

    result.narrative = build_narrative(result, talent_match=talent_match)
    return result


def build_narrative(result: OpportunityResult, *, talent_match: float | None) -> str:
    """Deterministic summary.

    An LLM may rewrite this later, but only *from* the structured object above —
    it can never change a number (AD-1). This template is also what the UI falls
    back to whenever no model is configured.
    """
    verdict = {
        OpportunityBand.PURSUE_NOW: "a strong opportunity",
        OpportunityBand.PURSUE: "worth pursuing",
        OpportunityBand.REVIEW: "one to review before committing",
        OpportunityBand.DEPRIORITIZE: "not worth sales time yet",
    }[result.band]

    sentences = [f"Scores {result.score:g} — {verdict}."]

    known = [item for item in result.components if item.is_known]
    if known:
        sentences.append(
            "Built from "
            + ", ".join(f"{item.label.lower()} {item.score:g}" for item in known)
            + "."
        )

    if result.suppressors:
        sentences.append(f"Capped because: {result.suppressors[0].lower()}.")

    unknown = [item for item in result.components if not item.is_known]
    if unknown:
        sentences.append(
            ", ".join(item.label for item in unknown)
            + (" is" if len(unknown) == 1 else " are")
            + " not yet assessed, so the remaining weight was redistributed rather than"
            + " counted as zero."
        )

    if result.recommended_action:
        sentences.append(f"Next: {result.recommended_action.lower()}.")

    return " ".join(sentences)


__all__ = [
    "BAND_ACTIONS",
    "ENGINE_VERSION",
    "SUPPRESSORS",
    "ComponentScore",
    "OpportunityBand",
    "OpportunityResult",
    "apply_suppressors",
    "band_for",
    "build_narrative",
    "compose",
]
