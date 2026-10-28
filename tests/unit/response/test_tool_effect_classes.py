"""Each outside tool has one stored effect class (#3304)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from core.agents import tools_router
from core.response import ceiling
from core.response.ceiling import Ceiling, tighten_from_hints, tool_classes
from core.storage.models import ToolEffectClass
from services.api.routers import autonomy


def test_the_seed_keeps_what_the_verb_heuristic_said(seeded_tool_classes):
    assert seeded_tool_classes["palo-alto_pan_block_ip"] == "irreversible"
    assert seeded_tool_classes["atomic_red_team_execute"] == "irreversible"
    assert seeded_tool_classes["atomic-red-team_atomic_red_team_execute"] == (
        "irreversible"
    )
    assert seeded_tool_classes["misp_misp_search_ioc"] == "read_unverified"
    # Splunk's tool.py runs as splunk-selfhosted, not as the vendor server.
    assert "splunk-selfhosted_splunk_execute" in seeded_tool_classes
    assert not [n for n in seeded_tool_classes if n.startswith("splunk_")]
    assert not set(seeded_tool_classes) & ceiling.own_tool_names()


def test_a_new_name_is_unclassified_and_vigils_own_have_no_class(
    stored_tool_classes,
):
    assert tool_classes(["okta_suspend_user", "add_case_comment", "get_finding"]) == {
        "okta_suspend_user": None
    }


# --- The direct-action gate -------------------------------------------------


def _body(**kw):
    return tools_router.InvokeRequest(
        tool="x", args={}, bounds={"max_rows": 1, "timeout_ms": 1000}, **kw
    )


def test_the_gate_refuses_an_unclassified_tool_without_an_approval(
    stored_tool_classes,
):
    refusal = tools_router._direct_action_refusal(_body(), "okta_suspend_user", {})
    assert (
        refusal
        == "okta_suspend_user changes the estate and no approval covers this call"
    )


def test_the_gate_lets_a_read_class_through(stored_tool_classes):
    stored_tool_classes["misp_misp_search_ioc"] = "read_unverified"
    assert (
        tools_router._direct_action_refusal(_body(), "misp_misp_search_ioc", {}) is None
    )


# --- PUT /api/autonomy/tools/{name} -----------------------------------------


class _Session:
    """Enough of a Session for the handler; the trail skips a stand-in."""

    def __init__(self, rows):
        self.rows = {
            name: ToolEffectClass(
                tool_name=name, effect_class=effect, classified_by="system"
            )
            for name, effect in rows.items()
        }

    def get(self, _model, name):
        return self.rows.get(name)

    def add(self, row):
        self.rows[row.tool_name] = row

    def flush(self):
        pass


@pytest.fixture
def put(monkeypatch):
    """Call the PUT handler as a caller holding ``held``."""
    stopped = {"value": False}
    monkeypatch.setattr(
        autonomy, "read_ceiling", lambda: Ceiling(stopped=stopped["value"])
    )
    monkeypatch.setattr(
        autonomy,
        "live_mcp_tools",
        lambda _r: [{"name": "okta_suspend_user"}, {"name": "add_case_comment"}],
    )

    def call(name, effect, *, held, service=False, rows=None):
        def deny_unless(_user_id, permission, *_a, **_k):
            if permission not in held:
                raise HTTPException(status_code=403, detail=permission)

        monkeypatch.setattr(autonomy, "deny_unless", deny_unless)
        session = _Session(rows or {})
        user = SimpleNamespace(
            user_id="u-1", principal_id="u-1", service_account=service
        )
        try:
            entry = autonomy.put_tool_class(
                name,
                autonomy.ToolClassBody(effect_class=effect),
                session,
                current_user=user,
                registry=None,
            )
        except HTTPException as exc:
            return exc.status_code, exc.detail
        return 200, entry.effect_class

    call.stopped = stopped
    return call


def test_a_person_confirms_a_read(put):
    rows = {"misp_misp_search_ioc": "read_unverified"}
    assert put(
        "misp_misp_search_ioc", "read", held={"ai_autonomy.write"}, rows=rows
    ) == (200, "read")


def test_classifying_an_exposed_name_is_loosening(put):
    assert put("okta_suspend_user", "irreversible", held={"ai_autonomy.stop"})[0] == 403
    assert put("okta_suspend_user", "reversible", held={"ai_autonomy.write"}) == (
        200,
        "reversible",
    )


def test_a_service_account_can_tighten_and_cannot_loosen(put):
    held = {"ai_autonomy.stop", "ai_autonomy.write"}
    rows = {"misp_misp_search_ioc": "read_unverified"}
    assert put(
        "misp_misp_search_ioc", "irreversible", held=held, service=True, rows=rows
    ) == (200, "irreversible")
    assert (
        put("misp_misp_search_ioc", "read", held=held, service=True, rows=rows)[0]
        == 403
    )


def test_stop_alone_tightens_and_does_not_loosen(put):
    rows = {"misp_misp_search_ioc": "read_unverified"}
    assert put(
        "misp_misp_search_ioc", "reversible", held={"ai_autonomy.stop"}, rows=rows
    ) == (200, "reversible")
    assert put(
        "misp_misp_search_ioc", "read", held={"ai_autonomy.stop"}, rows=rows
    ) == (403, "ai_autonomy.write")


def test_loosening_while_stopped_is_refused_and_tightening_is_not(put):
    put.stopped["value"] = True
    held = {"ai_autonomy.stop", "ai_autonomy.write"}
    rows = {"misp_misp_search_ioc": "read_unverified"}
    assert put("misp_misp_search_ioc", "read", held=held, rows=rows) == (
        409,
        "Stopped: resume first",
    )
    assert put("misp_misp_search_ioc", "irreversible", held=held, rows=rows)[0] == 200


def test_a_name_nobody_exposes_or_vigil_answers_is_not_found(put):
    held = {"ai_autonomy.stop", "ai_autonomy.write"}
    assert put("nobody_has_this", "read", held=held)[0] == 404
    assert put("add_case_comment", "irreversible", held=held)[0] == 404


# --- MCP annotations only tighten -------------------------------------------


@pytest.mark.external_service
def test_destructive_hint_tightens_and_read_only_hint_does_not_loosen():
    from core.storage.unit_of_work import unit_of_work

    with unit_of_work() as session:
        for name, effect in (
            ("srv_wipe", "read_unverified"),
            ("srv_lookup", "irreversible"),
        ):
            session.add(
                ToolEffectClass(
                    tool_name=name, effect_class=effect, classified_by="system"
                )
            )
    tighten_from_hints(
        "srv",
        [
            {"name": "wipe", "annotations": {"destructiveHint": True}},
            {"name": "lookup", "annotations": {"readOnlyHint": True}},
            {"name": "new", "annotations": {"destructiveHint": True}},
        ],
    )
    names = ["srv_wipe", "srv_lookup", "srv_new"]
    with unit_of_work() as session:
        stored = ceiling.stored_classes(session, names)
    # A hint never writes a row for a name that has none.
    assert stored == {"srv_wipe": "irreversible", "srv_lookup": "irreversible"}


def test_hints_apply_once_per_connect(monkeypatch):
    from core.integrations.mcp import registry as mcp_registry

    calls = []
    monkeypatch.setattr(mcp_registry, "_hinted", {})
    monkeypatch.setattr(
        ceiling,
        "tighten_from_hints",
        lambda server, tools: calls.append(server) or True,
    )
    reg = mcp_registry.MCPRegistry()
    first = [{"name": "wipe", "annotations": {"destructiveHint": True}}]
    mcp_registry._register(reg, "srv", first)
    mcp_registry._register(reg, "srv", first)  # a per-turn refresh
    assert calls == ["srv"]
    # A reconnect builds a new list, and the hints apply again.
    mcp_registry._register(reg, "srv", list(first))
    assert calls == ["srv", "srv"]
