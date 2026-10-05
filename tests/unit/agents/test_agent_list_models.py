"""GET /agents carries each agent's model and where it comes from."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.agents.manager import AgentManager  # noqa: E402
from core.llm.providers.registry import ComponentAssignment  # noqa: E402

pytestmark = pytest.mark.unit


def _listing(monkeypatch, assignments, default_model="provider-model"):
    monkeypatch.setattr(
        "core.llm.providers.registry.get_registry",
        lambda: SimpleNamespace(get_all_assignments=lambda: assignments),
    )
    monkeypatch.setattr(
        "core.llm.router.router.get_default_provider_spec",
        lambda: SimpleNamespace(default_model=default_model) if default_model else None,
    )
    mgr = AgentManager()
    triage = mgr.agents["triage"]
    mgr.agents["custom-x"] = replace(
        triage, id="custom-x", model="my-model", component_category="reporting"
    )
    return {a["id"]: a for a in mgr.get_agent_list()}


def _assign(component, model):
    return ComponentAssignment(component=component, provider_id="p", model_id=model)


def test_model_follows_agent_then_category_then_chat_default_then_provider(monkeypatch):
    by_id = _listing(
        monkeypatch,
        {"triage": _assign("triage", "haiku"), "chat_default": _assign("chat_default", "sonnet")},
    )
    assert by_id["triage"]["model"] == "haiku"
    assert by_id["triage"]["model_source"] == "triage"
    assert by_id["triage"]["component_category"] == "triage"
    # no investigation assignment: falls to chat_default
    assert by_id["investigator"]["model"] == "sonnet"
    assert by_id["investigator"]["model_source"] == "chat_default"
    # an agent's own model wins over its category's assignment
    assert (by_id["custom-x"]["model"], by_id["custom-x"]["model_source"]) == ("my-model", "agent")


def test_model_falls_back_to_provider_default_then_nothing(monkeypatch):
    by_id = _listing(monkeypatch, {})
    assert (by_id["triage"]["model"], by_id["triage"]["model_source"]) == ("provider-model", "default")

    by_id = _listing(monkeypatch, {}, default_model=None)
    assert by_id["triage"]["model"] is None
    assert by_id["triage"]["model_source"] is None
