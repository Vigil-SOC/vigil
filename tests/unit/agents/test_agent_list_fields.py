"""The agent list's model, skills, changes, and seven-day run fields."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.agents import manager as manager_module
from core.agents.builtins import AgentProfile
from core.agents.manager import AgentManager
from core.llm.providers import registry
from core.storage.models import (
    ChatMessage,
    Conversation,
    WorkflowRun,
    WorkflowRunPhase,
)
from core.storage.unit_of_work import unit_of_work

NOW = datetime(2099, 6, 1, 12, 0, 0)
EDGE = NOW - timedelta(days=7)
SECOND = timedelta(seconds=1)


@pytest.fixture
def agents(monkeypatch):
    """Rows by id, with no DB: no assignments, no default provider, no stats."""
    monkeypatch.setattr(manager_module, "_read_assignments", lambda: {})
    monkeypatch.setattr(
        "core.llm.router.router.get_default_provider_spec", lambda: None
    )
    monkeypatch.setattr(manager_module, "agent_run_stats", lambda: {})

    def rows(*customs):
        monkeypatch.setattr(
            AgentManager,
            "refresh_custom_agents",
            lambda self: self._swap_customs({p.id: p for p in customs}),
        )
        return {r["id"]: r for r in AgentManager().get_agent_list()}

    return rows


def custom(agent_id, tools=(), **kw):
    return AgentProfile(
        id=agent_id,
        name=agent_id,
        description="",
        system_prompt="",
        icon="C",
        color="#888888",
        specialization="Custom",
        recommended_tools=list(tools),
        **kw,
    )


def test_model_source_agent_assignment_default_and_none(agents, monkeypatch):
    pinned = custom("custom-pinned", model="claude-pinned", component_category="triage")

    row = agents(pinned)["custom-pinned"]
    assert (row["model"], row["model_source"]) == ("claude-pinned", "agent")
    assert row["component_category"] == "triage"

    # Nothing configured.
    triage = agents()["triage"]
    assert (triage["model"], triage["model_source"]) == (None, None)

    monkeypatch.setattr(
        "core.llm.router.router.get_default_provider_spec",
        lambda: SimpleNamespace(default_model="provider-default"),
    )
    assert (agents()["triage"]["model"], agents()["triage"]["model_source"]) == (
        "provider-default",
        "default",
    )

    monkeypatch.setattr(
        manager_module,
        "_read_assignments",
        lambda: {"chat_default": "chat-model", "triage": "triage-model"},
    )
    rows = agents(pinned)
    assert rows["triage"]["model_source"] == "assignment"
    assert rows["triage"]["model"] == "triage-model"
    # reporter has no assignment of its own, so chat_default applies.
    assert (rows["reporter"]["model"], rows["reporter"]["model_source"]) == (
        "chat-model",
        "default",
    )
    # An agent's own model beats every assignment.
    assert rows["custom-pinned"]["model_source"] == "agent"


def test_model_label_is_display_name_when_registry_lists_it(agents, monkeypatch):
    monkeypatch.setattr(
        registry,
        "_LIVE_META",
        {("anthropic", "claude-x"): {"display_name": "Claude X"}},
    )
    monkeypatch.setattr(
        manager_module, "_read_assignments", lambda: {"triage": "claude-x"}
    )
    assert agents()["triage"]["model"] == "Claude X"
    monkeypatch.setattr(
        manager_module, "_read_assignments", lambda: {"triage": "other"}
    )
    assert agents()["triage"]["model"] == "other"


def test_skills_count_needs_read_skill(agents):
    assert agents()["triage"]["skills"] > 0
    assert agents(custom("custom-bare", ["create_case"]))["custom-bare"]["skills"] == 0
    assert agents(custom("custom-read", ["read_skill"]))["custom-read"]["skills"] > 0


def test_changes_per_agent(agents):
    rows = agents()
    assert rows["triage"]["changes"] == "read_only"
    assert rows["investigator"]["changes"] == "asks_first"
    # ART execute is gated, so it asks first rather than acting on its own.
    assert rows["mitre_analyst"]["changes"] == "asks_first"
    # Ungated Cloudflare tools win over its approval tool.
    assert rows["auto_responder"]["changes"] == "on_its_own"


def test_changes_for_custom_tools():
    from core.llm.chat_layers import changes_for_tools

    assert changes_for_tools(["create_case", "update_case"]) == "read_only"
    assert changes_for_tools(["create_approval_action"]) == "asks_first"
    assert changes_for_tools(["create_approval_action", "block_ip"]) == "on_its_own"


def test_stats_failure_nulls_the_fields_and_keeps_the_list(agents, monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(manager_module, "agent_run_stats", boom)
    row = agents()["triage"]
    assert (row["runs_7d"], row["success_rate"], row["success_level"]) == (
        None,
        None,
        None,
    )
    assert row["changes"] == "read_only"


@pytest.mark.database
@pytest.mark.external_service
def test_runs_success_and_level_from_phases_and_chats(throwaway_database, monkeypatch):
    monkeypatch.setattr(manager_module, "_read_assignments", lambda: {})
    monkeypatch.setattr("core.agents.run_stats.utcnow", lambda: NOW)

    def phase(run, phase_id, agent, status, started):
        return WorkflowRunPhase(
            run_id=run,
            phase_id=phase_id,
            phase_order=1,
            agent_id=agent,
            status=status,
            started_at=started,
        )

    def turn(conv, seq, role, complete, at):
        return ChatMessage(
            conversation_id=conv,
            seq=seq,
            role=role,
            content="x",
            complete=complete,
            created_at=at,
        )

    with unit_of_work() as session:
        session.add_all(
            [
                WorkflowRun(
                    run_id="al-run",
                    workflow_id="al-wf",
                    workflow_name="AL",
                    status="completed",
                    started_at=NOW,
                ),
                # triage: 2 completed + 1 failed in window; 1 at the edge counts,
                # 1 just outside, a running one and one never started do not.
                phase("al-run", "a", "triage", "completed", NOW),
                phase("al-run", "b", "triage", "completed", EDGE),
                phase("al-run", "c", "triage", "failed", NOW),
                phase("al-run", "d", "triage", "completed", EDGE - SECOND),
                phase("al-run", "e", "triage", "running", NOW),
                phase("al-run", "f", "triage", "completed", None),
                Conversation(id="al-conv", agent_id="triage"),
                Conversation(id="al-anon", agent_id=None),
            ]
        )
        session.flush()
        session.add_all(
            [
                turn("al-conv", 0, "user", True, NOW),
                turn("al-conv", 1, "assistant", True, NOW),
                turn("al-conv", 2, "assistant", False, NOW),
                turn("al-conv", 3, "assistant", True, EDGE - SECOND),
                turn("al-anon", 0, "assistant", True, NOW),
            ]
        )
    try:
        row = {r["id"]: r for r in AgentManager().get_agent_list()}
        triage = row["triage"]
        # Phases: completed x2, failed x1. Chat: complete x1, incomplete x1.
        assert triage["runs_7d"] == 5
        assert triage["success_rate"] == pytest.approx(3 / 5)
        assert triage["success_level"] == "poor"
        reporter = row["reporter"]
        assert (reporter["runs_7d"], reporter["success_rate"]) == (0, None)
        assert reporter["success_level"] is None
    finally:
        with unit_of_work() as session:
            session.query(ChatMessage).filter(
                ChatMessage.conversation_id.in_(["al-conv", "al-anon"])
            ).delete(synchronize_session=False)
            session.query(Conversation).filter(
                Conversation.id.in_(["al-conv", "al-anon"])
            ).delete(synchronize_session=False)
            session.query(WorkflowRunPhase).filter(
                WorkflowRunPhase.run_id == "al-run"
            ).delete(synchronize_session=False)
            session.query(WorkflowRun).filter(WorkflowRun.run_id == "al-run").delete(
                synchronize_session=False
            )
