"""The 12-stage sales pipeline (SOW section 10).

The stage machine lives on its own, with no database in sight, because "which
moves are legal" is a business rule that has to be assertable by a test and
renderable in the UI — not something rediscovered by reading service code.

Two rules shape it:

* **Forward moves may skip.** Real deals do: a client who already knows the
  consultant goes straight from CONTACTED to INTERVIEW. Forcing every
  intermediate step would teach people to lie to the system.
* **Backward moves are allowed but recorded.** A deal that returns from
  INTERVIEW to CV_SUBMITTED is a real event, and hiding it would make the funnel
  conversion figures fiction.
"""

from __future__ import annotations

from enum import StrEnum


class OpportunityStage(StrEnum):
    REQUIREMENT_IDENTIFIED = "REQUIREMENT_IDENTIFIED"
    MATCHED = "MATCHED"
    QUALIFIED = "QUALIFIED"
    CONTACTED = "CONTACTED"
    CV_SUBMITTED = "CV_SUBMITTED"
    INTERVIEW = "INTERVIEW"
    COMMERCIAL_NEGOTIATION = "COMMERCIAL_NEGOTIATION"
    SELECTED = "SELECTED"
    PO_CONTRACT = "PO_CONTRACT"
    DEPLOYED = "DEPLOYED"
    BILLING = "BILLING"
    EXTENSION_REDEPLOYMENT = "EXTENSION_REDEPLOYMENT"
    LOST = "LOST"
    DROPPED = "DROPPED"


class OpportunityDecision(StrEnum):
    """The human answer to the score. Deliberately separate from the stage."""

    PURSUE = "PURSUE"
    HOLD = "HOLD"
    DECLINE = "DECLINE"


#: The forward ladder. Order matters: it defines "forward", drives the board
#: column order, and gives the funnel its shape.
STAGE_ORDER: list[OpportunityStage] = [
    OpportunityStage.REQUIREMENT_IDENTIFIED,
    OpportunityStage.MATCHED,
    OpportunityStage.QUALIFIED,
    OpportunityStage.CONTACTED,
    OpportunityStage.CV_SUBMITTED,
    OpportunityStage.INTERVIEW,
    OpportunityStage.COMMERCIAL_NEGOTIATION,
    OpportunityStage.SELECTED,
    OpportunityStage.PO_CONTRACT,
    OpportunityStage.DEPLOYED,
    OpportunityStage.BILLING,
    OpportunityStage.EXTENSION_REDEPLOYMENT,
]

#: Terminal outcomes. Reachable from anywhere; nothing is reachable from them
#: except a deliberate reopen.
TERMINAL_STAGES: frozenset[OpportunityStage] = frozenset(
    {OpportunityStage.LOST, OpportunityStage.DROPPED}
)

STAGE_LABELS: dict[OpportunityStage, str] = {
    OpportunityStage.REQUIREMENT_IDENTIFIED: "Requirement identified",
    OpportunityStage.MATCHED: "Matched",
    OpportunityStage.QUALIFIED: "Qualified",
    OpportunityStage.CONTACTED: "Client contacted",
    OpportunityStage.CV_SUBMITTED: "CV submitted",
    OpportunityStage.INTERVIEW: "Interview",
    OpportunityStage.COMMERCIAL_NEGOTIATION: "Commercial negotiation",
    OpportunityStage.SELECTED: "Selected",
    OpportunityStage.PO_CONTRACT: "PO / contract",
    OpportunityStage.DEPLOYED: "Deployed",
    OpportunityStage.BILLING: "Billing",
    OpportunityStage.EXTENSION_REDEPLOYMENT: "Extension / redeployment",
    OpportunityStage.LOST: "Lost",
    OpportunityStage.DROPPED: "Dropped",
}

#: Stages that require a submission to exist first. Claiming a CV went to the
#: client without recording which one is exactly the gap this phase closes.
STAGES_REQUIRING_SUBMISSION: frozenset[OpportunityStage] = frozenset(
    {
        OpportunityStage.CV_SUBMITTED,
        OpportunityStage.INTERVIEW,
        OpportunityStage.SELECTED,
    }
)

#: Losing needs a reason. "Lost" with no explanation teaches nobody anything.
STAGES_REQUIRING_REASON: frozenset[OpportunityStage] = TERMINAL_STAGES


def stage_index(stage: OpportunityStage) -> int:
    """Position on the forward ladder. Terminal stages sit past the end."""
    if stage in TERMINAL_STAGES:
        return len(STAGE_ORDER)
    return STAGE_ORDER.index(stage)


def is_forward(current: OpportunityStage, target: OpportunityStage) -> bool:
    return stage_index(target) > stage_index(current)


def is_terminal(stage: OpportunityStage) -> bool:
    return stage in TERMINAL_STAGES


def can_transition(current: OpportunityStage, target: OpportunityStage) -> tuple[bool, str | None]:
    """Whether a move is legal, and why not when it is not."""
    if current is target:
        return False, "The opportunity is already at that stage"

    if current in TERMINAL_STAGES and target in TERMINAL_STAGES:
        return False, f"Already closed as {STAGE_LABELS[current].lower()}"

    # Reopening is legal — deals do come back — but only onto the ladder, and
    # it is recorded like any other move.
    if current in TERMINAL_STAGES:
        return True, None

    return True, None


def next_suggested(stage: OpportunityStage) -> OpportunityStage | None:
    """The natural next step, for the board's primary action."""
    if stage in TERMINAL_STAGES:
        return None
    index = STAGE_ORDER.index(stage)
    if index + 1 >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[index + 1]


#: Which stage a submission outcome implies for its opportunity. Keeping the
#: mapping here rather than in the service means the board and the submission
#: screen cannot drift apart on what a status means.
SUBMISSION_STAGE_HINTS: dict[str, OpportunityStage] = {
    "SUBMITTED": OpportunityStage.CV_SUBMITTED,
    "SHORTLISTED": OpportunityStage.CV_SUBMITTED,
    "INTERVIEW": OpportunityStage.INTERVIEW,
    "SELECTED": OpportunityStage.SELECTED,
}


__all__ = [
    "STAGES_REQUIRING_REASON",
    "STAGES_REQUIRING_SUBMISSION",
    "STAGE_LABELS",
    "STAGE_ORDER",
    "SUBMISSION_STAGE_HINTS",
    "TERMINAL_STAGES",
    "OpportunityDecision",
    "OpportunityStage",
    "can_transition",
    "is_forward",
    "is_terminal",
    "next_suggested",
    "stage_index",
]
