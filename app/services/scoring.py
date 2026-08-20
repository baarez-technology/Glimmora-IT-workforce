"""Scoring service: gather the facts, run the engines, persist the snapshot.

The gathering is the interesting part. An opportunity score reaches across
accounts, contacts, routes, the requirement, the match snapshot and the
consultant's documents — and every one of those may be absent. Absent is
recorded as unknown, never coerced to a default, because the whole value of the
explainability object is telling a salesperson *which* fact is missing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import log_business_event
from app.db.types import utcnow
from app.engines.scoring.addressability import (
    AddressabilityInput,
    AddressabilityResult,
    score_addressability,
)
from app.engines.scoring.commercial import (
    CommercialInput,
    CommercialResult,
    score_commercial,
    to_monthly,
)
from app.engines.scoring.config import (
    DEFAULT_ADDRESSABILITY_RULES,
    DEFAULT_COMMERCIAL_BANDS,
    DEFAULT_CURRENCY_RATES,
    DEFAULT_OPPORTUNITY_WEIGHTS,
    validate_addressability_rules,
    validate_commercial_bands,
    validate_opportunity_weights,
)
from app.engines.scoring.opportunity import ENGINE_VERSION, OpportunityResult, compose
from app.models.accounts import Account, AccountRelationship, Contact
from app.models.demand import Requirement
from app.models.identity import AuditAction, User
from app.models.matching import Match, MatchDirection, ScoringConfigKind, ScoringConfiguration
from app.models.scoring import AddressabilityBandEnum, OpportunityBandEnum, OpportunityScore
from app.models.talent import Resource
from app.services.audit import AuditService
from app.services.documents import work_authorisation_state
from app.services.matching import ScoringConfigService

#: Which shipped defaults back-fill each configuration kind on read. Same
#: merge-on-read contract as the matching thresholds: a stored payload is a
#: snapshot and cannot contain keys the code learned about afterwards.
_DEFAULTS: dict[ScoringConfigKind, dict[str, Any]] = {
    ScoringConfigKind.ADDRESSABILITY_RULES: DEFAULT_ADDRESSABILITY_RULES,
    ScoringConfigKind.COMMERCIAL_BANDS: DEFAULT_COMMERCIAL_BANDS,
    ScoringConfigKind.OPPORTUNITY_WEIGHTS: DEFAULT_OPPORTUNITY_WEIGHTS,
}

_VALIDATORS = {
    ScoringConfigKind.ADDRESSABILITY_RULES: validate_addressability_rules,
    ScoringConfigKind.COMMERCIAL_BANDS: validate_commercial_bands,
    ScoringConfigKind.OPPORTUNITY_WEIGHTS: validate_opportunity_weights,
}


def validate_payload(kind: ScoringConfigKind, payload: dict[str, Any]) -> None:
    """Reject an invalid rule set at write time, with a usable message."""
    validator = _VALIDATORS.get(kind)
    if validator is None:
        return
    try:
        validator(payload)
    except ValueError as exc:
        raise ValidationError(
            str(exc), details=[{"field": "payload", "message": str(exc)}]
        ) from exc


def effective(kind: ScoringConfigKind, payload: dict[str, Any]) -> dict[str, Any]:
    """Stored payload layered over the shipped defaults."""
    return {**_DEFAULTS.get(kind, {}), **(payload or {})}


class ScoringService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.configs = ScoringConfigService(session)
        self.audit = AuditService(session)

    # ------------------------------------------------------------ loading
    async def _requirement(self, requirement_id: uuid.UUID) -> Requirement:
        requirement = (
            await self.session.execute(select(Requirement).where(Requirement.id == requirement_id))
        ).scalar_one_or_none()
        if requirement is None:
            raise NotFoundError("requirement", requirement_id)
        return requirement

    async def _account_facts(self, requirement: Requirement) -> dict[str, Any]:
        """Account, route and contact facts. Absent stays None, never False."""
        if requirement.account_id is None:
            return {}

        account = (
            await self.session.execute(select(Account).where(Account.id == requirement.account_id))
        ).scalar_one_or_none()
        if account is None:
            return {}

        has_decision_maker = (
            await self.session.execute(
                select(func.count())
                .select_from(Contact)
                .where(
                    Contact.account_id == account.id,
                    Contact.is_decision_maker.is_(True),
                )
            )
        ).scalar() or 0

        contact_count = (
            await self.session.execute(
                select(func.count()).select_from(Contact).where(Contact.account_id == account.id)
            )
        ).scalar() or 0

        route_count = (
            await self.session.execute(
                select(func.count())
                .select_from(AccountRelationship)
                .where(
                    AccountRelationship.to_account_id == account.id,
                    AccountRelationship.is_preferred_route.is_(True),
                )
            )
        ).scalar() or 0

        return {
            "account": account,
            "contract_outsourcing_friendly": account.contract_outsourcing_friendly,
            "is_existing_customer": account.is_existing_customer,
            "is_existing_partner": account.is_existing_partner,
            "has_preferred_route": route_count > 0 or requirement.route_account_id is not None,
            "is_approved_vendor": account.is_approved_vendor,
            "has_msa": account.has_msa,
            # No contacts at all is *unknown*; contacts but none flagged is a
            # real "no". Collapsing these would turn a data-entry gap into a
            # business conclusion.
            "has_decision_maker": (has_decision_maker > 0) if contact_count else None,
            "relationship_status": account.relationship_status.value,
        }

    async def _best_match(self, requirement_id: uuid.UUID) -> tuple[float | None, Match | None]:
        """The best single match. Glimmora needs one strong person, not an average."""
        row = (
            (
                await self.session.execute(
                    select(Match)
                    .where(
                        Match.requirement_id == requirement_id,
                        Match.direction == MatchDirection.DEMAND_TO_RESOURCE,
                    )
                    .order_by(Match.overall_score.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        return (float(row.overall_score), row) if row else (None, None)

    async def _best_resource(self, match: Match | None) -> Resource | None:
        """The consultant we would actually deploy, with documents eager-loaded.

        `Resource.documents` is `lazy="raise"`, so this must be explicit — and
        loading it once serves both the cost lookup and the work-authorisation
        suppressor rather than querying twice.
        """
        if match is None:
            return None
        return (
            await self.session.execute(
                select(Resource)
                .where(Resource.id == match.resource_id)
                .options(selectinload(Resource.documents))
            )
        ).scalar_one_or_none()

    @staticmethod
    def _work_auth_expired(resource: Resource | None, *, today: Any) -> bool:
        if resource is None:
            return False
        return work_authorisation_state(list(resource.documents), today=today).state.value == (
            "EXPIRED"
        )

    # ---------------------------------------------------------- scoring
    async def score_requirement(
        self,
        requirement_id: uuid.UUID,
        *,
        actor: User | None = None,
        persist: bool = True,
        now: datetime | None = None,
        overrides: dict[str, Any] | None = None,
        config_overrides: dict[ScoringConfigKind, ScoringConfiguration] | None = None,
    ) -> tuple[OpportunityResult, AddressabilityResult, CommercialResult, OpportunityScore | None]:
        reference = now or utcnow()
        requirement = await self._requirement(requirement_id)

        # A draft configuration can stand in for the active one, which is what
        # makes the simulation preview possible without activating anything.
        drafts = config_overrides or {}

        async def _config(kind: ScoringConfigKind) -> ScoringConfiguration:
            return drafts.get(kind) or await self.configs.active(kind)

        addr_config = await _config(ScoringConfigKind.ADDRESSABILITY_RULES)
        comm_config = await _config(ScoringConfigKind.COMMERCIAL_BANDS)
        opp_config = await _config(ScoringConfigKind.OPPORTUNITY_WEIGHTS)
        match_config = await _config(ScoringConfigKind.MATCH_WEIGHTS)

        rules = effective(ScoringConfigKind.ADDRESSABILITY_RULES, addr_config.payload)
        bands = effective(ScoringConfigKind.COMMERCIAL_BANDS, comm_config.payload)
        weights = effective(ScoringConfigKind.OPPORTUNITY_WEIGHTS, opp_config.payload)
        rates = {**DEFAULT_CURRENCY_RATES, **(bands.get("currency_rates") or {})}

        facts = await self._account_facts(requirement)
        best_match, match_row = await self._best_match(requirement_id)

        monthly_rate = to_monthly(
            requirement.rate_max or requirement.rate_min,
            requirement.rate_unit.value if requirement.rate_unit else None,
            working_days=int(bands["working_days_per_month"]),
            hours_per_day=int(bands["hours_per_day"]),
        )

        addressability = score_addressability(
            AddressabilityInput(
                account_known=bool(facts),
                contract_outsourcing_friendly=facts.get("contract_outsourcing_friendly"),
                is_existing_customer=facts.get("is_existing_customer"),
                is_existing_partner=facts.get("is_existing_partner"),
                has_preferred_route=facts.get("has_preferred_route"),
                is_approved_vendor=facts.get("is_approved_vendor"),
                has_msa=facts.get("has_msa"),
                has_decision_maker=facts.get("has_decision_maker"),
                relationship_status=facts.get("relationship_status"),
                requirement_is_open=requirement.is_open,
                response_deadline_at=requirement.response_deadline_at,
                monthly_rate=monthly_rate,
                best_talent_match=best_match,
            ),
            rules=rules,
            now=reference,
        )

        # Consultant cost comes from the best-matched resource: the commercial
        # picture is only meaningful for the person we would actually deploy.
        best_resource = await self._best_resource(match_row)
        cost_rate = best_resource.expected_cost_amount if best_resource else None
        cost_unit = best_resource.expected_cost_unit if best_resource else None
        cost_currency = best_resource.expected_cost_currency if best_resource else None

        overrides = overrides or {}
        commercial = score_commercial(
            CommercialInput(
                bill_rate=overrides.get("bill_rate", requirement.rate_max or requirement.rate_min),
                bill_unit=overrides.get(
                    "bill_unit", requirement.rate_unit.value if requirement.rate_unit else None
                ),
                bill_currency=overrides.get("bill_currency", requirement.rate_currency),
                cost_rate=overrides.get("cost_rate", cost_rate),
                cost_unit=overrides.get("cost_unit", cost_unit),
                cost_currency=overrides.get("cost_currency", cost_currency),
                visa_cost=overrides.get("visa_cost"),
                insurance_cost=overrides.get("insurance_cost"),
                other_cost=overrides.get("other_cost"),
                duration_months=overrides.get("duration_months", requirement.duration_months),
                positions=overrides.get("positions", requirement.positions or 1),
            ),
            bands=bands,
            rates=rates,
        )

        deadline = requirement.response_deadline_at
        opportunity = compose(
            talent_match=best_match,
            addressability=addressability,
            commercial=commercial,
            weights_config=weights,
            sla_expired=bool(deadline and deadline <= reference),
            best_match_work_auth_expired=self._work_auth_expired(
                best_resource, today=reference.date()
            ),
            now=reference,
        )

        snapshot: OpportunityScore | None = None
        if persist:
            snapshot = await self._persist(
                requirement,
                opportunity,
                addressability,
                commercial,
                versions={
                    "addressability": addr_config.version,
                    "commercial": comm_config.version,
                    "opportunity": opp_config.version,
                    "match": match_config.version,
                },
                actor=actor,
                computed_at=reference,
            )
            if actor is not None:
                await self.audit.record(
                    AuditAction.SCORE_COMPUTED,
                    summary=(
                        f"Scored {requirement.title}: {opportunity.score:g} "
                        f"({opportunity.band.value})"
                    ),
                    actor=actor,
                    entity_type="requirement",
                    entity_id=requirement.id,
                )
            log_business_event(
                "score_calculated",
                requirement_id=str(requirement.id),
                score=opportunity.score,
                band=opportunity.band.value,
            )

        return opportunity, addressability, commercial, snapshot

    async def _persist(
        self,
        requirement: Requirement,
        opportunity: OpportunityResult,
        addressability: AddressabilityResult,
        commercial: CommercialResult,
        *,
        versions: dict[str, int],
        actor: User | None,
        computed_at: datetime,
    ) -> OpportunityScore:
        # Append-only: the previous snapshot stays, it just stops being current.
        await self.session.execute(
            update(OpportunityScore)
            .where(
                OpportunityScore.requirement_id == requirement.id,
                OpportunityScore.is_current.is_(True),
            )
            .values(is_current=False)
        )

        calc = commercial.calculation
        talent = opportunity.component("talent_match")
        snapshot = OpportunityScore(
            requirement_id=requirement.id,
            talent_match_score=talent.score if talent else None,
            addressability_score=addressability.score,
            commercial_score=commercial.score,
            opportunity_score=opportunity.score,
            band=OpportunityBandEnum(opportunity.band.value),
            addressability_band=AddressabilityBandEnum(addressability.band.value),
            confidence=opportunity.confidence,
            supply_gate=addressability.supply_gate,
            monthly_revenue=calc.monthly_revenue,
            monthly_cost=calc.monthly_cost,
            gross_profit=calc.gross_profit,
            margin_percent=calc.margin_percent,
            contract_value=calc.contract_value,
            total_profit=calc.total_profit,
            currency=calc.currency,
            is_converted=calc.is_converted,
            components={
                item.key: {
                    "label": item.label,
                    "score": item.score,
                    "weight": item.weight,
                    "contribution": item.contribution,
                }
                for item in opportunity.components
            },
            factor_breakdown=opportunity.factors,
            commercial_breakdown=[
                {
                    "key": item.key,
                    "label": item.label,
                    "points": item.points,
                    "max_points": item.max_points,
                    "evidence": item.evidence,
                }
                for item in commercial.sub_scores
            ],
            positives=opportunity.positives,
            risks=opportunity.risks,
            missing_information=opportunity.missing_information,
            suppressors=opportunity.suppressors,
            recommended_action=opportunity.recommended_action,
            narrative=opportunity.narrative,
            addressability_config_version=versions["addressability"],
            commercial_config_version=versions["commercial"],
            opportunity_config_version=versions["opportunity"],
            match_config_version=versions["match"],
            engine_version=ENGINE_VERSION,
            is_current=True,
            computed_at=computed_at,
            computed_by=actor.id if actor else None,
        )
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    # ---------------------------------------------------------- reading
    async def current(self, requirement_id: uuid.UUID) -> OpportunityScore | None:
        return (
            (
                await self.session.execute(
                    select(OpportunityScore).where(
                        OpportunityScore.requirement_id == requirement_id,
                        OpportunityScore.is_current.is_(True),
                    )
                )
            )
            .scalars()
            .first()
        )

    async def history(
        self, requirement_id: uuid.UUID, *, limit: int = 20
    ) -> list[OpportunityScore]:
        rows = await self.session.execute(
            select(OpportunityScore)
            .where(OpportunityScore.requirement_id == requirement_id)
            .order_by(OpportunityScore.computed_at.desc())
            .limit(limit)
        )
        return list(rows.scalars().all())

    async def ranked(self, *, limit: int = 50, band: str | None = None) -> list[OpportunityScore]:
        stmt = select(OpportunityScore).where(OpportunityScore.is_current.is_(True))
        if band:
            stmt = stmt.where(OpportunityScore.band == band)
        stmt = stmt.order_by(OpportunityScore.opportunity_score.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["ScoringService", "effective", "validate_payload"]
