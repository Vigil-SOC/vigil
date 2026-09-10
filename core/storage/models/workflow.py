"""Investigation, workflow, skill, and agent ORM models."""

import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from core.storage.models.base import Base
from core.time import utcnow


class Investigation(Base):
    """Tracks an autonomous investigation assignment managed by the orchestrator."""

    __tablename__ = "investigations"

    investigation_id: Mapped[str] = mapped_column(String(60), primary_key=True)
    case_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="SET NULL"), nullable=True
    )
    workflow_id: Mapped[str] = mapped_column(String(50), nullable=False)

    trigger_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # Finding ids, not objects (#554). Both writers build a list of
    # ``findings.finding_id`` strings -- services/daemon/orchestrator.py:407
    # (``[f.get("finding_id") for f in findings ...]``) and :942
    # (``finding_ids[:10]``) -- and every reader treats the elements as those
    # strings: services/api/routers/orchestrator.py:484 tests them for set
    # membership against ``Finding.finding_id``, and orchestrator.py:1098 writes
    # ``trigger_ids[0]`` straight into ``AIDecisionLog.finding_id``, a
    # String(50) FK. The old ``List[dict]`` annotation matched no writer or
    # reader. Storage stays JSONB; promoting the column is deferred to #468.
    trigger_ids: Mapped[List[str]] = mapped_column(JSONB, nullable=False, default=list)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")

    workdir: Mapped[str] = mapped_column(String(255), nullable=False)

    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    iteration_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_iterations: Mapped[int] = mapped_column(Integer, nullable=False, default=50)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=5.0)

    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    max_runtime_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3600
    )

    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    proposed_actions: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    master_review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_activity: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_investigation_status", "status"),
        Index("idx_investigation_case_id", "case_id"),
        Index("idx_investigation_priority", "priority"),
        Index("idx_investigation_created_at", "created_at"),
        Index("idx_investigation_workflow_id", "workflow_id"),
    )


class InvestigationLog(Base):
    """Append-only audit log for investigation agent actions."""

    __tablename__ = "investigation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    investigation_id: Mapped[str] = mapped_column(
        String(60),
        ForeignKey("investigations.investigation_id", ondelete="CASCADE"),
        nullable=False,
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    details: Mapped[dict] = mapped_column(JSONB, nullable=False, default={})
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_inv_log_investigation_id", "investigation_id"),
        Index("idx_inv_log_timestamp", "timestamp"),
        Index("idx_inv_log_event_type", "event_type"),
    )


class CustomWorkflow(Base):
    """
    Custom Workflow Model - User-created multi-agent workflow definitions.

    File-based WORKFLOW.md definitions remain supported separately. This table
    holds workflows created/edited via the Workflow Builder UI.
    """

    __tablename__ = "custom_workflows"

    workflow_id: Mapped[str] = mapped_column(String(100), primary_key=True)

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    use_case: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    trigger_examples: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    phases: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    graph_layout: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        server_default="now()",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default="now()",
    )

    __table_args__ = (
        Index("idx_custom_workflows_active", "is_active"),
        Index("idx_custom_workflows_created_by", "created_by"),
        Index("idx_custom_workflows_name", "name"),
    )


class WorkflowRun(Base):
    """Per-invocation record of ``execute_workflow`` (#127).

    One row per `/api/workflows/{id}/execute` call, used for history +
    audit. The parent row always exists; `workflow_run_phases` rows are
    reserved for phase-by-phase execution (#128) and may be absent.
    """

    __tablename__ = "workflow_runs"

    run_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(Text, nullable=False)
    workflow_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    workflow_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="file", server_default="file"
    )
    workflow_name: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String(16), nullable=False)
    triggered_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    trigger_context: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    total_cost_usd: Mapped[float] = mapped_column(
        Numeric(10, 4), nullable=False, default=0, server_default="0"
    )
    result_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    skill_tools_available: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Set when an operator removes the run from History. The row and its ledger stay.
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_workflow_runs_workflow_id", "workflow_id", "started_at"),
        Index("idx_workflow_runs_started_at", "started_at"),
        # find_run_by_trigger reads this on every handoff a run hands over -- once
        # when the hunt journals it and again on the terminal that re-carries it --
        # to decide whether a root-cause run was already teed up. Without it that is
        # a sequential scan that grows with run history.
        Index("idx_workflow_runs_triggered_by", "triggered_by", "started_at"),
    )


class WorkflowRunPhase(Base):
    """Per-phase record within a workflow run.

    Reserved for phase-by-phase execution (#128). The table ships with
    the schema so the audit story is complete, but no rows are written
    until phase-level execution lands.
    """

    __tablename__ = "workflow_run_phases"

    run_id: Mapped[str] = mapped_column(
        String(80),
        ForeignKey("workflow_runs.run_id", ondelete="CASCADE"),
        primary_key=True,
    )
    phase_id: Mapped[str] = mapped_column(Text, primary_key=True)
    phase_order: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    input_context: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    output: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    approval_state: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    cost_usd: Mapped[float] = mapped_column(
        Numeric(10, 4), nullable=False, default=0, server_default="0"
    )
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("idx_workflow_run_phases_run_id", "run_id", "phase_order"),)


class ApprovalAction(Base):
    """Pending human-in-the-loop approval (#128).

    Supersedes the JSON-file persistence that ApprovalService used to
    do. Workflow phase-level approvals link back to the paused run via
    ``workflow_run_id`` + ``workflow_phase_id``. Non-workflow approvals
    (daemon containment actions, etc.) leave those columns null.
    """

    __tablename__ = "approval_actions"

    action_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    target: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(
        Numeric(4, 3), nullable=False, default=0, server_default="0"
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    execution_result: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    parameters: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    workflow_run_id: Mapped[Optional[str]] = mapped_column(
        String(80),
        ForeignKey("workflow_runs.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    workflow_phase_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Irreversible rows always need a human; default keeps isolate/block auto-approve.
    reversibility: Mapped[str] = mapped_column(
        String(16), nullable=False, default="reversible", server_default="reversible"
    )
    idempotency_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_approval_actions_status_created", "status", "created_at"),
        Index("idx_approval_actions_workflow_run", "workflow_run_id"),
        # Unique among non-failed rows so a failed isolate can be retried (#827).
        Index(
            "uq_approval_actions_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL AND status <> 'failed'"),
        ),
    )


class Skill(Base):
    """Skill model - reusable, parameterized SOC capability (detection,
    enrichment, response, reporting) that agents and workflows can invoke."""

    __tablename__ = "skills"

    skill_id: Mapped[str] = mapped_column(String(32), primary_key=True)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)

    # JSON Schema for skill parameters (the inputs the skill accepts).
    input_schema: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # JSON Schema for skill output.
    output_schema: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # MCP tool names required by this skill
    # (e.g. ["splunk.search", "virustotal.hash_lookup"]).
    required_tools: Mapped[List[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # LLM instructions; may contain {{param}} placeholders.
    prompt_template: Mapped[str] = mapped_column(Text, nullable=False)
    # Ordered execution steps (tool calls / prompts / transforms) — interpreted
    # by the future skill-execution worker.
    execution_steps: Mapped[List[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    created_by: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        server_default="now()",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default="now()",
    )

    __table_args__ = (
        Index("idx_skill_category", "category"),
        Index("idx_skill_is_active", "is_active"),
        Index(
            "idx_skill_name_trgm",
            "name",
            postgresql_ops={"name": "gin_trgm_ops"},
            postgresql_using="gin",
        ),
    )

    @staticmethod
    def generate_skill_id() -> str:
        """Generate a new skill_id in the form s-YYYYMMDD-XXXXXXXX."""
        ts = utcnow().strftime("%Y%m%d")
        return f"s-{ts}-{uuid.uuid4().hex[:8].upper()}"


class CustomAgent(Base):
    """User-defined SOC agent created via the Agent Builder UI."""

    __tablename__ = "custom_agents"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    icon: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    color: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    specialization: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    role: Mapped[str] = mapped_column(Text, nullable=False)
    extra_principles: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    methodology: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    system_prompt_override: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    recommended_tools: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    max_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=4096, server_default="4096"
    )
    enable_thinking: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    component_category: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="investigation",
        server_default="investigation",
    )
    # Origin agent ID this row was forked from (built-in id like "reporter" or
    # another custom id). Null for agents authored from scratch. Breadcrumb
    # only — no FK, because built-ins live in code, not a table.
    forked_from: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default="now()",
    )

    __table_args__ = (Index("idx_custom_agents_updated_at", "updated_at"),)
