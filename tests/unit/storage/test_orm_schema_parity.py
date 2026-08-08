"""Serialized-output parity between the ORM-mode schemas and the contract.

``tests/fixtures/orm_serialization_golden.json`` is a frozen capture of the
hand-written ``to_dict()`` output it replaced, taken from the commit before
the schemas landed. It is not regenerated from the schemas — that would be
circular. Edit it by hand, in a commit that says why, only when a contract
change is intended.

Each schema is registered with the builder states and dump variants it must
reproduce; the test then asserts key-for-key equality against the capture.
"""

import json
import pathlib

import pytest

import core.storage.models as models
from core.storage.schemas.ai import (
    AIDecisionLogSchema,
    AIModelConfigSchema,
    ChatMessageSchema,
    ConversationSchema,
    ConversationSummarySchema,
    LLMInteractionLogSchema,
    LLMProviderConfigSchema,
)
from core.storage.schemas.auth import RoleSchema, UserSchema
from core.storage.schemas.case import CaseSchema, CaseWithFindingsSchema
from core.storage.schemas.case_entities import (
    CaseAttachmentSchema,
    CaseAuditLogSchema,
    CaseClosureInfoSchema,
    CaseCommentSchema,
    CaseEscalationSchema,
    CaseEvidenceSchema,
    CaseIOCSchema,
    CaseMetricsSchema,
    CaseNotificationSchema,
    CaseRelationshipSchema,
    CaseSLASchema,
    CaseTaskSchema,
    CaseTemplateSchema,
    CaseWatcherSchema,
    SLAPolicySchema,
)
from core.storage.schemas.config import (
    AttackLayerSchema,
    ConfigAuditLogSchema,
    FederationSourceSchema,
    IntegrationConfigSchema,
    SharedIOCSchema,
    SketchMappingSchema,
    SystemConfigSchema,
    ThreatIndicatorSchema,
    UserPreferenceSchema,
)
from core.storage.schemas.finding import FindingSchema
from core.storage.schemas.workflow import (
    ApprovalActionSchema,
    CustomAgentSchema,
    CustomWorkflowSchema,
    InvestigationLogSchema,
    InvestigationSchema,
    SkillSchema,
    WorkflowRunPhaseSchema,
    WorkflowRunSchema,
)
from tests.unit.storage.orm_sample_instances import build_empty, build_populated, build_related

pytestmark = pytest.mark.unit

GOLDEN_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "fixtures"
    / "orm_serialization_golden.json"
)


@pytest.fixture(scope="module")
def golden():
    with GOLDEN_PATH.open() as fh:
        return json.load(fh)


def _standard(model, schema):
    """Registry entry for a model with a single unconditional shape."""
    return {
        "model": model,
        "cases": {
            "populated.to_dict.default": schema.dump,
            "empty.to_dict.default": schema.dump,
        },
    }


def _variant(model, cases):
    return {"model": model, "cases": cases}


# model name -> {golden case key: callable(instance) -> dict}
SCHEMA_REGISTRY: dict[str, dict] = {
    # Core models, whose serialized shape is gated by a flag.
    "Finding": _variant(
        models.Finding,
        {
            "populated.to_dict.with_embedding": FindingSchema.dump,
            "populated.to_dict.without_embedding": FindingSchema.dump_summary,
            "empty.to_dict.with_embedding": FindingSchema.dump,
            "empty.to_dict.without_embedding": FindingSchema.dump_summary,
        },
    ),
    "Case": _variant(
        models.Case,
        {
            "populated.to_dict.with_finding_ids": CaseSchema.dump,
            "populated.to_dict.with_findings": CaseWithFindingsSchema.dump,
            "empty.to_dict.with_finding_ids": CaseSchema.dump,
            "empty.to_dict.with_findings": CaseWithFindingsSchema.dump,
            "related.to_dict.with_finding_ids": CaseSchema.dump,
            "related.to_dict.with_findings": CaseWithFindingsSchema.dump,
        },
    ),
    # Case sub-entities.
    "SLAPolicy": _standard(models.SLAPolicy, SLAPolicySchema),
    "CaseSLA": _standard(models.CaseSLA, CaseSLASchema),
    "CaseComment": _standard(models.CaseComment, CaseCommentSchema),
    "CaseWatcher": _standard(models.CaseWatcher, CaseWatcherSchema),
    "CaseEvidence": _standard(models.CaseEvidence, CaseEvidenceSchema),
    "CaseIOC": _standard(models.CaseIOC, CaseIOCSchema),
    "CaseTask": _standard(models.CaseTask, CaseTaskSchema),
    "CaseTemplate": _standard(models.CaseTemplate, CaseTemplateSchema),
    "CaseRelationship": _standard(models.CaseRelationship, CaseRelationshipSchema),
    "CaseMetrics": _standard(models.CaseMetrics, CaseMetricsSchema),
    "CaseAttachment": _standard(models.CaseAttachment, CaseAttachmentSchema),
    "CaseClosureInfo": _standard(models.CaseClosureInfo, CaseClosureInfoSchema),
    "CaseEscalation": _standard(models.CaseEscalation, CaseEscalationSchema),
    "CaseAuditLog": _standard(models.CaseAuditLog, CaseAuditLogSchema),
    "CaseNotification": _standard(models.CaseNotification, CaseNotificationSchema),
    # Users and roles.
    "User": _standard(models.User, UserSchema),
    "Role": _standard(models.Role, RoleSchema),
    # Workflows, skills and agents.
    "CustomWorkflow": _standard(models.CustomWorkflow, CustomWorkflowSchema),
    "WorkflowRunPhase": _standard(models.WorkflowRunPhase, WorkflowRunPhaseSchema),
    "Skill": _standard(models.Skill, SkillSchema),
    "CustomAgent": _standard(models.CustomAgent, CustomAgentSchema),
    "ApprovalAction": _standard(models.ApprovalAction, ApprovalActionSchema),
    "Investigation": _standard(models.Investigation, InvestigationSchema),
    "InvestigationLog": _standard(models.InvestigationLog, InvestigationLogSchema),
    "WorkflowRun": _variant(
        models.WorkflowRun,
        {
            "populated.to_dict.with_result": WorkflowRunSchema.dump,
            "populated.to_dict.without_result": WorkflowRunSchema.dump_summary,
            "empty.to_dict.with_result": WorkflowRunSchema.dump,
            "empty.to_dict.without_result": WorkflowRunSchema.dump_summary,
        },
    ),
    # AI and LLM bookkeeping.
    "AIDecisionLog": _standard(models.AIDecisionLog, AIDecisionLogSchema),
    "AIModelConfig": _standard(models.AIModelConfig, AIModelConfigSchema),
    "ChatMessage": _standard(models.ChatMessage, ChatMessageSchema),
    "LLMProviderConfig": _variant(
        models.LLMProviderConfig,
        {
            "populated.to_dict.with_secrets": LLMProviderConfigSchema.dump_with_secrets,
            "populated.to_dict.without_secrets": LLMProviderConfigSchema.dump,
            "empty.to_dict.with_secrets": LLMProviderConfigSchema.dump_with_secrets,
            "empty.to_dict.without_secrets": LLMProviderConfigSchema.dump,
        },
    ),
    "LLMInteractionLog": _variant(
        models.LLMInteractionLog,
        {
            "populated.to_dict.default": LLMInteractionLogSchema.dump,
            "populated.to_summary_dict.default": LLMInteractionLogSchema.dump_summary,
            "empty.to_dict.default": LLMInteractionLogSchema.dump,
            "empty.to_summary_dict.default": LLMInteractionLogSchema.dump_summary,
        },
    ),
    "Conversation": _variant(
        models.Conversation,
        {
            "populated.to_dict.default": ConversationSchema.dump,
            "populated.to_summary_dict.default": ConversationSummarySchema.dump,
            "empty.to_dict.default": ConversationSchema.dump,
            "empty.to_summary_dict.default": ConversationSummarySchema.dump,
            "related.to_dict.default": ConversationSchema.dump,
            "related.to_summary_dict.default": ConversationSummarySchema.dump,
        },
    ),
    # Configuration and threat intel.
    "SystemConfig": _standard(models.SystemConfig, SystemConfigSchema),
    "UserPreference": _standard(models.UserPreference, UserPreferenceSchema),
    "IntegrationConfig": _standard(models.IntegrationConfig, IntegrationConfigSchema),
    "ConfigAuditLog": _standard(models.ConfigAuditLog, ConfigAuditLogSchema),
    "FederationSource": _standard(models.FederationSource, FederationSourceSchema),
    "SharedIOC": _standard(models.SharedIOC, SharedIOCSchema),
    "ThreatIndicator": _standard(models.ThreatIndicator, ThreatIndicatorSchema),
    "SketchMapping": _standard(models.SketchMapping, SketchMappingSchema),
    "AttackLayer": _standard(models.AttackLayer, AttackLayerSchema),
}


def _builder_for(state, model_name, model):
    if state == "populated":
        return build_populated(model_name, model)
    if state == "empty":
        return build_empty(model)
    if state == "related":
        return build_related(model_name, model)
    raise AssertionError(f"unknown builder state {state!r}")


def _registered_cases():
    for model_name, spec in sorted(SCHEMA_REGISTRY.items()):
        for case_key in sorted(spec["cases"]):
            yield model_name, case_key


@pytest.mark.parametrize("model_name,case_key", list(_registered_cases()))
def test_schema_matches_golden_contract(golden, model_name, case_key):
    """The schema's dump must equal the captured contract, key for key."""
    spec = SCHEMA_REGISTRY[model_name]
    expected = golden[model_name][case_key]

    state = case_key.split(".", 1)[0]
    instance = _builder_for(state, model_name, spec["model"])
    actual = spec["cases"][case_key](instance)

    assert set(actual) == set(expected), (
        f"{model_name}[{case_key}] key mismatch: "
        f"missing={sorted(set(expected) - set(actual))} "
        f"unexpected={sorted(set(actual) - set(expected))}"
    )
    assert actual == expected


def test_golden_fixture_is_present(golden):
    assert golden, "golden serialization fixture is empty"


def test_registry_covers_every_model(golden):
    """Every model in the capture must have a schema behind it.

    Guards against a model keeping an unreplaced serializer, or a new model
    landing without a declared contract.
    """
    missing = sorted(set(golden) - set(SCHEMA_REGISTRY))
    assert not missing, f"models with no registered schema: {missing}"


def test_registry_covers_every_captured_case(golden):
    """Every captured shape must be asserted, not just one per model."""
    gaps = {
        name: sorted(set(golden[name]) - set(spec["cases"]))
        for name, spec in SCHEMA_REGISTRY.items()
        if set(golden[name]) - set(spec["cases"])
    }
    assert not gaps, f"captured cases with no parity assertion: {gaps}"
