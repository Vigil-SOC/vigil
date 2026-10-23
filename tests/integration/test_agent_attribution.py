"""Agent attribution (#2763) on a database ``python -m core.upgrade`` has reached.

A child run's trail entry resolves to the person who started the parent. A
read-only tool is a ledger call and not a trail row. A forged principal is a
401 and writes nothing.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from core.agents import internal_auth, tools_router
from core.agents.attribution import note_child, note_run_start, originating_person
from core.auth import tool_principal
from core.integrations.mcp.registry import MCPRegistry
from core.storage.connection import get_db_manager, get_db_session
from tests.integration.test_upgrade_command import _run, _scratch

pytestmark = [pytest.mark.integration, pytest.mark.database]

PERSON = "11111111-1111-4111-8111-111111111111"
PARENT = "22222222-2222-4222-8222-222222222222"
CHILD = "33333333-3333-4333-8333-333333333333"
AUTH = {"Authorization": "Bearer shhh"}


@pytest.fixture
def db(tmp_path):
    with _scratch(False) as engine:
        code, report = _run(engine, tmp_path, restore_point="rp-2763")
        assert code == 0, report
        manager = get_db_manager()
        saved = manager._engine, manager._session_factory
        manager._engine = engine
        manager._session_factory = sessionmaker(bind=engine)
        try:
            yield engine
        finally:
            manager._engine, manager._session_factory = saved


def _claims(tool: str) -> str:
    return tool_principal.mint(
        "nestor",
        ttl=tool_principal.RUN_TTL,
        person_id=PERSON,
        agent_id="agent:hunt",
        run_id=PARENT,
        origin="person",
    )


def _client(monkeypatch):
    monkeypatch.setattr(internal_auth, "get_secret", lambda name: "shhh")
    app = FastAPI()
    app.state.mcp_registry = MCPRegistry()
    app.include_router(tools_router.router, prefix=tools_router.ROUTER_META.prefix)
    return TestClient(app)


def _invoke(client, tool: str, principal: str):
    return client.post(
        "/internal/tools/invoke",
        headers=AUTH,
        json={
            "tool": tool,
            "args": {"case_id": "case-1"},
            "bounds": {"max_rows": 5, "timeout_ms": 5000},
            "principal": principal,
        },
    )


def _trail_actions(engine) -> list[str]:
    # The upgrade writes roles.revised before the test. These assertions are
    # about the rows the tool call added.
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT payload->>'action' FROM audit_trail "
                    "WHERE payload->>'action' IS DISTINCT FROM 'roles.revised' "
                    "ORDER BY seq"
                )
            ).scalars()
        )


def test_a_child_run_resolves_to_the_person_who_started_it(db):
    note_run_start(
        run_id=PARENT,
        actor="agent:hunt",
        origin_kind="person",
        origin=PERSON,
        spec={"playbook": "workflow:threat-hunt"},
        model="openrouter/anthropic/claude-haiku-5.5",
    )
    session = get_db_session()
    try:
        note_child(
            session,
            parent_run_id=PARENT,
            child_run_id=CHILD,
            spec={"role": "critic", "key": "critic:1"},
            role="critic",
        )
        session.commit()
        assert originating_person(session, CHILD) == PERSON
        child = session.execute(
            text(
                "SELECT payload FROM audit_trail "
                "WHERE payload->>'run_id' = :run_id ORDER BY seq"
            ),
            {"run_id": CHILD},
        ).scalar_one()
        assert child["actor"] == "agent:hunt"
        assert child["origin_kind"] == "parent"
        assert child["origin"] == PARENT
        assert "critic" not in child["actor"]
    finally:
        session.close()


def test_a_read_only_tool_adds_a_ledger_event_and_no_trail_row(db, monkeypatch):
    async def _ran(tool, args, registry=None):
        return {"rows": [{"case_id": "case-1"}]}, True

    monkeypatch.setattr(tools_router, "execute_backend_tool", _ran)
    response = _invoke(_client(monkeypatch), "get_case", _claims("get_case"))

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert _trail_actions(db) == []

    # The agent service journals the call, with the principal id. This process
    # stands in for that write: the invoke path must not have added a trail row
    # beside it.
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO agent_events "
                "(run_id, run_kind, seq, kind, payload, schema_version) "
                "VALUES (CAST(:run_id AS uuid), 'hunt', 1, 'call', "
                "CAST(:payload AS jsonb), 1)"
            ),
            {
                "run_id": PARENT,
                "payload": json.dumps(
                    {
                        "tool": "get_case",
                        "actor": "agent:hunt",
                        "ok": True,
                        "arguments": "{}",
                        "result": "[]",
                    }
                ),
            },
        )
    with db.connect() as conn:
        kind = conn.execute(
            text(
                "SELECT kind FROM agent_events WHERE run_id = CAST(:run_id AS uuid)"
            ),
            {"run_id": PARENT},
        ).scalar_one()
    assert kind == "call"
    assert _trail_actions(db) == []


def test_an_effectful_tool_is_recorded_before_it_runs(db, monkeypatch):
    during: list[str] = []

    async def _ran(tool, args, registry=None):
        during.extend(_trail_actions(db))
        return {"closed": True}, True

    monkeypatch.setattr(tools_router, "execute_backend_tool", _ran)
    response = _invoke(_client(monkeypatch), "isolate_host", _claims("isolate_host"))

    assert response.status_code == 200, response.text
    assert during == ["agent.tool.intent"]
    actions = _trail_actions(db)
    assert actions == ["agent.tool.intent", "agent.tool.outcome"]
    with db.connect() as conn:
        payloads = list(
            conn.execute(
                text(
                    "SELECT payload FROM audit_trail "
                    "WHERE payload->>'action' IS DISTINCT FROM 'roles.revised' "
                    "ORDER BY seq"
                )
            ).scalars()
        )
    assert payloads[0]["tool"] == "isolate_host"
    assert payloads[0]["target"] == "case-1"
    assert "args" not in payloads[0]
    assert payloads[1]["outcome"] == "done"
    assert "rows" not in payloads[1]
    assert "closed" not in payloads[1]


def test_a_forged_principal_is_refused_and_writes_nothing(db, monkeypatch):
    called = {"n": 0}

    async def _ran(tool, args, registry=None):
        called["n"] += 1
        return {}, True

    monkeypatch.setattr(tools_router, "execute_backend_tool", _ran)
    response = _invoke(_client(monkeypatch), "isolate_host", "not-a-token")

    assert response.status_code == 401
    assert called["n"] == 0
    assert _trail_actions(db) == []
