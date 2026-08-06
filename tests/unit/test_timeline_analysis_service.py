"""Unit tests for services/timeline_analysis_service.py (#413 4c-4).

`generate_event_analysis` was relocated off ClaudeService and made
provider-agnostic: it dispatches through `LLMRouter.chat` with no explicit
provider (so the router resolves the configured default) and mirrors the old
`ClaudeService(use_backend_tools=True, use_mcp_tools=False)` construction via
`service_config`. These tests pin the dispatch contract and the JSON-parsing
behaviour, with the router patched so no provider/DB/SDK is required.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ.setdefault("DEV_MODE", "true")

import services.llm_router as llm_router  # noqa: E402
from services.timeline_analysis_service import generate_event_analysis  # noqa: E402

pytestmark = pytest.mark.unit

_EVENT = {
    "start": "2026-07-25T00:00:00Z",
    "type": "auth",
    "severity": "high",
    "content": "failed login burst",
}
_RELATED = [
    {"start": "2026-07-25T00:01:00Z", "severity": "medium", "content": "port scan"}
]
_FINDING = {
    "finding_id": "f1",
    "severity": "high",
    "data_source": "splunk",
    "anomaly_score": 0.9,
    "description": "suspicious",
    "entity_context": {"src_ip": "1.2.3.4", "user": "alice"},
    "mitre_predictions": {"T1110": 0.8},
}

_VALID = {
    "incident_summary": "s",
    "attack_narrative": "n",
    "entity_analysis": "e",
    "threat_assessment": "t",
    "investigation_priorities": ["p1"],
    "response_recommendations": ["r1"],
    "timeline_correlation": "c",
    "confidence_score": 0.9,
}


class _FakeRouter:
    last_kwargs = None
    reply = json.dumps(_VALID)

    def __init__(self, *a, **k):
        pass

    async def chat(self, message, **kwargs):
        type(self).last_kwargs = {"message": message, **kwargs}
        return type(self).reply


def _patch(monkeypatch, reply):
    _FakeRouter.last_kwargs = None
    _FakeRouter.reply = reply
    monkeypatch.setattr(llm_router, "LLMRouter", _FakeRouter)


def test_parses_valid_json_and_dispatch_contract(monkeypatch):
    _patch(monkeypatch, json.dumps(_VALID))
    result = asyncio.run(generate_event_analysis(_EVENT, _RELATED, _FINDING))
    assert result["incident_summary"] == "s"
    assert result["confidence_score"] == 0.9
    # Provider-agnostic: no explicit provider passed (router resolves default).
    assert "provider" not in _FakeRouter.last_kwargs
    # ctor flags mirror the old ClaudeService(use_backend_tools=True,
    # use_mcp_tools=False) construction.
    assert _FakeRouter.last_kwargs["service_config"] == {
        "use_backend_tools": True,
        "use_mcp_tools": False,
    }


def test_strips_markdown_json_fence(monkeypatch):
    _patch(monkeypatch, "```json\n" + json.dumps(_VALID) + "\n```")
    result = asyncio.run(generate_event_analysis(_EVENT, _RELATED, _FINDING))
    assert result["attack_narrative"] == "n"


def test_missing_fields_are_backfilled(monkeypatch):
    _patch(monkeypatch, json.dumps({"incident_summary": "only"}))
    result = asyncio.run(generate_event_analysis(_EVENT, _RELATED, _FINDING))
    assert result["incident_summary"] == "only"
    # Required fields backfilled; confidence defaulted.
    assert result["attack_narrative"] == "Analysis for attack_narrative not available"
    assert result["confidence_score"] == 0.7


def test_non_json_reply_returns_fallback(monkeypatch):
    _patch(monkeypatch, "sorry, I cannot help with that")
    result = asyncio.run(generate_event_analysis(_EVENT, _RELATED, _FINDING))
    assert result["error"] == "JSON parsing failed"
    assert result["confidence_score"] == 0.5


def test_none_reply_returns_fallback(monkeypatch):
    _patch(monkeypatch, None)
    result = asyncio.run(generate_event_analysis(_EVENT, _RELATED, _FINDING))
    assert result["error"] == "JSON parsing failed"


def test_works_without_finding_data(monkeypatch):
    _patch(monkeypatch, json.dumps(_VALID))
    result = asyncio.run(generate_event_analysis(_EVENT, [], None))
    assert result["threat_assessment"] == "t"
