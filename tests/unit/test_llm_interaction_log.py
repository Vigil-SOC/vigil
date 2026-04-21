"""Unit tests for LLM reasoning-trace persistence (GH #79).

Covers:
- LLMInteractionLog model registration + to_dict / to_summary_dict shape
- ClaudeService serialization helpers (static methods — no DB, no API)
- ClaudeService._persist_interaction graceful failure when DB is unavailable
"""

from datetime import datetime


class TestLLMInteractionLogModel:
    """Shape-level checks that don't require a live database."""

    def test_model_registered_in_metadata(self):
        """llm_interaction_logs must be in Base.metadata so create_all creates it."""
        from database.models import Base
        import database.connection  # noqa: F401 — side-effect import

        assert "llm_interaction_logs" in Base.metadata.tables

    def test_to_summary_dict_has_no_heavy_fields(self):
        """List endpoints must not leak heavy text/JSONB columns."""
        from database.models import LLMInteractionLog

        row = LLMInteractionLog(
            interaction_id="abc-123",
            session_id="session-1",
            agent_id="investigator",
            investigation_id=None,
            created_at=datetime.utcnow(),
            model="claude-sonnet-4-5",
            request_messages=[{"role": "user", "content": "hi"}],
            thinking_enabled=True,
            thinking_budget=10000,
            thinking_content="long reasoning " * 100,
            response_content="response text",
            tool_calls=[{"type": "tool_use", "id": "1", "name": "x", "input": {}}],
            tool_results=[],
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=20,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=0.001,
            duration_ms=1234,
        )
        summary = row.to_summary_dict()

        # heavy fields absent
        assert "thinking_content" not in summary
        assert "response_content" not in summary
        assert "request_messages" not in summary
        assert "tool_calls" not in summary
        assert "tool_results" not in summary
        # flags present
        assert summary["has_thinking"] is True
        assert summary["has_tools"] is True
        assert summary["interaction_id"] == "abc-123"
        assert summary["input_tokens"] == 10
        assert summary["output_tokens"] == 20

    def test_to_dict_includes_heavy_fields(self):
        """Detail endpoint must expose the full interaction."""
        from database.models import LLMInteractionLog

        row = LLMInteractionLog(
            interaction_id="abc-123",
            session_id="session-1",
            agent_id=None,
            investigation_id=None,
            created_at=datetime.utcnow(),
            model="claude-sonnet-4-5",
            request_messages=[{"role": "user", "content": "hi"}],
            thinking_enabled=False,
            thinking_budget=None,
            thinking_content=None,
            response_content="hello world",
            tool_calls=[],
            tool_results=[],
            stop_reason="end_turn",
            input_tokens=5,
            output_tokens=2,
            cost_usd=0.0,
            duration_ms=50,
        )
        full = row.to_dict()
        assert "thinking_content" in full
        assert "response_content" in full
        assert "request_messages" in full
        assert "tool_calls" in full
        assert "tool_results" in full
        assert full["has_thinking"] is False
        assert full["has_tools"] is False


class TestSerializationHelpers:
    """Pure-function helpers — no mocks, no DB."""

    def test_serialize_response_blocks_dict_input(self):
        from services.claude_service import ClaudeService

        raw = [
            {"type": "text", "text": "hello"},
            {"type": "thinking", "text": "reasoning..."},
            {"type": "tool_use", "id": "1", "name": "lookup", "input": {"q": "x"}},
        ]
        out = ClaudeService._serialize_response_blocks(raw)
        assert len(out) == 3
        assert out[0] == {"type": "text", "text": "hello"}
        assert out[1] == {"type": "thinking", "text": "reasoning..."}
        assert out[2]["name"] == "lookup"
        assert out[2]["input"] == {"q": "x"}

    def test_serialize_response_blocks_handles_sdk_objects(self):
        """SDK blocks expose attributes rather than dict keys."""
        from services.claude_service import ClaudeService

        class _Block:
            def __init__(self, **kw):
                for k, v in kw.items():
                    setattr(self, k, v)

        blocks = [
            _Block(type="text", text="hi"),
            _Block(type="thinking", thinking="ponder"),
            _Block(type="tool_use", id="t1", name="search", input={"x": 1}),
        ]
        out = ClaudeService._serialize_response_blocks(blocks)
        assert out[0]["text"] == "hi"
        assert out[1]["text"] == "ponder"
        assert out[2]["name"] == "search" and out[2]["input"] == {"x": 1}

    def test_serialize_empty(self):
        from services.claude_service import ClaudeService

        assert ClaudeService._serialize_response_blocks(None) == []
        assert ClaudeService._serialize_response_blocks([]) == []

    def test_sanitize_messages_strips_image_base64(self):
        from services.claude_service import ClaudeService

        msgs = [
            {"role": "user", "content": "plain string"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "here's an image"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "data": "AAAAAA" * 10000},
                    },
                ],
            },
        ]
        out = ClaudeService._sanitize_messages_for_log(msgs)
        assert out[0] == {"role": "user", "content": "plain string"}
        second = out[1]["content"]
        assert second[0] == {"type": "text", "text": "here's an image"}
        assert second[1] == {"type": "image", "source": {"type": "redacted"}}

    def test_extract_prior_tool_results(self):
        from services.claude_service import ClaudeService

        messages = [
            {"role": "user", "content": "initial question"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "result A"},
                ],
            },
        ]
        out = ClaudeService._extract_prior_tool_results(messages)
        assert len(out) == 1
        assert out[0]["type"] == "tool_result"
        assert out[0]["tool_use_id"] == "t1"

    def test_extract_prior_tool_results_none_when_no_tool_result(self):
        from services.claude_service import ClaudeService

        messages = [
            {"role": "user", "content": "just a chat"},
        ]
        assert ClaudeService._extract_prior_tool_results(messages) == []


class TestPersistInteractionRobustness:
    """_persist_interaction must never raise on persistence failure."""

    def test_persist_swallows_db_errors(self, monkeypatch, caplog):
        """If the DB isn't available, the helper must log-and-move-on."""
        from services.claude_service import ClaudeService

        # Force get_db_manager to blow up
        def _boom():
            raise RuntimeError("no db for you")

        monkeypatch.setattr("database.connection.get_db_manager", _boom)

        svc = ClaudeService.__new__(
            ClaudeService
        )  # bypass __init__ (no API key needed)

        # Should not raise, should log warning
        svc._persist_interaction(
            session_id="s1",
            agent_id=None,
            investigation_id=None,
            model="test-model",
            system_prompt=None,
            request_messages=[{"role": "user", "content": "hi"}],
            response_content=[{"type": "text", "text": "hello"}],
            thinking_enabled=False,
            thinking_budget=None,
            stop_reason="end_turn",
            input_tokens=1,
            output_tokens=1,
            duration_ms=10,
        )
