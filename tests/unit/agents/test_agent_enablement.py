"""Turning an agent off, and every place that lookup refuses or omits it (#1543)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.agents.builtins import AgentProfile
from core.agents.enablement import DISABLED_AGENTS_KEY, disabled_agent_ids
from core.storage.models import ConfigAuditLog, SystemConfig
from core.workflows.custom_workflow_service import _validate_agent_ids
from core.workflows.playbook_resolver import UnknownPlaybook, _profile_for
from services.api.routers import agents as agents_router
from services.api.routers import claude

pytestmark = pytest.mark.unit

CUSTOM = "custom-foo"


class _Query:
    def __init__(self, rows, model):
        self._rows, self._model, self._filters = rows, model, {}

    def filter_by(self, **kwargs):
        self._filters = kwargs
        return self

    def first(self):
        for row in self._rows:
            if type(row) is self._model and all(
                getattr(row, k) == v for k, v in self._filters.items()
            ):
                return row
        return None


class _Session:
    def __init__(self):
        self.rows: list = []

    def query(self, model):
        return _Query(self.rows, model)

    def add(self, obj):
        if obj not in self.rows:
            self.rows.append(obj)

    def commit(self):
        return None


@pytest.fixture
def session(monkeypatch):
    s = _Session()

    @contextmanager
    def fake_session():
        yield s

    monkeypatch.setattr("core.storage.config_service.get_session", fake_session)
    return s


@pytest.fixture
def custom_agent(monkeypatch):
    profile = AgentProfile(
        id=CUSTOM,
        name="Foo",
        description="",
        system_prompt="CUSTOM PROMPT",
        icon="C",
        color="#888888",
        specialization="Custom",
        recommended_tools=[],
    )
    monkeypatch.setitem(agents_router.agent_manager.agents, CUSTOM, profile)
    monkeypatch.setattr(agents_router.agent_manager, "refresh_custom_agents", lambda: 0)
    monkeypatch.setattr(
        "core.agents.manager.AgentManager",
        lambda: SimpleNamespace(agents={CUSTOM: profile}),
    )
    return profile


@pytest.fixture
def client(authenticate_app, session, custom_agent):
    app = FastAPI()
    app.include_router(agents_router.router, prefix="/api")
    authenticate_app(app)
    return TestClient(app)


def _enabled(client, agent_id):
    rows = client.get("/api/agents").json()["agents"]
    return next(r["enabled"] for r in rows if r["id"] == agent_id)


@pytest.mark.parametrize("agent_id", ["triage", CUSTOM])
def test_toggle_round_trip(client, session, agent_id):
    assert _enabled(client, agent_id) is True

    off = client.put(f"/api/agents/{agent_id}/enabled", json={"enabled": False})
    assert off.status_code == 200, off.text
    assert _enabled(client, agent_id) is False
    assert agent_id in disabled_agent_ids()
    # Other agents stay on.
    assert _enabled(client, "investigator" if agent_id == "triage" else "triage")

    client.put(f"/api/agents/{agent_id}/enabled", json={"enabled": True})
    assert _enabled(client, agent_id) is True
    assert disabled_agent_ids() == set()


def test_toggle_is_audited_as_the_user(client, session):
    client.put("/api/agents/triage/enabled", json={"enabled": False})
    audit = [r for r in session.rows if isinstance(r, ConfigAuditLog)]
    assert [a.config_key for a in audit] == [DISABLED_AGENTS_KEY]
    assert audit[0].changed_by == "test-admin"
    assert "triage" in audit[0].change_reason
    assert audit[0].new_value == {"ids": ["triage"]}


def test_noop_toggle_succeeds_without_a_write(client, session):
    assert (
        client.put("/api/agents/triage/enabled", json={"enabled": True}).status_code
        == 200
    )
    assert session.rows == []


def test_unknown_agent_is_404(client):
    r = client.put("/api/agents/nope/enabled", json={"enabled": False})
    assert r.status_code == 404


def test_failed_write_is_a_500(client, monkeypatch):
    monkeypatch.setattr(
        "services.api.routers.agents.set_agent_enabled", lambda *a: False
    )
    r = client.put("/api/agents/triage/enabled", json={"enabled": False})
    assert r.status_code == 500


def _disable(session, *ids):
    session.rows.append(
        SystemConfig(
            key=DISABLED_AGENTS_KEY, value={"ids": list(ids)}, config_type="agents"
        )
    )


def test_unknown_ids_in_the_list_are_ignored(session):
    _disable(session, "gone", "triage")
    assert disabled_agent_ids() == {"gone", "triage"}
    assert _profile_for("investigator")  # unaffected


def test_profile_for_refuses_a_disabled_agent(session, custom_agent):
    _disable(session, "triage", CUSTOM)
    for agent_id in ("triage", CUSTOM):
        with pytest.raises(UnknownPlaybook, match=f"{agent_id} is turned off"):
            _profile_for(agent_id)
    assert _profile_for("investigator")


def test_missing_agent_is_still_reported_as_missing(session):
    _disable(session, "nope")
    with pytest.raises(UnknownPlaybook, match="does not exist"):
        _profile_for("nope")


def test_run_start_returns_the_refusal_to_the_operator(session, monkeypatch):
    from core.workflows import playbooks_router

    monkeypatch.setattr(playbooks_router, "authorise", lambda *_: None)

    _disable(session, "triage")
    definition = SimpleNamespace(
        run_kind="compose", phases=[{"id": "p1", "agent_id": "triage"}]
    )
    workflows = SimpleNamespace(get_workflow=lambda _id: definition)
    with pytest.raises(HTTPException) as exc:
        playbooks_router.get_playbook(
            "wf-x",
            authorization=None,
            workflows=workflows,
            registry=MagicMock(),
        )
    assert exc.value.status_code == 404
    assert "triage is turned off" in exc.value.detail


@pytest.mark.asyncio
async def test_chat_refuses_a_disabled_agent_and_serves_an_enabled_one(
    session, monkeypatch
):
    _disable(session, "triage")
    monkeypatch.setattr(
        claude, "_resolve_provider_model_for_request", lambda *_: (None, "m")
    )
    monkeypatch.setattr(
        claude, "provider_for", lambda _: SimpleNamespace(provider_type="gemini")
    )
    monkeypatch.setattr(claude, "model_for", lambda _p, model: model)
    monkeypatch.setattr(claude, "live_mcp_tools", lambda _: [])

    async def _relay(*_args):
        yield ""

    monkeypatch.setattr(claude, "_relay", _relay)

    def ask(agent_id):
        return claude.chat_stream(
            claude.ChatRequest(
                messages=[{"role": "user", "content": "hi"}], agent_id=agent_id
            ),
            current_user=SimpleNamespace(username="nestor", user_id="u-1"),
            registry=MagicMock(),
        )

    with pytest.raises(HTTPException) as exc:
        await ask("triage")
    assert exc.value.status_code == 409
    assert "triage is turned off" in exc.value.detail

    assert await ask("investigator") is not None


def test_generators_omit_a_disabled_agent(session):
    from core.agents.agent_ai_generator import AgentAIGenerator
    from core.workflows.workflow_ai_generator import WorkflowAIGenerator

    _disable(session, "triage")
    for gen in (AgentAIGenerator, WorkflowAIGenerator):
        context = gen.__new__(gen)._agents_context()
        assert "`triage`" not in context
        assert "`investigator`" in context


def test_saving_a_workflow_that_names_a_disabled_agent_still_works(
    session, custom_agent
):
    _disable(session, CUSTOM)
    _validate_agent_ids([{"agent_id": CUSTOM}])
