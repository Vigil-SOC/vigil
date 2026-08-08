"""Serialization schemas for the workflow, skill and agent models."""

from typing import Any, Optional

from pydantic import Field

from core.storage.schemas.base import (
    CoercedBool,
    JsonDict,
    JsonList,
    OptDateTime,
    ORMSchema,
    ZeroFloat,
)


class CustomWorkflowSchema(ORMSchema):
    """CustomWorkflow."""

    workflow_id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    use_case: Optional[str] = None
    trigger_examples: JsonList = Field(default_factory=list)
    phases: JsonList = Field(default_factory=list)
    graph_layout: JsonDict = Field(default_factory=dict)
    is_active: Optional[bool] = None
    created_by: Optional[str] = None
    version: Optional[int] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class WorkflowRunSchema(ORMSchema):
    """A workflow run.

    ``result_summary`` can be large, so list responses use
    :meth:`dump_summary` to leave it out.
    """

    run_id: Optional[str] = None
    workflow_id: Optional[str] = None
    workflow_version: Optional[int] = None
    workflow_source: Optional[str] = None
    workflow_name: Optional[str] = None
    status: Optional[str] = None
    triggered_by: Optional[str] = None
    trigger_context: JsonDict = Field(default_factory=dict)
    started_at: OptDateTime = None
    finished_at: OptDateTime = None
    duration_ms: Optional[int] = None
    total_cost_usd: ZeroFloat = 0.0
    skill_tools_available: JsonList = Field(default_factory=list)
    error: Optional[str] = None
    result_summary: Optional[str] = None

    @classmethod
    def dump_summary(cls, obj: Any, **kwargs: Any) -> dict:
        """Serialize without the potentially-large result summary."""
        return cls.dump(obj, exclude={"result_summary"}, **kwargs)


class WorkflowRunPhaseSchema(ORMSchema):
    """WorkflowRunPhase."""

    run_id: Optional[str] = None
    phase_id: Optional[str] = None
    phase_order: Optional[int] = None
    agent_id: Optional[str] = None
    status: Optional[str] = None
    started_at: OptDateTime = None
    finished_at: OptDateTime = None
    duration_ms: Optional[int] = None
    input_context: JsonDict = Field(default_factory=dict)
    output: JsonDict = Field(default_factory=dict)
    approval_state: Optional[str] = None
    cost_usd: ZeroFloat = 0.0
    error: Optional[str] = None


class SkillSchema(ORMSchema):
    """Skill."""

    skill_id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    input_schema: JsonDict = Field(default_factory=dict)
    output_schema: JsonDict = Field(default_factory=dict)
    required_tools: JsonList = Field(default_factory=list)
    prompt_template: Optional[str] = None
    execution_steps: JsonList = Field(default_factory=list)
    is_active: Optional[bool] = None
    created_by: Optional[str] = None
    version: Optional[int] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class CustomAgentSchema(ORMSchema):
    """CustomAgent."""

    id: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    specialization: Optional[str] = None
    role: Optional[str] = None
    extra_principles: Optional[str] = None
    methodology: Optional[str] = None
    system_prompt_override: Optional[str] = None
    recommended_tools: JsonList = Field(default_factory=list)
    max_tokens: Optional[int] = None
    enable_thinking: Optional[bool] = None
    model: Optional[str] = None
    component_category: Optional[str] = None
    forked_from: Optional[str] = None
    created_by: Optional[str] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class ApprovalActionSchema(ORMSchema):
    """ApprovalAction."""

    action_id: Optional[str] = None
    action_type: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    target: Optional[str] = None
    confidence: ZeroFloat = 0.0
    reason: Optional[str] = None
    evidence: JsonList = Field(default_factory=list)
    created_at: OptDateTime = None
    created_by: Optional[str] = None
    requires_approval: CoercedBool = False
    status: Optional[str] = None
    approved_at: OptDateTime = None
    approved_by: Optional[str] = None
    executed_at: OptDateTime = None
    execution_result: Optional[Any] = None
    rejection_reason: Optional[str] = None
    parameters: JsonDict = Field(default_factory=dict)
    workflow_run_id: Optional[str] = None
    workflow_phase_id: Optional[str] = None


class InvestigationSchema(ORMSchema):
    """Investigation."""

    investigation_id: Optional[str] = None
    case_id: Optional[str] = None
    workflow_id: Optional[str] = None
    trigger_type: Optional[str] = None
    trigger_ids: Optional[Any] = None
    status: Optional[str] = None
    workdir: Optional[str] = None
    current_step: Optional[int] = None
    total_steps: Optional[int] = None
    iteration_count: Optional[int] = None
    max_iterations: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    max_cost_usd: Optional[float] = None
    priority: Optional[str] = None
    created_at: OptDateTime = None
    started_at: OptDateTime = None
    completed_at: OptDateTime = None
    last_activity_at: OptDateTime = None
    max_runtime_seconds: Optional[int] = None
    summary: Optional[str] = None
    proposed_actions: Optional[Any] = None
    master_review_notes: Optional[str] = None
    error_count: Optional[int] = None
    last_error: Optional[str] = None
    current_activity: Optional[str] = None


class InvestigationLogSchema(ORMSchema):
    """InvestigationLog."""

    id: Optional[int] = None
    investigation_id: Optional[str] = None
    timestamp: OptDateTime = None
    event_type: Optional[str] = None
    details: Optional[Any] = None
    tokens_used: Optional[int] = None
