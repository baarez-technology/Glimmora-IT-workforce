"""Document expiry — revenue protection, not administration.

In the Gulf an expired QID or work permit stops a consultant working, which
stops billing on a live deployment. So expiry state is derived on every read
(never stored stale), reminders fire at 90/60/30/7 days, and the resource's
overall work-authorisation state is computed from its worst document rather
than from a field somebody has to remember to update (SOW section 6 NEW).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.core.config import settings
from app.models.talent import (
    WORK_AUTHORISATION_TYPES,
    DocumentExpiryState,
    DocumentType,
    ResourceDocument,
    VisaStatus,
)


@dataclass(frozen=True, slots=True)
class ExpiryStatus:
    state: DocumentExpiryState
    expiry_date: date | None
    days_remaining: int | None
    is_expired: bool
    label: str

    @property
    def needs_attention(self) -> bool:
        return self.state in {DocumentExpiryState.EXPIRING_SOON, DocumentExpiryState.EXPIRED}


def expiry_status(expiry_date: date | None, *, today: date | None = None) -> ExpiryStatus:
    """Classify a document by how much validity is left."""
    if expiry_date is None:
        return ExpiryStatus(
            DocumentExpiryState.NOT_APPLICABLE, None, None, False, "No expiry recorded"
        )

    reference = today or datetime.now(UTC).date()
    days = (expiry_date - reference).days

    if days < 0:
        return ExpiryStatus(
            DocumentExpiryState.EXPIRED, expiry_date, days, True, _overdue_label(-days)
        )
    if days <= settings.DOCUMENT_EXPIRING_SOON_DAYS:
        return ExpiryStatus(
            DocumentExpiryState.EXPIRING_SOON, expiry_date, days, False, _remaining_label(days)
        )
    return ExpiryStatus(DocumentExpiryState.VALID, expiry_date, days, False, _remaining_label(days))


def _remaining_label(days: int) -> str:
    if days == 0:
        return "Expires today"
    if days == 1:
        return "Expires tomorrow"
    if days < 60:
        return f"{days} days left"
    return f"{days // 30} months left"


def _overdue_label(days: int) -> str:
    if days == 0:
        return "Expired today"
    if days == 1:
        return "Expired yesterday"
    if days < 60:
        return f"Expired {days} days ago"
    return f"Expired {days // 30} months ago"


def reminder_milestone(days_remaining: int) -> int | None:
    """The configured reminder step this document has just crossed, if any.

    Returns the milestone so the notification can be deduplicated per step —
    a daily repeat trains people to ignore the alert (A16).
    """
    for milestone in settings.document_reminder_days:
        if days_remaining == milestone:
            return milestone
    return None


def work_authorisation_state(
    documents: list[ResourceDocument], *, today: date | None = None
) -> ExpiryStatus:
    """The resource's overall right to work, taken from its worst document.

    A valid passport does not compensate for an expired work permit, so the
    worst state wins.
    """
    relevant = [
        document
        for document in documents
        if document.doc_type in WORK_AUTHORISATION_TYPES and document.expiry_date is not None
    ]
    if not relevant:
        return ExpiryStatus(
            DocumentExpiryState.NOT_APPLICABLE,
            None,
            None,
            False,
            "No work authorisation recorded",
        )

    statuses = [expiry_status(document.expiry_date, today=today) for document in relevant]
    severity = {
        DocumentExpiryState.EXPIRED: 0,
        DocumentExpiryState.EXPIRING_SOON: 1,
        DocumentExpiryState.VALID: 2,
        DocumentExpiryState.NOT_APPLICABLE: 3,
    }
    return min(statuses, key=lambda status: (severity[status.state], status.days_remaining or 0))


def derive_visa_status(
    documents: list[ResourceDocument], *, today: date | None = None
) -> VisaStatus:
    """Map the work-authorisation state onto the resource's visa status field."""
    state = work_authorisation_state(documents, today=today).state
    if state is DocumentExpiryState.EXPIRED:
        return VisaStatus.EXPIRED
    if state in {DocumentExpiryState.VALID, DocumentExpiryState.EXPIRING_SOON}:
        return VisaStatus.VALID
    return VisaStatus.UNKNOWN


def blocks_deployment(documents: list[ResourceDocument], *, today: date | None = None) -> bool:
    """Whether a lapsed document would stop this consultant billing today."""
    return work_authorisation_state(documents, today=today).state is DocumentExpiryState.EXPIRED


def describe_document(doc_type: DocumentType) -> str:
    labels = {
        DocumentType.CV: "CV",
        DocumentType.ID: "Identity document",
        DocumentType.PASSPORT: "Passport",
        DocumentType.VISA: "Visa",
        DocumentType.WORK_PERMIT: "Work permit",
        DocumentType.QID: "Qatar ID (QID)",
        DocumentType.CONTRACT: "Contract",
        DocumentType.CERTIFICATE: "Certificate",
        DocumentType.OTHER: "Other document",
    }
    return labels[doc_type]


__all__ = [
    "ExpiryStatus",
    "blocks_deployment",
    "derive_visa_status",
    "describe_document",
    "expiry_status",
    "reminder_milestone",
    "work_authorisation_state",
]
