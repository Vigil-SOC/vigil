"""case_records: empty sections for an unknown case, backend_error on a store failure."""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from core.agents import internal_auth, tools_router
from core.agents.tool_registry import execute_backend_tool
from core.integrations.mcp.registry import MCPRegistry
from core.llm.tool_schemas import ALL_TOOLS
from core.storage.models import Base, CaseEscalation, CaseTask

pytestmark = pytest.mark.unit


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture
def store(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[CaseTask.__table__, CaseEscalation.__table__]
    )
    session = sessionmaker(bind=engine)()

    @contextmanager
    def _this_store(_session=None):
        yield session

    monkeypatch.setattr("core.storage.unit_of_work.unit_of_work", _this_store)
    yield session
    session.close()


def test_schema_names_case_records():
    assert any(tool.get("name") == "case_records" for tool in ALL_TOOLS)


@pytest.mark.asyncio
async def test_unknown_case_returns_empty_sections(store):
    result, handled = await execute_backend_tool(
        "case_records", {"case_id": "c-does-not-exist"}
    )

    assert handled is True
    assert result == {"tasks": [], "escalations": []}


@pytest.mark.asyncio
async def test_returns_the_cases_tasks(store):
    store.add(CaseTask(case_id="c-1", title="Pull logs", priority="high"))
    store.add(CaseTask(case_id="c-2", title="Other case", priority="low"))
    store.add(
        CaseEscalation(
            case_id="c-1",
            escalated_from="analyst",
            escalated_to="lead",
            reason="Own case",
            urgency_level="high",
        )
    )
    store.add(
        CaseEscalation(
            case_id="c-2",
            escalated_from="analyst",
            escalated_to="lead",
            reason="Other case",
            urgency_level="high",
        )
    )
    store.commit()

    result, _ = await execute_backend_tool("case_records", {"case_id": "c-1"})

    assert [task["title"] for task in result["tasks"]] == ["Pull logs"]
    assert [escalation["reason"] for escalation in result["escalations"]] == ["Own case"]


@pytest.mark.asyncio
async def test_store_failure_is_backend_error(monkeypatch):
    monkeypatch.setattr(internal_auth, "get_secret", lambda name: "shhh")

    def _down():
        raise RuntimeError("database is down")

    monkeypatch.setattr("core.storage.unit_of_work.unit_of_work", _down)

    answer = await tools_router.invoke(
        tools_router.InvokeRequest(
            tool="case_records",
            args={"case_id": "c-1"},
            bounds={"max_rows": 5, "timeout_ms": 5000},
        ),
        "Bearer shhh",
        MCPRegistry(),
    )

    assert answer["ok"] is False
    assert answer["failure"] == {"kind": "backend_error", "detail": "database is down"}
