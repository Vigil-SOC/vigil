"""AI decision, LLM, and conversation ORM models."""

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
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
)

from core.storage.models.base import Base
from core.time import utcnow


class AIDecisionLog(Base):
    """
    AI Decision Log - Tracks AI decisions for feedback and learning.

    This model enables human oversight and continuous improvement of AI agents
    by tracking all AI decisions, collecting human feedback, and measuring accuracy.
    """

    __tablename__ = "ai_decision_logs"

    # Primary key
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)

    # Decision context
    agent_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    workflow_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    finding_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("findings.finding_id", ondelete="CASCADE"), nullable=True
    )
    case_id: Mapped[Optional[str]] = mapped_column(
        String(50), ForeignKey("cases.case_id", ondelete="CASCADE"), nullable=True
    )

    # AI's decision
    decision_type: Mapped[str] = mapped_column(String(50), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_action: Mapped[str] = mapped_column(Text, nullable=False)

    # Additional decision metadata
    decision_metadata: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # Human feedback
    human_reviewer: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    human_decision: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    feedback_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Grading (0-1 scale)
    accuracy_grade: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    reasoning_grade: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    action_appropriateness: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    # Outcome tracking
    actual_outcome: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    time_saved_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Timestamps
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    feedback_timestamp: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    # Indexes
    __table_args__ = (
        Index("idx_ai_decision_agent_id", "agent_id"),
        Index("idx_ai_decision_finding_id", "finding_id"),
        Index("idx_ai_decision_case_id", "case_id"),
        Index("idx_ai_decision_timestamp", "timestamp"),
        Index("idx_ai_decision_human_decision", "human_decision"),
        Index("idx_ai_decision_actual_outcome", "actual_outcome"),
    )


class LLMInteractionLog(Base):
    """Durable audit log of Claude API interactions for chain-of-thought visibility.

    One row per Anthropic API response (per tool-use iteration). Captures
    full untruncated thinking blocks, tool call chains, token usage, and
    cost so analysts can audit AI decision-making post-hoc.
    """

    __tablename__ = "llm_interaction_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    interaction_id: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    session_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    agent_id: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    investigation_id: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    request_messages: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    system_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    thinking_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    thinking_budget: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    thinking_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    response_content: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    tool_results: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    stop_reason: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # NULL = the call could not be priced; 0 = priced and genuinely free (#1115).
    cost_usd: Mapped[Optional[float]] = mapped_column(Numeric(10, 6), nullable=True)
    # Per-token USD that produced cost_usd, frozen at write (#1190). Float, not
    # Numeric(10, 6): that scale fits a call total, and a cache rate below 1e-6
    # would round to zero. NULL alongside cost_usd when the call is unpriced —
    # the 0.0 defaults for pricing_source "unknown" are not a price.
    input_cost_per_token: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    output_cost_per_token: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    cache_read_cost_per_token: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    cache_write_cost_per_token: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    rates_fetched_at: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Bifrost virtual-key attribution (#186). Stores the VK the call was
    # made under so we can group spend per-tenant once Vigil grows a
    # tenant model. Empty / NULL for calls made before the budget feature
    # was enabled or while running in DEV_MODE / LLM_BUDGET_UNLIMITED.
    virtual_key_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("idx_llm_interaction_session", "session_id"),
        Index("idx_llm_interaction_agent", "agent_id"),
        Index("idx_llm_interaction_investigation", "investigation_id"),
        Index("idx_llm_interaction_created", "created_at"),
        Index("idx_llm_interaction_vk", "virtual_key_id", "created_at"),
    )


class LLMProviderConfig(Base):
    """LLM provider configuration (Anthropic, OpenAI, Ollama, ...).

    Keys are not stored here — `api_key_ref` points to a secrets_manager key.
    See infra/database/init/09_llm_providers.sql for the table definition.
    """

    __tablename__ = "llm_provider_configs"

    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    api_key_ref: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    default_model: Mapped[str] = mapped_column(String(200), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    last_test_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_test_success: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )

    __table_args__ = (
        Index("idx_llm_provider_type", "provider_type"),
        Index("idx_llm_provider_active", "is_active"),
        # Partial unique index — enforces "one default per provider_type"
        # for non-Docker deployments too (Base.metadata.create_all path).
        # Mirrors the SQL in infra/database/init/07_llm_providers.sql.
        Index(
            "llm_provider_default_per_type",
            "provider_type",
            unique=True,
            postgresql_where=text("is_default = TRUE"),
        ),
    )


class AIModelConfig(Base):
    """Per-component AI model assignment (GH #89).

    Each row maps a logical component (chat_default, triage, investigation,
    summarization, reporting) to a
    (provider, model) pair. Components without a row fall back to the
    `chat_default` row; if that is missing, callers fall back to the
    default active provider (of any type) and its default_model.

    See infra/database/init/10_ai_model_configs.sql for the table definition.
    """

    __tablename__ = "ai_model_configs"

    component: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("llm_provider_configs.provider_id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    settings: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    updated_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )

    __table_args__ = (Index("idx_ai_model_configs_provider", "provider_id"),)


class Conversation(Base):
    """Cross-device, per-analyst persistent chat conversation.

    The Claude.ai-style history store for the console chat dock: a
    listable, reopenable conversation owned by an analyst. The primary key
    IS the frontend ``session_id`` so reopening a conversation continues
    the same session.

    This is distinct from ``llm_interaction_logs``, which remains the
    per-API-call compliance audit log (system-of-record). Deleting a
    conversation here never touches that audit trail.
    """

    __tablename__ = "conversations"

    # = the frontend session_id (see Chat.tsx newSessionId()).
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    # Owner. No hard FK: DEV_MODE's mock fallback user is not persisted, so
    # a FK would reject those rows. user-admin-default is the seeded dev id.
    user_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    title: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    agent_id: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Denormalized for the list view (avoids COUNT per row).
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
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
    # Sort key for the history list; null until the first message lands.
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    messages: Mapped[List["ChatMessage"]] = relationship(
        "ChatMessage",
        back_populates="conversation",
        order_by="ChatMessage.seq",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("idx_conversations_user_last_msg", "user_id", "last_message_at"),
        Index("idx_conversations_user_archived", "user_id", "archived"),
    )


class ChatMessage(Base):
    """A single message within a :class:`Conversation`.

    Full-fidelity copy of what the analyst saw live: visible text, extended
    thinking, and the tool-call chain captured from the stream. ``complete``
    is False for assistant turns that were aborted or errored mid-stream, so
    a reopened chat renders exactly what was on screen.
    """

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(120),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Order within the conversation (0-based, dense). Unique per conversation.
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    thinking: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tool_calls: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    complete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    model: Mapped[Optional[str]] = mapped_column(String(80), nullable=True)
    input_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[float] = mapped_column(
        Numeric(10, 6), nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, server_default="now()"
    )

    conversation: Mapped["Conversation"] = relationship(
        "Conversation", back_populates="messages"
    )

    __table_args__ = (
        UniqueConstraint("conversation_id", "seq", name="uq_chat_messages_conv_seq"),
        Index("idx_chat_messages_conversation", "conversation_id"),
    )
