"""
Unit tests for daemon/autonomous operations.
Tests configured floors, triage provider resolution and triage timeouts.
"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from services.daemon.config import ProcessingConfig
from services.daemon.processor import FindingProcessor
from services.daemon.responder import AutonomousResponder


class TestConfiguredFloors:
    """The responder's severity floors and the processor's queue line read
    ResponseConfig rather than literals (#916)."""

    def _responder(self, **overrides):
        from services.daemon.config import EscalationConfig, ResponseConfig

        return AutonomousResponder(
            ResponseConfig(**overrides),
            EscalationConfig(),
            response_service=Mock(),
            approvals=Mock(),
        )

    def test_default_floors_match_the_old_literals(self):
        responder = self._responder()
        assert responder._determine_action("critical", 0.70, "")[0] == "isolate"
        assert responder._determine_action("critical", 0.69, "") is None
        assert responder._determine_action("high", 0.80, "")[0] == "investigate"
        assert responder._determine_action("high", 0.79, "") is None

    def test_raised_floors_move_the_decision(self):
        responder = self._responder(critical_action_floor=0.90, high_action_floor=0.95)
        assert responder._determine_action("critical", 0.85, "") is None
        assert responder._determine_action("high", 0.90, "") is None

    def test_decision_records_the_branch_that_fired(self):
        """Two findings decided by different branches record different rules (#917)."""
        responder = self._responder()
        assert responder._determine_action("critical", 0.70, "") == (
            "isolate",
            "response.critical_action_floor=0.70 met (0.70)",
        )
        assert responder._determine_action("medium", 0.92, "isolate") == (
            "isolate",
            "response.confidence_threshold=0.90 met (0.92)",
        )

    @pytest.mark.asyncio
    async def test_reason_carries_rule_and_dry_run_logs_it(self, caplog):
        from services.daemon.config import EscalationConfig, ResponseConfig

        finding = {
            "finding_id": "f-917",
            "severity": "critical",
            "triage_confidence": 0.75,
            "entity_context": {"src_ips": ["10.0.0.9"]},
        }
        responder = self._responder()
        await responder._evaluate_response(finding)
        reason = responder._response_service.create_isolation_action.call_args.kwargs[
            "reason"
        ]
        assert reason == (
            "Automated response to f-917; "
            "response.critical_action_floor=0.70 met (0.75)"
        )

        dry = AutonomousResponder(
            ResponseConfig(dry_run=True),
            EscalationConfig(),
            response_service=Mock(),
            approvals=Mock(),
        )
        with caplog.at_level("INFO", logger="services.daemon.responder"):
            await dry._evaluate_response(finding)
        assert "response.critical_action_floor=0.70 met (0.75)" in caplog.text
        dry._response_service.create_isolation_action.assert_not_called()

    @pytest.mark.asyncio
    async def test_reused_isolation_is_not_counted_as_auto_executed(self):
        """#1217: a reused isolation (collapsed by the idempotency key) must not
        be logged or counted the same as a fresh auto-execution."""
        finding = {
            "finding_id": "f-1217",
            "severity": "critical",
            "triage_confidence": 0.75,
            "entity_context": {"hostnames": ["host-b"]},
        }
        responder = self._responder()
        responder._response_service.create_isolation_action.return_value = {
            "status": "executed",
            "reused": True,
            "action_id": "action-existing",
        }
        await responder._evaluate_response(finding)
        assert responder.stats["auto_executed"] == 0
        assert responder.stats["reused"] == 1

    @pytest.mark.asyncio
    async def test_fresh_isolation_is_counted_as_auto_executed(self):
        finding = {
            "finding_id": "f-1217b",
            "severity": "critical",
            "triage_confidence": 0.75,
            "entity_context": {"hostnames": ["host-a"]},
        }
        responder = self._responder()
        responder._response_service.create_isolation_action.return_value = {
            "status": "executed",
            "action_id": "action-new",
        }
        await responder._evaluate_response(finding)
        assert responder.stats["auto_executed"] == 1
        assert responder.stats["reused"] == 0

    @pytest.mark.asyncio
    async def test_failed_isolation_is_not_counted_as_auto_executed(self, caplog):
        """#1276: a failed isolation must not be logged or counted as contained."""
        finding = {
            "finding_id": "f-1276",
            "severity": "critical",
            "triage_confidence": 0.95,
            "entity_context": {"src_ips": ["10.0.0.5"]},
        }
        responder = self._responder()
        responder._response_service.create_isolation_action.return_value = {
            "status": "failed",
            "action_id": "action-failed",
            "result": {"success": False, "error": "unsupported_action_type"},
        }
        with caplog.at_level("INFO", logger="services.daemon.responder"):
            await responder._evaluate_response(finding)
        assert responder.stats["auto_executed"] == 0
        assert "Auto-executed" not in caplog.text

    @pytest.mark.asyncio
    async def test_processor_queues_at_review_threshold(self):
        from services.daemon.config import ResponseConfig

        processor = FindingProcessor(
            ProcessingConfig(), response_config=ResponseConfig(review_threshold=0.95)
        )
        queue = asyncio.Queue()
        processor.set_response_queue(queue)
        with patch("services.daemon.orchestrator.insert_intake_trigger"):
            await processor._evaluate_for_response(
                {"finding_id": "f-1", "severity": "low", "triage_confidence": 0.90}
            )
            assert queue.empty()
            await processor._evaluate_for_response(
                {"finding_id": "f-2", "severity": "low", "triage_confidence": 0.95}
            )
            assert queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_processor_feed_hit_offers_detection_without_response(self):
        """#1008: a threat_indicators hit reaches intake even when Gate 1 is false."""
        from services.daemon.config import ResponseConfig

        processor = FindingProcessor(
            ProcessingConfig(), response_config=ResponseConfig(review_threshold=0.95)
        )
        queue = asyncio.Queue()
        processor.set_response_queue(queue)
        below_gate = {"severity": "medium", "triage_confidence": 0.5}
        hit = {"threat_indicators": {"src_ip": [{"indicator": "10.0.0.9"}]}}

        with patch("services.daemon.orchestrator.insert_intake_trigger") as insert:
            await processor._evaluate_for_response(
                {"finding_id": "f-hit", "enrichment": hit, **below_gate}
            )
            insert.assert_called_once()
            assert insert.call_args.kwargs["kind"] == "detection"
            assert insert.call_args.kwargs["finding_id"] == "f-hit"
            assert queue.empty()

        # No hit: the key is absent when the lookup found nothing, and the
        # whole enrichment block is absent when enrichment is off.
        with patch("services.daemon.orchestrator.insert_intake_trigger") as insert:
            await processor._evaluate_for_response(
                {"finding_id": "f-miss", "enrichment": {"geo": {}}, **below_gate}
            )
            await processor._evaluate_for_response(
                {"finding_id": "f-bare", **below_gate}
            )
            insert.assert_not_called()
            assert queue.empty()

        with patch("services.daemon.orchestrator.insert_intake_trigger") as insert:
            await processor._evaluate_for_response(
                {"finding_id": "f-both", "severity": "high", "enrichment": hit}
            )
            insert.assert_called_once()
            assert queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_processor_detection_priority_is_severity_band(self):
        """#1105: an unrated feed hit stores "unknown"; a rated finding keeps its band."""
        from services.daemon.config import ResponseConfig

        processor = FindingProcessor(
            ProcessingConfig(), response_config=ResponseConfig(review_threshold=0.95)
        )
        hit = {"threat_indicators": {"src_ip": [{"indicator": "10.0.0.9"}]}}

        with patch("services.daemon.orchestrator.insert_intake_trigger") as insert:
            await processor._evaluate_for_response(
                {"finding_id": "f-unrated", "enrichment": hit, "triage_confidence": 0.5}
            )
            await processor._evaluate_for_response(
                {"finding_id": "f-high", "severity": "High", "triage_confidence": 0.5}
            )
        priorities = [c.kwargs["priority"] for c in insert.call_args_list]
        assert priorities == ["unknown", "high"]


class TestTriageProviderResolution:
    """Daemon triage resolves a provider like chat does and records failures (#965)."""

    @staticmethod
    def _spec(provider_id="openai-1", provider_type="openai", model="gpt-4o"):
        from core.llm.router.router import ProviderSpec

        return ProviderSpec(
            provider_id=provider_id,
            provider_type=provider_type,
            base_url=None,
            api_key_ref=None,
            default_model=model,
            config={},
        )

    def test_openai_only_install_routes_to_default_provider(self):
        """No Anthropic default: registry yields None, default provider row wins."""
        registry = Mock()
        registry.resolve_model_for_component.return_value = None
        with patch(
            "core.llm.providers.registry.get_registry", return_value=registry
        ), patch(
            "core.llm.target.provider_for", return_value=self._spec()
        ) as pf, patch(
            "core.llm.target.model_for", return_value="gpt-4o"
        ):
            assert FindingProcessor._resolve_triage_target() == ("openai-1", "gpt-4o")
        registry.resolve_model_for_component.assert_called_once_with("triage")
        pf.assert_called_once_with(None)

    def test_no_provider_records_error(self):
        processor = FindingProcessor(ProcessingConfig())
        processor._llm_gateway = Mock()
        with patch.object(
            FindingProcessor, "_resolve_triage_target", return_value=None
        ):
            content, error = asyncio.run(processor._get_ai_triage("p"))
        assert content is None and "no LLM provider" in error
        processor._llm_gateway.submit.assert_not_called()

    def test_gateway_gets_resolved_provider_and_worker_error_is_surfaced(self):
        processor = FindingProcessor(ProcessingConfig())
        gateway = Mock()
        gateway.submit = AsyncMock(
            return_value={
                "content": "",
                "type": "error",
                "error": "AuthenticationError: bad key",
            }
        )
        processor._llm_gateway = gateway
        with patch.object(
            FindingProcessor,
            "_resolve_triage_target",
            return_value=("openai-1", "gpt-4o"),
        ):
            content, error = asyncio.run(processor._get_ai_triage("p"))
        gateway.submit.assert_awaited_once_with(
            "p", provider_id="openai-1", model="gpt-4o", timeout=60
        )
        assert content is None and error == "AuthenticationError: bad key"

    def test_failed_triage_lands_in_ai_enrichment_without_ai_triage(self):
        processor = FindingProcessor(ProcessingConfig())
        finding = {"finding_id": "f1", "severity": "high"}
        with patch.object(
            FindingProcessor, "_get_ai_triage", AsyncMock(return_value=(None, "boom"))
        ):
            finding = asyncio.run(processor._triage_finding(finding))
        assert finding["ai_triage_error"] == "boom"
        assert "ai_triage" not in finding
        processor._data_service = Mock()
        processor._data_service.update_finding.return_value = True
        asyncio.run(processor._update_finding(finding))
        kwargs = processor._data_service.update_finding.call_args.kwargs
        assert kwargs["ai_enrichment"] == {"ai_triage_error": "boom"}

    def test_success_clears_previous_error(self):
        processor = FindingProcessor(ProcessingConfig())
        finding = {"finding_id": "f1", "ai_triage_error": "old"}
        with patch.object(
            FindingProcessor,
            "_get_ai_triage",
            AsyncMock(return_value=("SEVERITY: low\nREASONING: fine", None)),
        ):
            finding = asyncio.run(processor._triage_finding(finding))
        assert "ai_triage_error" not in finding
        assert finding["ai_triage"]["result"]["severity"] == "low"


class TestTriageTimeout:
    """DAEMON_TRIAGE_TIMEOUT bounds both the daemon wait and the gateway wait (#1058)."""

    ROUND_TRIP = 0.3  # stands in for a >60s model round trip, scaled down

    @classmethod
    def _run(cls, triage_timeout):
        async def slow_submit(prompt, *, provider_id, model, timeout):
            # Mirrors arq's job.result(timeout=...): the gateway's own clock.
            await asyncio.wait_for(asyncio.sleep(cls.ROUND_TRIP), timeout)
            return {"content": "SEVERITY: high\nREASONING: slow but fine"}

        processor = FindingProcessor(ProcessingConfig(triage_timeout=triage_timeout))
        processor._llm_gateway = Mock(submit=slow_submit)
        with patch.object(
            FindingProcessor, "_resolve_triage_target", return_value=("gemini", "m")
        ):
            return processor, asyncio.run(
                processor._triage_finding({"finding_id": "f1"})
            )

    def test_raised_timeout_triages_on_first_attempt(self):
        processor, finding = self._run(triage_timeout=1)
        assert "ai_triage_error" not in finding
        assert finding["severity"] == "high"
        assert processor.stats["triaged"] == 1

    def test_short_timeout_times_out_naming_the_knob(self, caplog):
        processor, finding = self._run(triage_timeout=0.1)
        assert finding["ai_triage_error"] == "timed out after 0.1s"
        assert processor.stats["triaged"] == 0
        assert "timed out after 0.1s for f1 (DAEMON_TRIAGE_TIMEOUT)" in caplog.text
