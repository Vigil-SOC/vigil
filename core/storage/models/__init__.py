"""SQLAlchemy ORM models for Vigil SOC.

Domain modules live beside the matching schemas in
``core.storage.schemas``. Public names are re-exported here so
``from core.storage.models import ...`` stays valid. Import from this
package, not a submodule, so every mapped class registers on
``Base.metadata``.
"""

from core.storage.models.ai import (
    AIDecisionLog,
    AIModelConfig,
    ChatMessage,
    Conversation,
    LLMInteractionLog,
    LLMProviderConfig,
)
from core.storage.models.auth import McpCredential, Role, User
from core.storage.models.base import Base, JSONBList, case_findings
from core.storage.models.case import Case
from core.storage.models.case_entities import (
    CaseAttachment,
    CaseAuditLog,
    CaseClosureInfo,
    CaseComment,
    CaseEscalation,
    CaseEvidence,
    CaseIOC,
    CaseMetrics,
    CaseNotification,
    CaseRelationship,
    CaseSLA,
    CaseTask,
    CaseTemplate,
    CaseWatcher,
    SLAPolicy,
)
from core.storage.models.config import (
    ConfigAuditLog,
    FederationSource,
    IntegrationConfig,
    SharedIOC,
    SketchMapping,
    SystemConfig,
    ThreatIndicator,
    UserPreference,
)
from core.storage.models.episodic import (
    EpisodicDistilFailure,
    EpisodicDistilMarker,
    EpisodicGap,
    EpisodicReadLog,
    EpisodicSighting,
    EpisodicVerdict,
    EpisodicVerdictSource,
)
from core.storage.models.exclusion import IpExclusion
from core.storage.models.finding import Finding, FindingMitrePrediction
from core.storage.models.workflow import (
    IN_FLIGHT_INVESTIGATION_STATUSES,
    LIVE_INVESTIGATION_STATUSES,
    ApprovalAction,
    CustomAgent,
    CustomWorkflow,
    IntakeTrigger,
    Investigation,
    InvestigationLog,
    WorkflowRun,
    WorkflowRunPhase,
)

__all__ = [
    "AIDecisionLog",
    "AIModelConfig",
    "ApprovalAction",
    "Base",
    "Case",
    "CaseAttachment",
    "CaseAuditLog",
    "CaseClosureInfo",
    "CaseComment",
    "CaseEscalation",
    "CaseEvidence",
    "CaseIOC",
    "CaseMetrics",
    "CaseNotification",
    "CaseRelationship",
    "CaseSLA",
    "CaseTask",
    "CaseTemplate",
    "CaseWatcher",
    "ChatMessage",
    "ConfigAuditLog",
    "Conversation",
    "CustomAgent",
    "CustomWorkflow",
    "EpisodicDistilFailure",
    "EpisodicDistilMarker",
    "EpisodicGap",
    "EpisodicReadLog",
    "EpisodicSighting",
    "EpisodicVerdict",
    "EpisodicVerdictSource",
    "FederationSource",
    "Finding",
    "FindingMitrePrediction",
    "IntakeTrigger",
    "IN_FLIGHT_INVESTIGATION_STATUSES",
    "IntegrationConfig",
    "IpExclusion",
    "Investigation",
    "InvestigationLog",
    "JSONBList",
    "LIVE_INVESTIGATION_STATUSES",
    "LLMInteractionLog",
    "LLMProviderConfig",
    "McpCredential",
    "Role",
    "SLAPolicy",
    "SharedIOC",
    "SketchMapping",
    "SystemConfig",
    "ThreatIndicator",
    "User",
    "UserPreference",
    "WorkflowRun",
    "WorkflowRunPhase",
    "case_findings",
]
