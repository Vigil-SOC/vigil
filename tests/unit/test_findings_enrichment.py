"""Unit tests for the finding-enrichment module (issue #470).

These cover the seam the extraction created. Before it, none of this was
reachable without an HTTP round-trip and a live provider:

* ``prompt.py`` — singular/plural entity keys, MITRE technique fallback
* ``parse.py``  — fenced JSON, raw JSON, malformed text, empty response
* ``service.py`` — orchestration: metadata stamping, persist on/off, and the
  domain errors that replaced the inline ``HTTPException`` raises

Provider resolution and dispatch are stubbed; nothing here touches a network
or a database.
"""

from __future__ import annotations

import json

import pytest

from services.findings.enrichment import (
    EmptyProviderResponse,
    FindingNotFound,
    build_entity_string,
    build_prompt,
    build_techniques_string,
    UnidentifiableFinding,
    enrich,
    extract_json_block,
    parse_enrichment,
    summarize_finding,
)
from services.findings.enrichment import service as enrichment_service

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _FakeProvider:
    def __init__(self, provider_type="ollama", provider_id="ollama-default"):
        self.provider_type = provider_type
        self.provider_id = provider_id


class _RecordingDataService:
    """Stands in for DatabaseDataService; records writes."""

    def __init__(self, success=True):
        self.success = success
        self.writes = []

    def update_finding(self, finding_id, **updates):
        self.writes.append((finding_id, updates))
        return self.success


def _finding(**overrides):
    finding = {
        "finding_id": "f-20260803-001",
        "severity": "high",
        "data_source": "zeek",
        "timestamp": "2026-08-03T12:00:00Z",
        "description": "Beaconing to a rare external host",
        "anomaly_score": 0.91,
        "entity_context": {},
        "mitre_predictions": {},
        "predicted_techniques": [],
    }
    finding.update(overrides)
    return finding


@pytest.fixture
def stub_provider(monkeypatch):
    """Stub resolution + dispatch so ``enrich()`` runs without a provider.

    Returns a mutable dict the test can retune: ``response`` is what the fake
    provider returns, ``provider`` the resolved spec.
    """
    state = {
        "provider": _FakeProvider(),
        "model_id": "qwen3:8b",
        "response": json.dumps({"threat_summary": "beaconing"}),
        "prompts": [],
    }

    def fake_resolve(component):
        state["component"] = component
        return state["provider"], state["model_id"], None

    async def fake_dispatch(*, provider, model_id, prompt, claude_service, finding_id):
        state["prompts"].append(prompt)
        return state["response"]

    monkeypatch.setattr(enrichment_service, "_resolve_provider", fake_resolve)
    monkeypatch.setattr(enrichment_service, "_dispatch", fake_dispatch)
    return state


# ---------------------------------------------------------------------------
# prompt.py — entity context
# ---------------------------------------------------------------------------


def test_entity_string_accepts_plural_keys():
    entity_str = build_entity_string(
        {
            "src_ips": ["10.0.0.1", "10.0.0.2"],
            "dst_ips": ["8.8.8.8"],
            "hostnames": ["web-01"],
            "users": ["alice"],
        }
    )
    assert "Source IPs: 10.0.0.1, 10.0.0.2\n" in entity_str
    assert "Destination IPs: 8.8.8.8\n" in entity_str
    assert "Hostnames: web-01\n" in entity_str
    assert "Users: alice\n" in entity_str


def test_entity_string_accepts_singular_keys():
    entity_str = build_entity_string(
        {
            "src_ip": "10.0.0.1",
            "dst_ip": "8.8.8.8",
            "hostname": "web-01",
            "user": "alice",
        }
    )
    assert entity_str == (
        "Source IPs: 10.0.0.1\n"
        "Destination IPs: 8.8.8.8\n"
        "Hostnames: web-01\n"
        "Users: alice\n"
    )


def test_entity_string_accepts_dest_ips_and_usernames_aliases():
    entity_str = build_entity_string({"dest_ips": ["1.1.1.1"], "usernames": ["bob"]})
    assert "Destination IPs: 1.1.1.1\n" in entity_str
    assert "Users: bob\n" in entity_str


def test_entity_string_prefers_plural_over_singular():
    entity_str = build_entity_string({"src_ips": ["10.0.0.9"], "src_ip": "10.0.0.1"})
    assert entity_str == "Source IPs: 10.0.0.9\n"


def test_entity_string_caps_each_list_at_five():
    entity_str = build_entity_string({"src_ips": [f"10.0.0.{i}" for i in range(9)]})
    assert (
        entity_str == "Source IPs: 10.0.0.0, 10.0.0.1, 10.0.0.2, 10.0.0.3, 10.0.0.4\n"
    )


def test_entity_string_is_empty_for_missing_or_empty_context():
    assert build_entity_string(None) == ""
    assert build_entity_string({}) == ""
    # Keys present but None must not render an empty label.
    assert build_entity_string({"src_ips": None, "src_ip": None}) == ""


# ---------------------------------------------------------------------------
# prompt.py — MITRE techniques
# ---------------------------------------------------------------------------


def test_predicted_techniques_win_over_mitre_predictions():
    techniques_str = build_techniques_string(
        [{"technique_id": "T1071.001", "confidence": 0.85}],
        {"T1048.003": 0.99},
    )
    assert techniques_str == "T1071.001 (confidence: 0.85)"


def test_mitre_predictions_fallback_is_sorted_by_confidence_desc():
    techniques_str = build_techniques_string(
        [],
        {"T1000": 0.10, "T3000": 0.90, "T2000": 0.50},
    )
    assert techniques_str.splitlines() == [
        "T3000 (confidence: 0.90)",
        "T2000 (confidence: 0.50)",
        "T1000 (confidence: 0.10)",
    ]


def test_mitre_predictions_fallback_caps_at_five():
    techniques_str = build_techniques_string(
        [], {f"T{i}000": i / 10 for i in range(1, 9)}
    )
    assert len(techniques_str.splitlines()) == 5


def test_techniques_tolerate_missing_id_and_none_confidence():
    techniques_str = build_techniques_string([{"confidence": None}], {})
    assert techniques_str == "Unknown (confidence: 0.00)"
    assert build_techniques_string([], {"T1000": None}) == "T1000 (confidence: 0.00)"


def test_techniques_string_is_empty_when_nothing_predicted():
    assert build_techniques_string([], {}) == ""
    assert build_techniques_string(None, None) == ""


# ---------------------------------------------------------------------------
# prompt.py — summarize + build
# ---------------------------------------------------------------------------


def test_summarize_finding_defaults_none_valued_keys():
    summary = summarize_finding(
        {
            "finding_id": "f-1",
            "severity": None,
            "data_source": None,
            "timestamp": None,
            "description": None,
            "anomaly_score": None,
            "entity_context": None,
            "mitre_predictions": None,
            "predicted_techniques": None,
        }
    )
    assert summary.severity == "unknown"
    assert summary.data_source == "unknown"
    assert summary.timestamp == ""
    assert summary.description == ""
    assert summary.anomaly_score == 0.0
    assert summary.entity_str == ""
    assert summary.techniques_str == ""


def test_build_prompt_includes_optional_sections_when_present():
    prompt = build_prompt(
        summarize_finding(
            _finding(
                entity_context={"src_ip": "10.0.0.1"},
                predicted_techniques=[
                    {"technique_id": "T1071.001", "confidence": 0.85}
                ],
            )
        )
    )
    assert "Finding ID: f-20260803-001" in prompt
    assert "Anomaly Score: 0.91" in prompt
    assert "Entity Context:\nSource IPs: 10.0.0.1" in prompt
    assert "MITRE ATT&CK Techniques:\nT1071.001 (confidence: 0.85)" in prompt
    assert "Respond ONLY with valid JSON." in prompt


def test_build_prompt_uses_placeholders_when_sections_are_empty():
    prompt = build_prompt(summarize_finding(_finding(description="")))
    assert "No description available" in prompt
    assert "No MITRE techniques predicted" in prompt
    assert "Entity Context:" not in prompt


# ---------------------------------------------------------------------------
# parse.py
# ---------------------------------------------------------------------------


def test_parse_extracts_fenced_json():
    payload = {"threat_summary": "exfil", "confidence_score": 0.9}
    response = f"Here you go:\n```json\n{json.dumps(payload)}\n```\nHope that helps."
    assert parse_enrichment(response, severity="high") == payload


def test_parse_extracts_raw_json_with_surrounding_prose():
    payload = {"threat_summary": "lateral movement"}
    response = f"Analysis follows. {json.dumps(payload)} End of analysis."
    assert parse_enrichment(response, severity="high") == payload


def test_parse_falls_back_to_structured_payload_on_malformed_json():
    response = "I think this is suspicious but here is no JSON at all."
    enrichment = parse_enrichment(response, severity="critical")

    assert enrichment["threat_summary"] == (
        "AI analysis completed - see full analysis below"
    )
    assert enrichment["risk_level"] == "Critical"  # severity.title()
    assert enrichment["confidence_score"] == 0.7
    assert enrichment["analysis_notes"] == response
    assert enrichment["raw_response"] == response


def test_parse_fallback_truncates_analysis_notes_but_not_raw_response():
    response = "x" * 1500
    enrichment = parse_enrichment(response, severity="low")
    assert len(enrichment["analysis_notes"]) == 1000
    assert len(enrichment["raw_response"]) == 1500


def test_parse_fallback_defaults_risk_level_when_severity_is_blank():
    enrichment = parse_enrichment("not json", severity="")
    assert enrichment["risk_level"] == "Medium"


@pytest.mark.parametrize("response", [None, ""])
def test_parse_raises_on_empty_response(response):
    with pytest.raises(EmptyProviderResponse):
        parse_enrichment(response, severity="high")


def test_extract_json_block_prefers_fence_over_later_braces():
    response = '```json\n{"a": 1}\n```\nand also {"b": 2}'
    assert extract_json_block(response) == '{"a": 1}'


def test_extract_json_block_returns_response_when_no_braces():
    assert extract_json_block("no json here") == "no json here"


# ---------------------------------------------------------------------------
# service.py — orchestration
# ---------------------------------------------------------------------------


async def test_enrich_stamps_provenance_metadata(stub_provider):
    stub_provider["provider"] = _FakeProvider("anthropic", "anthropic-default")
    stub_provider["model_id"] = "claude-opus-5"

    enrichment = await enrich(_finding(), persist=False)

    assert enrichment["threat_summary"] == "beaconing"
    assert enrichment["model"] == "claude-opus-5"
    assert enrichment["provider_id"] == "anthropic-default"
    assert enrichment["provider_type"] == "anthropic"
    assert enrichment["generated_at"].endswith("Z")


async def test_enrich_keeps_raw_response_even_when_json_parsed_cleanly(stub_provider):
    stub_provider["response"] = '```json\n{"threat_summary": "ok"}\n```'
    enrichment = await enrich(_finding(), persist=False)
    assert enrichment["raw_response"] == stub_provider["response"]


async def test_enrich_persists_by_default(stub_provider):
    data_service = _RecordingDataService()

    enrichment = await enrich(_finding(), data_service=data_service)

    assert data_service.writes == [("f-20260803-001", {"ai_enrichment": enrichment})]


async def test_enrich_skips_write_when_persist_is_false(stub_provider):
    data_service = _RecordingDataService()
    await enrich(_finding(), persist=False, data_service=data_service)
    assert data_service.writes == []


async def test_enrich_returns_payload_even_when_the_write_fails(stub_provider):
    data_service = _RecordingDataService(success=False)
    enrichment = await enrich(_finding(), data_service=data_service)
    assert enrichment["threat_summary"] == "beaconing"


async def test_enrich_passes_component_through_to_resolution(stub_provider):
    await enrich(_finding(), component="triage", persist=False)
    assert stub_provider["component"] == "triage"


async def test_enrich_defaults_to_the_reporting_component(stub_provider):
    await enrich(_finding(), persist=False)
    assert stub_provider["component"] == "reporting"


async def test_enrich_rejects_an_empty_finding(stub_provider):
    with pytest.raises(FindingNotFound):
        await enrich({}, persist=False)


async def test_enrich_propagates_empty_provider_response(stub_provider):
    stub_provider["response"] = ""
    with pytest.raises(EmptyProviderResponse):
        await enrich(_finding(), persist=False)


async def test_enrich_builds_the_prompt_from_the_finding(stub_provider):
    await enrich(_finding(entity_context={"hostnames": ["db-07"]}), persist=False)
    assert "Hostnames: db-07" in stub_provider["prompts"][0]


# ---------------------------------------------------------------------------
# service.py — the write target
#
# The pre-extraction handler persisted with its path param. Deriving the write
# target from the finding dict instead means an id-less dict silently writes to
# update_finding(""), which matches no row and only logs — a lost write. These
# pin the explicit id and the guard that replaced that hole.
# ---------------------------------------------------------------------------


async def test_explicit_finding_id_is_the_write_target(stub_provider):
    """A caller holding the id independently must win over the dict."""
    data_service = _RecordingDataService()

    await enrich(
        _finding(finding_id="stale-in-dict"),
        finding_id="authoritative-id",
        data_service=data_service,
    )

    written_id, _ = data_service.writes[0]
    assert written_id == "authoritative-id"


async def test_explicit_finding_id_is_what_the_prompt_reports(stub_provider):
    await enrich(
        _finding(finding_id="stale-in-dict"),
        finding_id="authoritative-id",
        persist=False,
    )

    assert "Finding ID: authoritative-id" in stub_provider["prompts"][0]
    assert "stale-in-dict" not in stub_provider["prompts"][0]


async def test_finding_id_falls_back_to_the_dict_when_not_passed(stub_provider):
    data_service = _RecordingDataService()

    await enrich(_finding(finding_id="from-dict"), data_service=data_service)

    written_id, _ = data_service.writes[0]
    assert written_id == "from-dict"


@pytest.mark.parametrize("missing_id", [None, ""])
async def test_persist_without_any_id_raises_instead_of_writing_to_empty_string(
    stub_provider, missing_id
):
    """The regression this guard exists for: a silently-dropped write."""
    data_service = _RecordingDataService()

    with pytest.raises(UnidentifiableFinding):
        await enrich(_finding(finding_id=missing_id), data_service=data_service)

    assert data_service.writes == []


async def test_the_id_guard_runs_before_any_provider_call(stub_provider):
    """Fail fast — don't pay for a dispatch whose result can't be stored."""
    with pytest.raises(UnidentifiableFinding):
        await enrich(_finding(finding_id=None))

    assert stub_provider["prompts"] == []


async def test_an_id_less_finding_is_fine_when_not_persisting(stub_provider):
    """persist=False callers compose their own write, so no id is needed."""
    enrichment = await enrich(_finding(finding_id=None), persist=False)
    assert enrichment["threat_summary"] == "beaconing"


# ---------------------------------------------------------------------------
# service.py — the dispatch asymmetry
#
# Anthropic goes through the sync ClaudeService on a threadpool with NO retry;
# every other provider goes through LLMRouter with a retry loop and local
# Bifrost recovery. That asymmetry is existing behaviour carried over from the
# handler, so pin it — if it's ever unified, these are the tests to change.
# ---------------------------------------------------------------------------


class _FakeClaudeService:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


async def test_anthropic_dispatch_uses_claude_service_with_the_larger_cap():
    claude_service = _FakeClaudeService(['{"threat_summary": "ok"}'])

    response = await enrichment_service._dispatch(
        provider=_FakeProvider("anthropic", "anthropic-default"),
        model_id="claude-opus-5",
        prompt="PROMPT",
        claude_service=claude_service,
        finding_id="f-1",
    )

    assert response == '{"threat_summary": "ok"}'
    assert claude_service.calls == [
        {
            "message": "PROMPT",
            "model": "claude-opus-5",
            "max_tokens": enrichment_service.ANTHROPIC_MAX_TOKENS,
        }
    ]


async def test_anthropic_dispatch_does_not_retry():
    """The cloud path has never had a retry. Don't add one by accident."""
    claude_service = _FakeClaudeService(
        [RuntimeError("upstream 529"), '{"threat_summary": "second try"}']
    )

    with pytest.raises(RuntimeError, match="upstream 529"):
        await enrichment_service._dispatch(
            provider=_FakeProvider("anthropic", "anthropic-default"),
            model_id="claude-opus-5",
            prompt="PROMPT",
            claude_service=claude_service,
            finding_id="f-1",
        )

    assert len(claude_service.calls) == 1


def _patch_local_dispatch(monkeypatch, *, results, retry_limit=1, recovery_ready=True):
    """Stub LLMRouter + local-Bifrost recovery for the non-Anthropic path."""
    from services import llm_router, local_ai_recovery

    calls = []

    class _FakeRouter:
        async def dispatch(self, **kwargs):
            calls.append(kwargs)
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

    class _Recovery:
        ready = recovery_ready
        detail = "restarted" if recovery_ready else "still down"

    async def fake_recover():
        return _Recovery()

    monkeypatch.setattr(llm_router, "LLMRouter", _FakeRouter)
    monkeypatch.setattr(
        local_ai_recovery, "local_bifrost_recovery_retry_limit", lambda: retry_limit
    )
    monkeypatch.setattr(
        local_ai_recovery, "local_bifrost_recovery_enabled", lambda: True
    )
    monkeypatch.setattr(
        local_ai_recovery, "is_gateway_connection_error", lambda err: True
    )
    monkeypatch.setattr(local_ai_recovery, "recover_local_bifrost", fake_recover)
    return calls


async def _dispatch_local(provider_type="ollama"):
    return await enrichment_service._dispatch(
        provider=_FakeProvider(provider_type, f"{provider_type}-default"),
        model_id="qwen3:8b",
        prompt="PROMPT",
        claude_service=None,
        finding_id="f-1",
    )


async def test_local_dispatch_prefixes_no_think_and_uses_the_tighter_cap(monkeypatch):
    calls = _patch_local_dispatch(monkeypatch, results=[{"content": "OK"}])

    assert await _dispatch_local() == "OK"

    assert calls[0]["messages"] == [{"role": "user", "content": "/no_think\nPROMPT"}]
    assert calls[0]["max_tokens"] == enrichment_service.LOCAL_MAX_TOKENS
    assert calls[0]["system_prompt"] == enrichment_service.LOCAL_SYSTEM_PROMPT
    assert calls[0]["model"] == "qwen3:8b"


async def test_local_dispatch_retries_after_a_successful_recovery(monkeypatch):
    calls = _patch_local_dispatch(
        monkeypatch,
        results=[ConnectionError("gateway down"), {"content": "OK"}],
    )

    assert await _dispatch_local() == "OK"
    assert len(calls) == 2


async def test_local_dispatch_reraises_when_recovery_is_not_ready(monkeypatch):
    calls = _patch_local_dispatch(
        monkeypatch,
        results=[ConnectionError("gateway down")],
        recovery_ready=False,
    )

    with pytest.raises(ConnectionError):
        await _dispatch_local()
    assert len(calls) == 1


async def test_local_dispatch_stops_after_the_retry_limit(monkeypatch):
    calls = _patch_local_dispatch(
        monkeypatch,
        results=[ConnectionError("down"), ConnectionError("still down")],
        retry_limit=1,
    )

    with pytest.raises(ConnectionError, match="still down"):
        await _dispatch_local()
    assert len(calls) == 2


async def test_local_dispatch_recovery_is_ollama_only(monkeypatch):
    """`eligible` gates on provider_type == "ollama" — openai must not retry."""
    calls = _patch_local_dispatch(monkeypatch, results=[ConnectionError("down")])

    with pytest.raises(ConnectionError):
        await _dispatch_local(provider_type="openai")
    assert len(calls) == 1


async def test_local_dispatch_defaults_missing_content_to_empty_string(monkeypatch):
    _patch_local_dispatch(monkeypatch, results=[{}])
    assert await _dispatch_local() == ""
