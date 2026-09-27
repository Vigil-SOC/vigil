"""Unit tests for LLM reasoning-trace persistence (GH #79).

Covers:
- LLMInteractionLog model registration + LLMInteractionLogSchema shape
- ClaudeService serialization helpers (static methods — no DB, no API)
- ClaudeService._persist_interaction graceful failure when DB is unavailable
"""

from core.time import utcnow


class TestLLMInteractionLogModel:
    """Shape-level checks that don't require a live database."""

    def test_model_registered_in_metadata(self):
        """llm_interaction_logs must be in Base.metadata so create_all creates it."""
        from core.storage.models import Base
        import core.storage.connection  # noqa: F401 — side-effect import

        assert "llm_interaction_logs" in Base.metadata.tables

    def test_to_summary_dict_has_no_heavy_fields(self):
        """List endpoints must not leak heavy text/JSONB columns."""
        from core.storage.models import LLMInteractionLog
        from core.storage.schemas import LLMInteractionLogSchema

        row = LLMInteractionLog(
            interaction_id="abc-123",
            session_id="session-1",
            agent_id="investigator",
            investigation_id=None,
            created_at=utcnow(),
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
        summary = LLMInteractionLogSchema.dump_summary(row)

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
        from core.storage.models import LLMInteractionLog
        from core.storage.schemas import LLMInteractionLogSchema

        row = LLMInteractionLog(
            interaction_id="abc-123",
            session_id="session-1",
            agent_id=None,
            investigation_id=None,
            created_at=utcnow(),
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
        full = LLMInteractionLogSchema.dump(row)
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
        from core.llm.harness.claude import ClaudeService

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
        from core.llm.harness.claude import ClaudeService

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
        from core.llm.harness.claude import ClaudeService

        assert ClaudeService._serialize_response_blocks(None) == []
        assert ClaudeService._serialize_response_blocks([]) == []

    def test_sanitize_messages_strips_image_base64(self):
        from core.llm.harness.claude import ClaudeService

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
        from core.llm.harness.claude import ClaudeService

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
        from core.llm.harness.claude import ClaudeService

        messages = [
            {"role": "user", "content": "just a chat"},
        ]
        assert ClaudeService._extract_prior_tool_results(messages) == []


class TestPersistInteractionRobustness:
    """_persist_interaction must never raise on persistence failure."""

    def test_persist_swallows_db_errors(self, monkeypatch, caplog):
        """If the DB isn't available, the helper must log-and-move-on."""
        from core.llm.harness.claude import ClaudeService

        # Force get_db_manager to blow up
        def _boom():
            raise RuntimeError("no db for you")

        monkeypatch.setattr("core.storage.connection.get_db_manager", _boom)

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


class TestChatRecordsGenAIMetrics:
    """#894: the direct-SDK path records once, priced once, next to the log row."""

    def _svc(self, response):
        from unittest.mock import MagicMock

        from core.llm.harness.claude import ClaudeService

        svc = ClaudeService.__new__(ClaudeService)
        svc.api_key = "k"
        svc.client = MagicMock()
        svc.client.messages.create.return_value = response
        return svc

    def test_chat_records_once_and_prices_once(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        response = SimpleNamespace(
            model="claude-sonnet-4-5-20250929",
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=120,
                output_tokens=30,
                cache_read_input_tokens=50,
                cache_creation_input_tokens=10,
            ),
            content=[SimpleNamespace(type="text", text="hi there")],
        )
        svc = self._svc(response)

        from core.llm.cost.calls import CallQuote

        quote = CallQuote(
            0.005, 3e-6, 1.5e-5, 3e-7, 3.75e-6, "2026-01-01T00:00:00+00:00"
        )
        with patch(
            "core.llm.harness.claude.quote_call", return_value=quote
        ) as cost, patch(
            "core.llm.harness.claude.record_llm_call"
        ) as record, patch.object(
            svc, "_persist_interaction"
        ) as persist:
            out = svc.chat("hello", model="claude-sonnet-4-5-20250929")

        assert out == "hi there"
        cost.assert_called_once_with(
            "claude-sonnet-4-5-20250929",
            "anthropic",
            120,
            30,
            cache_read_tokens=50,
            cache_creation_tokens=10,
        )
        record.assert_called_once()
        kw = record.call_args.kwargs
        assert kw["model"] == "claude-sonnet-4-5-20250929"
        assert kw["provider"] == "anthropic"
        assert (kw["input_tokens"], kw["output_tokens"]) == (120, 30)
        assert (kw["cache_read_tokens"], kw["cache_creation_tokens"]) == (50, 10)
        assert kw["cost_usd"] == 0.005
        # The log row reuses that same read rather than pricing again.
        assert persist.call_args.kwargs["quote"] is quote

    def test_unpriced_chat_stores_null_and_prices_once(self, monkeypatch):
        """#1115: an unpriced call is stored as NULL, not re-priced into $0."""
        from contextlib import contextmanager
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        session = MagicMock()

        @contextmanager
        def _scope():
            yield session

        monkeypatch.setattr(
            "core.storage.connection.get_db_manager",
            lambda: SimpleNamespace(session_scope=_scope),
        )
        response = SimpleNamespace(
            model="mystery-model",
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            content=[SimpleNamespace(type="text", text="ok")],
        )
        svc = self._svc(response)

        from core.llm.cost.calls import CallQuote

        with patch(
            "core.llm.harness.claude.quote_call", return_value=CallQuote.unpriced()
        ) as cost, patch("core.llm.harness.claude.record_llm_call") as record:
            svc.chat("hello", model="mystery-model")

        cost.assert_called_once()
        assert record.call_args.kwargs["cost_usd"] is None
        row = session.add.call_args.args[0]
        assert row.cost_usd is None
        assert row.input_cost_per_token is None
        assert row.output_cost_per_token is None
        assert row.cache_read_cost_per_token is None
        assert row.cache_write_cost_per_token is None
        assert row.rates_fetched_at is None


class TestSpendFigureFreezesItsRates:
    """#1190: the dollar, the rates, and the fetch time are one write."""

    def _chat(self, model, usage):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from core.llm.harness.claude import ClaudeService

        response = SimpleNamespace(
            model=model,
            stop_reason="end_turn",
            usage=usage,
            content=[SimpleNamespace(type="text", text="ok")],
        )
        svc = ClaudeService.__new__(ClaudeService)
        svc.api_key = "k"
        svc.client = MagicMock()
        svc.client.messages.create.return_value = response
        return svc.chat("hello", model=model)

    def test_priced_call_keeps_its_rates_and_unpriced_stores_nulls(self, monkeypatch):
        from contextlib import contextmanager
        from decimal import Decimal
        from types import SimpleNamespace
        from unittest.mock import patch

        from sqlalchemy import Float

        from core.llm.providers.discovery import ModelMeta
        from core.llm.providers.registry import (
            ModelRegistry,
            clear_live_meta,
            get_registry,
            record_live_meta,
        )
        from core.storage.models import LLMInteractionLog

        # A cache rate below 1e-6 must survive the column. Numeric(10, 6) would not.
        rate_column = LLMInteractionLog.__table__.c.cache_read_cost_per_token
        assert isinstance(rate_column.type, Float)

        added = []
        session = SimpleNamespace(add=lambda row: added.append(row))

        @contextmanager
        def _scope():
            yield session

        monkeypatch.setattr(
            "core.storage.connection.get_db_manager",
            lambda: SimpleNamespace(session_scope=_scope),
        )
        unknown = []
        monkeypatch.setattr(
            "core.llm.providers.registry._record_pricing_unknown",
            lambda provider, model: unknown.append((provider, model)),
        )

        model = "claude-freeze-1190"
        rates = (3e-6, 15e-6, 1.25e-7, 3.75e-6)
        clear_live_meta()
        try:
            record_live_meta(
                "anthropic",
                [
                    ModelMeta(
                        id=model,
                        display_name=model,
                        input_cost_per_token=rates[0],
                        output_cost_per_token=rates[1],
                        cache_read_cost_per_token=rates[2],
                        cache_write_cost_per_token=rates[3],
                    )
                ],
                rates_only=True,
            )
            stamped = get_registry().get_rates(model, "anthropic")["rates_fetched_at"]
            usage = SimpleNamespace(
                input_tokens=1_000,
                output_tokens=200,
                cache_read_input_tokens=8_000,
                cache_creation_input_tokens=400,
            )
            reads = []
            real_rates = ModelRegistry.get_rates

            def _counting(model_id, provider_type):
                reads.append((provider_type, model_id))
                return real_rates(model_id, provider_type)

            monkeypatch.setattr(ModelRegistry, "get_rates", staticmethod(_counting))
            with patch("core.llm.harness.claude.record_llm_call") as recorded:
                self._chat(model, usage)
            assert reads == [("anthropic", model)]

            row = added[0]
            product = (
                row.input_tokens * row.input_cost_per_token
                + row.output_tokens * row.output_cost_per_token
                + row.cache_read_tokens * row.cache_read_cost_per_token
                + row.cache_creation_tokens * row.cache_write_cost_per_token
            )
            # cost_usd is Numeric(10, 6). The product of the stored rates and
            # the stored tokens matches that column's rounding.
            assert Decimal(str(product)).quantize(Decimal("0.000001")) == Decimal(
                str(row.cost_usd)
            ).quantize(Decimal("0.000001"))
            assert (
                row.input_cost_per_token,
                row.output_cost_per_token,
                row.cache_read_cost_per_token,
                row.cache_write_cost_per_token,
            ) == rates
            assert row.rates_fetched_at == stamped
            assert recorded.call_args.kwargs["cost_usd"] == row.cost_usd
            assert unknown == []

            record_live_meta(
                "anthropic",
                [
                    ModelMeta(
                        id=model,
                        display_name=model,
                        input_cost_per_token=9e-6,
                        output_cost_per_token=9e-6,
                        cache_read_cost_per_token=9e-6,
                        cache_write_cost_per_token=9e-6,
                    )
                ],
                rates_only=True,
            )
            live = get_registry().get_rates(model, "anthropic")
            assert live["input"] == 9e-6
            assert live["rates_fetched_at"] != stamped
            assert row.input_cost_per_token == rates[0]
            assert row.rates_fetched_at == stamped

            with patch("core.llm.harness.claude.record_llm_call") as recorded:
                self._chat(
                    "mystery-freeze-1190",
                    SimpleNamespace(input_tokens=10, output_tokens=5),
                )

            blank = added[1]
            assert blank.cost_usd is None
            assert blank.input_cost_per_token is None
            assert blank.output_cost_per_token is None
            assert blank.cache_read_cost_per_token is None
            assert blank.cache_write_cost_per_token is None
            assert blank.rates_fetched_at is None
            assert recorded.call_args.kwargs["cost_usd"] is None
            # One catalog read: a second would count the miss twice.
            assert unknown == [("anthropic", "mystery-freeze-1190")]
        finally:
            clear_live_meta()
