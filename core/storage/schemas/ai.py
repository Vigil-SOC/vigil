"""Serialization schemas for the AI and LLM models."""

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


class AIDecisionLogSchema(ORMSchema):
    """AIDecisionLog."""

    id: Optional[int] = None
    decision_id: Optional[str] = None
    agent_id: Optional[str] = None
    workflow_id: Optional[str] = None
    finding_id: Optional[str] = None
    case_id: Optional[str] = None
    decision_type: Optional[str] = None
    confidence_score: Optional[float] = None
    reasoning: Optional[str] = None
    recommended_action: Optional[str] = None
    decision_metadata: Optional[Any] = None
    human_reviewer: Optional[str] = None
    human_decision: Optional[str] = None
    feedback_comment: Optional[str] = None
    accuracy_grade: Optional[float] = None
    reasoning_grade: Optional[float] = None
    action_appropriateness: Optional[float] = None
    actual_outcome: Optional[str] = None
    time_saved_minutes: Optional[int] = None
    timestamp: OptDateTime = None
    feedback_timestamp: OptDateTime = None


class LLMInteractionLogSchema(ORMSchema):
    """LLMInteractionLog."""

    id: Optional[int] = None
    interaction_id: Optional[str] = None
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    investigation_id: Optional[str] = None
    created_at: OptDateTime = None
    model: Optional[str] = None
    thinking_enabled: Optional[bool] = None
    has_thinking: CoercedBool = Field(default=None, validation_alias="thinking_content")
    has_tools: CoercedBool = Field(default=None, validation_alias="tool_calls")
    stop_reason: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: ZeroFloat = 0.0
    duration_ms: Optional[int] = None
    error: Optional[str] = None
    request_messages: Optional[Any] = None
    system_prompt: Optional[str] = None
    thinking_budget: Optional[int] = None
    thinking_content: Optional[str] = None
    response_content: Optional[str] = None
    tool_calls: Optional[Any] = None
    tool_results: Optional[Any] = None
    cache_read_tokens: Optional[int] = None
    cache_creation_tokens: Optional[int] = None

    @classmethod
    def dump_summary(cls, obj: Any, **kwargs: Any) -> dict:
        """Serialize without the heavy detail-only fields."""
        return cls.dump(
            obj,
            exclude={
                "request_messages",
                "system_prompt",
                "thinking_budget",
                "thinking_content",
                "response_content",
                "tool_calls",
                "tool_results",
                "cache_read_tokens",
                "cache_creation_tokens",
            },
            **kwargs
        )


class LLMProviderConfigSchema(ORMSchema):
    """An LLM provider's configuration.

    ``dump`` redacts ``api_key_ref`` and reports only whether a key is set;
    callers that genuinely need the reference must ask for it explicitly via
    :meth:`dump_with_secrets`. Redacting by default means a missed call site
    leaks nothing.
    """

    provider_id: Optional[str] = None
    provider_type: Optional[str] = None
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key_ref: Optional[str] = None
    has_api_key: CoercedBool = Field(default=False, validation_alias="api_key_ref")
    default_model: Optional[str] = None
    is_active: Optional[bool] = None
    is_default: Optional[bool] = None
    config: JsonDict = Field(default_factory=dict)
    last_test_at: OptDateTime = None
    last_test_success: Optional[bool] = None
    last_error: Optional[str] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None

    @classmethod
    def dump(cls, obj: Any, **kwargs: Any) -> dict:
        """Serialize with the API key reference redacted."""
        data = super().dump(obj, **kwargs)
        data["api_key_ref"] = None
        return data

    @classmethod
    def dump_with_secrets(cls, obj: Any, **kwargs: Any) -> dict:
        """Serialize including the API key reference."""
        return super().dump(obj, **kwargs)


class AIModelConfigSchema(ORMSchema):
    """AIModelConfig."""

    component: Optional[str] = None
    provider_id: Optional[str] = None
    model_id: Optional[str] = None
    settings: JsonDict = Field(default_factory=dict)
    updated_by: Optional[str] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None


class ChatMessageSchema(ORMSchema):
    """ChatMessage."""

    id: Optional[int] = None
    conversation_id: Optional[str] = None
    seq: Optional[int] = None
    role: Optional[str] = None
    content: Optional[str] = None
    thinking: Optional[str] = None
    tool_calls: JsonList = Field(default_factory=list)
    complete: Optional[bool] = None
    model: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: ZeroFloat = 0.0
    created_at: OptDateTime = None


class ConversationSummarySchema(ORMSchema):
    """A conversation without its messages, for the history list.

    ``messages`` is left undeclared rather than excluded at dump time.
    Pydantic populates every declared field while validating, so an excluded
    relationship would still be lazy-loaded — a query per row — before being
    dropped from the output.
    """

    id: Optional[str] = None
    user_id: Optional[str] = None
    title: Optional[str] = None
    agent_id: Optional[str] = None
    model: Optional[str] = None
    archived: Optional[bool] = None
    message_count: Optional[int] = None
    created_at: OptDateTime = None
    updated_at: OptDateTime = None
    last_message_at: OptDateTime = None


class ConversationSchema(ConversationSummarySchema):
    """A conversation with its messages inlined, for the detail endpoint."""

    messages: list[ChatMessageSchema] = Field(default_factory=list)
