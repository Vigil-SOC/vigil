"""Forking a built-in with the drawer's edits: one Save makes one row, a bad Save none (#1729)."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.storage.models import Base, ConfigAuditLog, CustomAgent
from services.api.middleware.auth import get_current_active_user
from services.api.routers import agents as agents_router
from services.api.routers import custom_agents

pytestmark = pytest.mark.unit


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture
def db(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(
        engine, tables=[CustomAgent.__table__, ConfigAuditLog.__table__]
    )
    maker = sessionmaker(bind=engine)

    @contextmanager
    def session_scope():
        s = maker()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    monkeypatch.setattr(
        "core.agents.custom_agent_service.get_db_manager",
        lambda: SimpleNamespace(session_scope=session_scope),
    )
    monkeypatch.setattr(
        agents_router.agent_manager, "refresh_custom_agents", lambda: None
    )
    return session_scope


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(custom_agents.router, prefix="/api")
    app.include_router(agents_router.router, prefix="/api")
    app.dependency_overrides[get_current_active_user] = lambda: SimpleNamespace(
        user_id="u-1"
    )
    return TestClient(app)


def _rows(db):
    with db() as s:
        return {r.id: r for r in s.query(CustomAgent).all()}


def test_plain_fork_copies_the_built_in(client, db):
    built_in = agents_router.agent_manager.agents["triage"]
    res = client.post("/api/agents/triage/fork")
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == f"{built_in.name} (copy)"
    assert body["system_prompt_override"] == built_in.system_prompt
    assert body["forked_from"] == "triage"
    assert len(_rows(db)) == 1


def test_overrides_land_in_the_one_insert_and_the_edited_prompt_wins(client, db):
    built_in = agents_router.agent_manager.agents["triage"]
    before = (built_in.name, built_in.system_prompt, built_in.max_tokens)
    res = client.post(
        "/api/agents/triage/fork",
        json={
            "name": "Careful triage",
            "system_prompt_override": "Be careful.",
            "max_tokens": 8192,
            "enable_thinking": True,
            "recommended_tools": ["search_logs"],
            "model": "claude-sonnet-5",
            "fallback_model": None,
        },
    )
    assert res.status_code == 201
    body = res.json()
    assert body["id"] == "custom-careful-triage"
    assert body["system_prompt_override"] == "Be careful."
    assert (body["max_tokens"], body["enable_thinking"]) == (8192, True)
    assert body["recommended_tools"] == ["search_logs"]
    assert body["model"] == "claude-sonnet-5"
    assert body["description"] == built_in.description  # unsent fields still copy
    assert list(_rows(db)) == ["custom-careful-triage"]
    # the built-in is untouched, in memory and as served
    assert (built_in.name, built_in.system_prompt, built_in.max_tokens) == before
    served = client.get("/api/agents/triage").json()
    assert served["name"] == before[0] and served["system_prompt"] == before[1]


def test_a_failed_save_leaves_no_row(client, db):
    too_long = "x" * (1024 * 1024)
    for bad in ({"name": "   "}, {"system_prompt_override": too_long}):
        res = client.post("/api/agents/triage/fork", json=bad)
        assert res.status_code in (400, 422), res.text
    assert _rows(db) == {}


def test_built_in_detail_carries_prompt_model_and_category(client):
    body = client.get("/api/agents/triage").json()
    built_in = agents_router.agent_manager.agents["triage"]
    assert body["system_prompt"] == built_in.system_prompt
    assert body["model"] == built_in.model
    assert body["fallback_model"] == built_in.fallback_model
    assert body["component_category"] == "triage"
