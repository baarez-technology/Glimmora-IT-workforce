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

# --- Phase 10: pipeline --------------------------------------------------
# --- Phase 11: delivery --------------------------------------------------
# --- Phase 12: platform --------------------------------------------------

__all__ = [
    "PERSONAL_DOCUMENT_TYPES",
    "WORK_AUTHORISATION_TYPES",
    "Account",
    "AccountRelationship",
    "AccountType",
    "Activity",
    "ActivityType",
    "AssessmentStatus",
    "AuditAction",
    "AuditLog",
    "AvailabilityStatus",
    "Base",
    "BaseEntity",
    "Contact",
    "ContractType",
    "DeadlineState",
    "Document",
    "DocumentExpiryState",
    "DocumentType",
    "LoginAttempt",
    "Match",
    "MatchBand",
    "MatchDirection",
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
    "ScoringConfigKind",
    "ScoringConfiguration",
    "Skill",
    "SkillImportance",
    "SoftDeleteEntity",
    "Technology",
    "User",
    "VisaStatus",
    "WorkMode",
    "normalize_skill",
]
