"""Unit tests for ``POST /api/findings/{id}/enrich`` (issue #470).

The enrichment flow moved to ``services/findings/enrichment/``; what's left in
the handler is cache policy plus the domain-error → status-code mapping. These
tests pin both, so the extraction is provably behaviour-preserving:

* 404 on a missing finding
* cached hit short-circuits (and ``force_regenerate`` bypasses it)
* ``NoProviderConfigured`` → 503 carrying the structured ``NO_PROVIDER_DETAIL``
  payload the chat drawer matches on — *not* a bare string
* ``ProviderUnavailable`` → 503, any other failure → 500
* the success envelope is ``{finding_id, cached: False, enrichment}``

They also cover ``_resolve_provider``'s error mapping, which is where the old
inline ``HTTPException`` raises used to live.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
# backend/ must be on sys.path too: importing backend.api.findings cascades
# into backend/api/__init__.py which does bare `from api.findings import ...`.
sys.path.insert(0, str(REPO / "backend"))

from fastapi import HTTPException  # noqa: E402

from backend.api import findings as findings_api  # noqa: E402
from services.findings.enrichment import (  # noqa: E402
    EmptyProviderResponse,
    FindingNotFound,
    NoProviderConfigured,
    ProviderUnavailable,
)
from services.findings.enrichment import service as enrichment_service  # noqa: E402

pytestmark = pytest.mark.unit

FINDING_ID = "f-20260803-001"


class _StubDataService:
    def __init__(self, finding=None):
        self.finding = finding

    def get_finding(self, finding_id):
        return self.finding


@pytest.fixture
def stub_data_service(monkeypatch):
    """Replace the module-level singleton with a stub holding one finding."""
    stub = _StubDataService({"finding_id": FINDING_ID, "severity": "high"})
    monkeypatch.setattr(findings_api, "data_service", stub)
    return stub


def _stub_enrich(monkeypatch, *, result=None, raises=None):
    """Patch the handler's `enrich` seam; return the recorded call kwargs."""
    calls = []

    async def fake_enrich(finding, **kwargs):
        calls.append((finding, kwargs))
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(findings_api, "enrich", fake_enrich)
    return calls


# ---------------------------------------------------------------------------
# Fetch + cache policy
# ---------------------------------------------------------------------------


async def test_missing_finding_is_404(monkeypatch):
    monkeypatch.setattr(findings_api, "data_service", _StubDataService(None))

    with pytest.raises(HTTPException) as exc_info:
        await findings_api.get_or_generate_enrichment(FINDING_ID, False)

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Finding not found"


async def test_existing_enrichment_is_returned_from_cache(monkeypatch):
    cached = {"threat_summary": "already analysed"}
    monkeypatch.setattr(
        findings_api,
        "data_service",
        _StubDataService({"finding_id": FINDING_ID, "ai_enrichment": cached}),
    )
    calls = _stub_enrich(monkeypatch, result={"threat_summary": "fresh"})

    response = await findings_api.get_or_generate_enrichment(FINDING_ID, False)

    assert response == {
        "finding_id": FINDING_ID,
        "cached": True,
        "enrichment": cached,
    }
    assert calls == [], "a cache hit must not call a provider"


async def test_force_regenerate_bypasses_the_cache(monkeypatch):
    monkeypatch.setattr(
        findings_api,
        "data_service",
        _StubDataService(
            {"finding_id": FINDING_ID, "ai_enrichment": {"threat_summary": "stale"}}
        ),
    )
    calls = _stub_enrich(monkeypatch, result={"threat_summary": "fresh"})

    response = await findings_api.get_or_generate_enrichment(FINDING_ID, True)

    assert response["cached"] is False
    assert response["enrichment"] == {"threat_summary": "fresh"}
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Success envelope + wiring
# ---------------------------------------------------------------------------


async def test_success_response_shape(stub_data_service, monkeypatch):
    enrichment = {"threat_summary": "exfil", "confidence_score": 0.9}
    _stub_enrich(monkeypatch, result=enrichment)

    response = await findings_api.get_or_generate_enrichment(FINDING_ID, False)

    assert response == {
        "finding_id": FINDING_ID,
        "cached": False,
        "enrichment": enrichment,
    }


async def test_handler_reuses_its_own_data_service_for_the_write(
    stub_data_service, monkeypatch
):
    """Otherwise the module would build a second DatabaseDataService."""
    calls = _stub_enrich(monkeypatch, result={})

    await findings_api.get_or_generate_enrichment(FINDING_ID, False)

    finding, kwargs = calls[0]
    assert finding is stub_data_service.finding
    assert kwargs["data_service"] is stub_data_service


async def test_handler_passes_the_path_param_as_the_authoritative_id(monkeypatch):
    """The write target must be the path param, not the row's own key.

    The pre-extraction handler persisted with the path param. A row whose
    finding_id disagrees (or is absent) must not redirect or drop the write.
    """
    monkeypatch.setattr(
        findings_api,
        "data_service",
        _StubDataService({"finding_id": None, "severity": "high"}),
    )
    calls = _stub_enrich(monkeypatch, result={})

    await findings_api.get_or_generate_enrichment(FINDING_ID, False)

    _, kwargs = calls[0]
    assert kwargs["finding_id"] == FINDING_ID


# ---------------------------------------------------------------------------
# Domain error → status code
# ---------------------------------------------------------------------------


async def test_no_provider_configured_is_503_with_the_structured_detail(
    stub_data_service, monkeypatch
):
    from backend.api.claude import NO_PROVIDER_DETAIL

    _stub_enrich(monkeypatch, raises=NoProviderConfigured("nothing resolved"))

    with pytest.raises(HTTPException) as exc_info:
        await findings_api.get_or_generate_enrichment(FINDING_ID, False)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == NO_PROVIDER_DETAIL
    assert exc_info.value.detail["code"] == "no_llm_provider_configured"


async def test_provider_unavailable_is_503_with_the_message(
    stub_data_service, monkeypatch
):
    _stub_enrich(
        monkeypatch,
        raises=ProviderUnavailable("Configured provider 'ollama-x' is unavailable"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await findings_api.get_or_generate_enrichment(FINDING_ID, False)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Configured provider 'ollama-x' is unavailable"


async def test_finding_not_found_from_the_module_is_404(stub_data_service, monkeypatch):
    _stub_enrich(monkeypatch, raises=FindingNotFound("Finding not found"))

    with pytest.raises(HTTPException) as exc_info:
        await findings_api.get_or_generate_enrichment(FINDING_ID, False)

    assert exc_info.value.status_code == 404


async def test_empty_provider_response_is_500(stub_data_service, monkeypatch):
    _stub_enrich(
        monkeypatch,
        raises=EmptyProviderResponse("LLM provider returned an empty response"),
    )

    with pytest.raises(HTTPException) as exc_info:
        await findings_api.get_or_generate_enrichment(FINDING_ID, False)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == (
        "Failed to generate enrichment: LLM provider returned an empty response"
    )


async def test_unexpected_failure_is_500(stub_data_service, monkeypatch):
    _stub_enrich(monkeypatch, raises=RuntimeError("gateway exploded"))

    with pytest.raises(HTTPException) as exc_info:
        await findings_api.get_or_generate_enrichment(FINDING_ID, False)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Failed to generate enrichment: gateway exploded"


# ---------------------------------------------------------------------------
# _resolve_provider — the raises that used to be inline HTTPExceptions
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self, resolved):
        self.resolved = resolved
        self.components = []

    def resolve_model_for_component(self, component):
        self.components.append(component)
        return self.resolved


def _patch_resolution(monkeypatch, *, resolved, provider_spec):
    from services import llm_router, model_registry

    registry = _FakeRegistry(resolved)
    monkeypatch.setattr(model_registry, "get_registry", lambda: registry)
    monkeypatch.setattr(llm_router, "get_provider_spec", lambda pid: provider_spec)
    return registry


def test_resolve_raises_no_provider_when_registry_resolves_nothing(monkeypatch):
    _patch_resolution(monkeypatch, resolved=None, provider_spec=None)

    with pytest.raises(NoProviderConfigured):
        enrichment_service._resolve_provider("reporting")


def test_resolve_raises_provider_unavailable_when_spec_is_missing(monkeypatch):
    _patch_resolution(
        monkeypatch, resolved=("ollama-x", "qwen3:8b"), provider_spec=None
    )

    with pytest.raises(ProviderUnavailable) as exc_info:
        enrichment_service._resolve_provider("reporting")

    assert str(exc_info.value) == "Configured provider 'ollama-x' is unavailable"


def test_resolve_skips_the_claude_service_for_non_anthropic_providers(monkeypatch):
    class _Spec:
        provider_id = "ollama-default"
        provider_type = "ollama"

    _patch_resolution(
        monkeypatch, resolved=("ollama-default", "qwen3:8b"), provider_spec=_Spec()
    )

    provider, model_id, claude_service = enrichment_service._resolve_provider(
        "reporting"
    )

    assert model_id == "qwen3:8b"
    assert provider.provider_type == "ollama"
    assert claude_service is None


def test_resolve_raises_no_provider_when_anthropic_has_no_api_key(monkeypatch):
    class _Spec:
        provider_id = "anthropic-default"
        provider_type = "anthropic"

    _patch_resolution(
        monkeypatch,
        resolved=("anthropic-default", "claude-opus-5"),
        provider_spec=_Spec(),
    )

    from services import claude_service as claude_service_module

    class _NoKeyClaudeService:
        def __init__(self, **kwargs):
            pass

        def has_api_key(self):
            return False

    monkeypatch.setattr(claude_service_module, "ClaudeService", _NoKeyClaudeService)

    with pytest.raises(NoProviderConfigured):
        enrichment_service._resolve_provider("reporting")
