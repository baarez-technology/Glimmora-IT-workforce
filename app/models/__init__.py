"""SQLAlchemy model registry.

Importing this package must register every table on ``Base.metadata`` — Alembic
autogenerate and the test-suite schema creation both depend on it. Each phase
adds its models here.
"""

from __future__ import annotations

from app.db.base import Base, BaseEntity, SoftDeleteEntity

# --- Phase 4: accounts ---------------------------------------------------
from app.models.accounts import (
    Account,
    AccountRelationship,
    AccountType,
    Activity,
    ActivityType,
    Contact,
    Project,
    ProjectStatus,
    ProjectTechnology,
    RelationshipStatus,
    RelationType,
    Technology,
)

# --- Phase 11: delivery --------------------------------------------------
from app.models.delivery import (
    REALISED_BILLING_STATUSES,
    BillingRecord,
    BillingStatus,
    Deployment,
    DeploymentStatus,
)

# --- Phase 5: demand -----------------------------------------------------
from app.models.demand import (
    ContractType,
    DeadlineState,
    PrioritySource,
    RateUnit,
    Requirement,
    RequirementSkill,
    RequirementSource,
    RequirementStatus,
    RequirementStatusHistory,
    ReviewStatus,
    SkillImportance,
    WorkMode,
)

# --- Phase 3: identity ---------------------------------------------------
from app.models.identity import AuditAction, AuditLog, LoginAttempt, RefreshToken, User

# --- Phase 7-9: intelligence ---------------------------------------------
from app.models.matching import (
    Match,
    MatchBand,
    MatchDirection,
    ScoringConfigKind,
    ScoringConfiguration,
)
from app.models.notifications import (
    Notification,
    NotificationCategory,
    NotificationSeverity,
)
from app.models.pipeline import (
    BLOCKING_SUBMISSION_STATUSES,
    Communication,
    CommunicationChannel,
    CommunicationDirection,
    CommunicationStatus,
    Interview,
    InterviewMode,
    InterviewOutcome,
    Opportunity,
    OpportunityStageHistory,
    Submission,
    SubmissionHistory,
    SubmissionStatus,
)
from app.models.platform import (
    COMMITTABLE_STATES,
    ImportBatch,
    ImportEntity,
    ImportRow,
    ImportStatus,
    RowState,
)
from app.models.scoring import (
    AddressabilityBandEnum,
    OpportunityBandEnum,
    OpportunityScore,
)
from app.models.skills import Skill, normalize_skill

# --- Phase 6: talent -----------------------------------------------------
from app.models.talent import (
    PERSONAL_DOCUMENT_TYPES,
    WORK_AUTHORISATION_TYPES,
    AssessmentStatus,
    AvailabilityStatus,
    Document,
    DocumentExpiryState,
    DocumentType,
    Proficiency,
    Resource,
    ResourceCertification,
    ResourceDocument,
    ResourceExperience,
    ResourceSkill,
    ResourceType,
    VisaStatus,
)

# --- Phase 10: pipeline (imported above) ---------------------------------
# --- Phase 12: platform (notifications land early, in Phase 8) -----------

__all__ = [
    "BLOCKING_SUBMISSION_STATUSES",
    "COMMITTABLE_STATES",
    "PERSONAL_DOCUMENT_TYPES",
    "REALISED_BILLING_STATUSES",
    "WORK_AUTHORISATION_TYPES",
    "Account",
    "AccountRelationship",
    "AccountType",
    "Activity",
    "ActivityType",
    "AddressabilityBandEnum",
    "AssessmentStatus",
    "AuditAction",
    "AuditLog",
    "AvailabilityStatus",
    "Base",
    "BaseEntity",
    "BillingRecord",
    "BillingStatus",
    "Communication",
    "CommunicationChannel",
    "CommunicationDirection",
    "CommunicationStatus",
    "Contact",
    "ContractType",
    "DeadlineState",
    "Deployment",
    "DeploymentStatus",
    "Document",
    "DocumentExpiryState",
    "DocumentType",
    "ImportBatch",
    "ImportEntity",
    "ImportRow",
    "ImportStatus",
    "Interview",
    "InterviewMode",
    "InterviewOutcome",
    "LoginAttempt",
    "Match",
    "MatchBand",
    "MatchDirection",
    "Notification",
    "NotificationCategory",
    "NotificationSeverity",
    "Opportunity",
    "OpportunityBandEnum",
    "OpportunityScore",
    "OpportunityStageHistory",
    "PrioritySource",
    "Proficiency",
    "Project",
    "ProjectStatus",
    "ProjectTechnology",
    "RateUnit",
    "RefreshToken",
    "RelationType",
    "RelationshipStatus",
    "Requirement",
    "RequirementSkill",
    "RequirementSource",
    "RequirementStatus",
    "RequirementStatusHistory",
    "Resource",
    "ResourceCertification",
    "ResourceDocument",
    "ResourceExperience",
    "ResourceSkill",
    "ResourceType",
    "ReviewStatus",
    "RowState",
    "ScoringConfigKind",
    "ScoringConfiguration",
    "Skill",
    "SkillImportance",
    "SoftDeleteEntity",
    "Submission",
    "SubmissionHistory",
    "SubmissionStatus",
    "Technology",
    "User",
    "VisaStatus",
    "WorkMode",
    "normalize_skill",
]
