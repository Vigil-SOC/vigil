"""AI insights go through the LLM gateway and fall back on a failed call or bad reply."""

import logging

import pytest

from core.reporting.ai_insights_service import AIInsightsService

pytestmark = pytest.mark.unit

METRICS = {
    "totalFindings": 12,
    "findingsChange": 25.0,
    "totalCases": 3,
    "casesChange": 0.0,
    "avgResponseTime": 15,
    "responseTimeChange": 0.0,
    "falsePositiveRate": 40,
    "falsePositiveChange": 1.0,
}

TIME_SERIES = [{"timestamp": "2026-09-29T00:00:00Z", "findings": 4}]


class _Gateway:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def submit(self, **kwargs):
        self.calls += 1
        return self.result


def _patch_gateway(monkeypatch, result) -> _Gateway:
    gateway = _Gateway(result)

    async def get_llm_gateway():
        return gateway

    monkeypatch.setattr("core.llm.gateway.gateway.get_llm_gateway", get_llm_gateway)
    return gateway


def _signature(insights):
    return [
        (item["type"], item["title"], item["description"], item["actionable"])
        for item in insights
    ]


@pytest.mark.asyncio
async def test_error_dict_returns_fallback_and_logs_the_worker_error(
    monkeypatch, caplog
):
    gateway = _patch_gateway(
        monkeypatch,
        {
            "content": "",
            "type": "error",
            "error": "RuntimeError: 401 invalid x-api-key",
        },
    )
    service = AIInsightsService()

    with caplog.at_level(logging.ERROR, logger="core.reporting.ai_insights_service"):
        result = await service.generate_insights(METRICS, TIME_SERIES, "7d")

    assert gateway.calls == 1
    assert _signature(result) == _signature(service._get_fallback_insights(METRICS))
    assert "RuntimeError: 401 invalid x-api-key" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "I could not produce insights.",
        '[{"type": "warning", "title": ]',
    ],
)
async def test_unparseable_reply_returns_fallback(monkeypatch, content):
    gateway = _patch_gateway(monkeypatch, {"content": content, "type": "text"})
    service = AIInsightsService()

    result = await service.generate_insights(METRICS, TIME_SERIES, "7d")

    assert gateway.calls == 1
    assert _signature(result) == _signature(service._get_fallback_insights(METRICS))


@pytest.mark.asyncio
async def test_well_formed_reply_is_parsed(monkeypatch):
    gateway = _patch_gateway(
        monkeypatch,
        {
            "content": """[
              {
                "type": "recommendation",
                "title": "Optimize alert tuning",
                "description": "Review detection rules.",
                "confidence": 0.85,
                "actionable": true
              }
            ]""",
            "type": "text",
        },
    )
    service = AIInsightsService()

    result = await service.generate_insights(METRICS, TIME_SERIES, "7d")

    assert gateway.calls == 1
    assert len(result) == 1
    insight = result[0]
    assert insight["type"] == "recommendation"
    assert insight["title"] == "Optimize alert tuning"
    assert insight["description"] == "Review detection rules."
    assert insight["confidence"] == 0.85
    assert insight["actionable"] is True
    assert insight["id"].startswith("insight-")
    assert insight["timestamp"]


@pytest.mark.asyncio
async def test_valid_empty_array_is_returned_as_empty(monkeypatch):
    gateway = _patch_gateway(monkeypatch, {"content": "[]", "type": "text"})
    service = AIInsightsService()

    result = await service.generate_insights(METRICS, TIME_SERIES, "7d")

    assert gateway.calls == 1
    assert result == []


@pytest.mark.asyncio
async def test_gateway_is_called_without_an_anthropic_key(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "anthropic_api_key"):
        monkeypatch.delenv(name, raising=False)
    gateway = _patch_gateway(
        monkeypatch,
        {"content": '[{"type": "info", "title": "t", "description": "d"}]'},
    )

    result = await AIInsightsService().generate_insights(METRICS, TIME_SERIES, "7d")

    assert gateway.calls == 1
    assert [item["title"] for item in result] == ["t"]
