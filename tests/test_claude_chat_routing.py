"""Unit tests for the reconciled non-Anthropic chat routing in
``backend/api/claude.py``.

Background: ``main`` merged #348 ("route local Ollama providers through
Bifrost") while this branch carried an overlapping non-Anthropic routing
change. The reconciliation kept #348's ``provider_id::model_id`` parsing and
no-tools guardrail prompt, and added a fallback to the *configured default*
provider so the redesigned Chat dock — which sends a **bare** model id — still
routes to a non-Anthropic provider instead of 503-ing on Ollama-only
deployments. These tests pin that behaviour.

The module is loaded via ``importlib`` so the pure helper functions can be
exercised without importing the whole ``backend.api`` package (which pulls in
auth/DB through its ``__init__``).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
# DEV_MODE so importing the endpoint module (via services.claude_service) does
# not trip the production JWT-secret guard.
os.environ.setdefault("DEV_MODE", "true")
for _p in (str(REPO), str(REPO / "backend")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from services.llm_router import ProviderSpec  # noqa: E402

pytestmark = pytest.mark.unit

# Synthetic stand-in for "some Anthropic model id". The routing logic only
# branches on the ``claude-`` prefix, never on a specific version, so we
# deliberately avoid pinning a real Sonnet/Opus id here (the whole point of the
# branch is to stop hardcoding model names — see services/defaults.DEFAULT_MODEL).
A_CLAUDE_MODEL = "claude-unit-test"
AN_OLLAMA_MODEL = "llama3.1:8b"


def _load_claude_module():
    """Load backend/api/claude.py as a standalone module, bypassing the
    backend.api package __init__ (auth/DB). Skip the suite if its imports are
    unavailable in this environment."""
    spec = importlib.util.spec_from_file_location(
        "claude_api_under_test", str(REPO / "backend" / "api" / "claude.py")
    )
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(
            f"backend.api.claude not importable here: {exc}",
            allow_module_level=True,
        )
    return mod


claude = _load_claude_module()


def _spec(
    provider_type: str = "ollama",
    provider_id: str = "ollama-local",
    default_model: str | None = None,
) -> ProviderSpec:
    if default_model is None:
        default_model = (
            A_CLAUDE_MODEL if provider_type == "anthropic" else AN_OLLAMA_MODEL
        )
    return ProviderSpec(
        provider_id=provider_id,
        provider_type=provider_type,
        base_url="http://localhost:11434" if provider_type == "ollama" else None,
        api_key_ref=None,
        default_model=default_model,
        config={},
    )


# --- _resolve_provider_model_for_request -----------------------------------


def test_bare_model_id_has_no_provider():
    # The redesigned Chat dock sends a bare model id (no "::").
    assert claude._resolve_provider_model_for_request("qwen3-coder:latest", None) == (
        None,
        "qwen3-coder:latest",
    )


def test_scoped_model_id_splits_provider_and_model():
    # provider_id::model_id form persisted by older Chat UI state (#348).
    assert claude._resolve_provider_model_for_request(
        "ollama-local::llama3.1:8b", None
    ) == ("ollama-local", "llama3.1:8b")


def test_scoped_model_id_with_empty_provider_falls_back_to_none():
    assert claude._resolve_provider_model_for_request("::gpt-4o-mini", None) == (
        None,
        "gpt-4o-mini",
    )


def test_unspecified_model_falls_back_to_default_model(monkeypatch):
    # No request model and no registry hit → the centralised DEFAULT_MODEL,
    # NOT a hardcoded Claude id.
    class _Reg:
        def resolve_model_for_component(self, *a, **k):
            return None

    monkeypatch.setattr(claude, "get_registry", lambda: _Reg())
    assert claude._resolve_provider_model_for_request(None, None) == (
        None,
        claude.DEFAULT_MODEL,
    )


def test_unspecified_model_uses_registry_tuple(monkeypatch):
    class _Reg:
        def resolve_model_for_component(self, *a, **k):
            return ("ollama-local", "llama3.1:8b")

    monkeypatch.setattr(claude, "get_registry", lambda: _Reg())
    assert claude._resolve_provider_model_for_request(None, None) == (
        "ollama-local",
        "llama3.1:8b",
    )


# --- _select_active_provider ------------------------------------------------


def test_explicit_provider_id_wins(monkeypatch):
    import services.llm_router as r

    oll = _spec()
    anthropic_default = _spec(
        provider_type="anthropic", provider_id="anthropic-default"
    )
    monkeypatch.setattr(
        r, "get_provider_spec", lambda pid: oll if pid == "ollama-local" else None
    )
    monkeypatch.setattr(r, "get_default_provider_spec", lambda: anthropic_default)
    assert claude._select_active_provider("ollama-local") is oll


def test_no_provider_id_falls_back_to_default(monkeypatch):
    # Bare model id (redesign Chat dock) → no provider_id → use the default.
    import services.llm_router as r

    default = _spec()
    monkeypatch.setattr(r, "get_provider_spec", lambda pid: None)
    monkeypatch.setattr(r, "get_default_provider_spec", lambda: default)
    assert claude._select_active_provider(None) is default


def test_unknown_provider_id_falls_back_to_default(monkeypatch):
    import services.llm_router as r

    default = _spec()
    monkeypatch.setattr(r, "get_provider_spec", lambda pid: None)
    monkeypatch.setattr(r, "get_default_provider_spec", lambda: default)
    assert claude._select_active_provider("ghost") is default


def test_provider_lookup_error_degrades_to_default(monkeypatch):
    import services.llm_router as r

    default = _spec(provider_type="anthropic", provider_id="anthropic-default")

    def _boom(pid):
        raise RuntimeError("db down")

    monkeypatch.setattr(r, "get_provider_spec", _boom)
    monkeypatch.setattr(r, "get_default_provider_spec", lambda: default)
    # A transient lookup error must not 500 — it degrades to the default.
    assert claude._select_active_provider("ollama-local") is default


def test_no_provider_anywhere_returns_none(monkeypatch):
    import services.llm_router as r

    monkeypatch.setattr(r, "get_provider_spec", lambda pid: None)
    monkeypatch.setattr(r, "get_default_provider_spec", lambda: None)
    assert claude._select_active_provider(None) is None


# --- _router_model ----------------------------------------------------------


def test_stale_claude_model_pinned_to_ollama_default():
    # Any claude-* selection on a non-Anthropic provider would 404 at Bifrost —
    # pin it to the provider's own default model.
    assert claude._router_model(_spec(), A_CLAUDE_MODEL) == AN_OLLAMA_MODEL


def test_non_claude_model_passes_through():
    assert claude._router_model(_spec(), "qwen3-coder:latest") == "qwen3-coder:latest"


def test_claude_model_kept_for_anthropic_provider():
    anth = _spec(provider_type="anthropic", provider_id="a")
    # On an Anthropic provider a claude-* model is valid and must pass through.
    assert claude._router_model(anth, A_CLAUDE_MODEL) == A_CLAUDE_MODEL


def test_none_requested_uses_provider_default():
    assert claude._router_model(_spec(), None) == AN_OLLAMA_MODEL


# --- guardrail prompt -------------------------------------------------------


def test_router_guardrail_prompt_forbids_tools():
    p = claude.ROUTER_NO_TOOLS_SYSTEM_PROMPT
    assert "no executable tools" in p
    # Must not invite tool/placeholder hallucination on the no-tools path.
    assert "Do not" in p


# --- end-to-end routing decision (the use_router contract) ------------------


@pytest.mark.parametrize(
    "provider_id, default_type, expect_router",
    [
        (None, "ollama", True),  # bare id + ollama default → route (redesign)
        (None, "anthropic", False),  # anthropic default → ClaudeService path
        ("ollama-local", "anthropic", True),  # explicit ollama beats default
        (None, None, False),  # nothing configured → ClaudeService 503 gate
    ],
)
def test_use_router_decision(monkeypatch, provider_id, default_type, expect_router):
    import services.llm_router as r

    explicit = _spec() if provider_id else None
    default = (
        _spec(provider_type=default_type, provider_id="default")
        if default_type
        else None
    )
    monkeypatch.setattr(r, "get_provider_spec", lambda pid: explicit)
    monkeypatch.setattr(r, "get_default_provider_spec", lambda: default)

    active = claude._select_active_provider(provider_id)
    # Mirrors the inline gate in chat()/chat_stream().
    use_router = (
        active is not None and getattr(active, "provider_type", None) != "anthropic"
    )
    assert use_router is expect_router
