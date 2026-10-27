"""case_records: the case's tasks, escalations and a page of its findings;
empty sections for an unknown case, backend_error on a store failure."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from core.agents import internal_auth, tools_router
from core.agents.tool_registry import execute_backend_tool
from core.integrations.mcp.registry import MCPRegistry
from core.llm.tool_schemas import ALL_TOOLS
from core.storage.models import (
    Base,
    CaseEscalation,
    CaseTask,
    Finding,
    case_findings,
)

pytestmark = pytest.mark.unit


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture
def store(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[CaseTask.__table__, CaseEscalation.__table__, case_findings]
    )
    # Without its indexes: the trigram ones are Postgres-only.
    with engine.begin() as conn:
        conn.execute(CreateTable(Finding.__table__))
    session = sessionmaker(bind=engine)()

    @contextmanager
    def _this_store(_session=None):
        yield session

    monkeypatch.setattr("core.storage.unit_of_work.unit_of_work", _this_store)
    # No database behind the data service, so findings carry no derived state.
    monkeypatch.setattr("core.agents.tool_registry._data", lambda: object())
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
    assert result == {
        "tasks": [],
        "escalations": [],
        "total": 0,
        "offset": 0,
        "limit": 100,
        "has_more": False,
        "findings": [],
    }


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
    assert [escalation["reason"] for escalation in result["escalations"]] == [
        "Own case"
    ]


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


def _finding(store, case_id, n, src, dst, start):
    store.add(
        Finding(
            finding_id=f"f-{n:02d}",
            title=f"Flow {n}",
            description="beaconing",
            data_source="netflow",
            severity="high",
            timestamp=start + timedelta(minutes=n),
            entity_context={"src_ips": [src], "dest_ips": [dst]},
        )
    )
    store.execute(
        case_findings.insert().values(case_id=case_id, finding_id=f"f-{n:02d}")
    )


# A case larger than one page: newest first, the total, and offset reads the
# rest -- including a finding on hosts an IP search for the trigger pair misses.
@pytest.mark.asyncio
async def test_pages_the_cases_findings_newest_first(store):
    start = datetime(2026, 10, 26, 12, 0)
    for n in range(1, 6):
        _finding(store, "c-1", n, "10.0.0.1", "10.0.0.2", start)
    _finding(store, "c-1", 6, "10.9.9.9", "172.16.0.5", start - timedelta(hours=1))
    _finding(store, "c-2", 7, "10.0.0.1", "10.0.0.2", start)
    store.add(CaseTask(case_id="c-1", title="Pull logs", priority="high"))
    store.commit()

    first, _ = await execute_backend_tool(
        "case_records", {"case_id": "c-1", "limit": 4}
    )
    assert first["total"] == 6
    assert first["has_more"] is True
    assert [f["finding_id"] for f in first["findings"]] == [
        "f-05",
        "f-04",
        "f-03",
        "f-02",
    ]
    assert first["findings"][0]["title"] == "Flow 5"
    assert [task["title"] for task in first["tasks"]] == ["Pull logs"]

    rest, _ = await execute_backend_tool(
        "case_records", {"case_id": "c-1", "limit": 4, "offset": 4}
    )
    assert rest["has_more"] is False
    assert [f["finding_id"] for f in rest["findings"]] == ["f-01", "f-06"]
    assert rest["findings"][1]["entities"]["ips"] == ["10.9.9.9", "172.16.0.5"]
    assert [task["title"] for task in rest["tasks"]] == ["Pull logs"]
