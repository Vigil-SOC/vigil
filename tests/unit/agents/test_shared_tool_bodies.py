# One body per name that both doors publish. The MCP tool is a wrapper: it
# accepts the manifest's arguments and calls the function the backend calls.

from __future__ import annotations

import inspect
import json
from contextlib import contextmanager

import pytest

from core.agents import tool_registry
from core.agents.tool_registry import MANIFEST, execute_backend_tool
from core.cases.case_workflow_service import CaseWorkflowService
from core.cases.closure import ClosedByKind, ClosureCategory
from core.integrations.mcp import in_process
from core.integrations.mcp.surface import acting_as
from tools.mcp import vigil

pytestmark = pytest.mark.unit


def _shared():
    mcp = {tool["name"]: tool for tool in in_process.list_tools()}
    names = sorted(set(mcp) & set(MANIFEST))
    return mcp, names


def _sample(schema: dict) -> dict:
    args = {}
    properties = schema.get("properties") or {}
    for key in schema.get("required") or []:
        kind = (properties.get(key) or {}).get("type")
        if kind == "integer":
            args[key] = 1
        elif kind == "number":
            args[key] = 0.5
        elif kind == "array":
            args[key] = []
        else:
            args[key] = "x"
    return args


@pytest.mark.asyncio
async def test_both_doors_call_that_function(monkeypatch):
    mcp, names = _shared()

    assert "get_technique_rollup" in names
    assert "technique_rollup" not in mcp
    assert len(names) >= 13

    for name in names:
        body = tool_registry.__dict__[name]
        seen: list[str] = []

        def spy(*_args, _name=name, **_kwargs):
            seen.append(_name)
            return {"shared": _name}

        async def aspy(*_args, _name=name, **_kwargs):
            seen.append(_name)
            return {"shared": _name}

        monkeypatch.setattr(
            tool_registry,
            name,
            aspy if inspect.iscoroutinefunction(body) else spy,
        )
        schema = MANIFEST[name].get("input_schema") or {}
        published = (mcp[name].get("input_schema") or {}).get("properties") or {}
        declared = schema.get("properties") or {}
        assert set(published) == set(declared), name

        args = _sample(schema)
        result, handled = await execute_backend_tool(name, args)
        assert handled is True
        assert result == {"shared": name}

        mcp_fn = getattr(vigil, name)
        text = (
            await mcp_fn(**args)
            if inspect.iscoroutinefunction(mcp_fn)
            else mcp_fn(**args)
        )
        assert json.loads(text) == {"shared": name}
        assert seen == [name, name]


@pytest.mark.asyncio
async def test_closing_a_case_through_the_backend_records_a_closure(monkeypatch):
    class _Cases:
        def get_case(self, case_id):
            return {"case_id": case_id, "status": "investigating", "notes": []}

        def update_case(self, case_id, **updates):
            assert updates["status"] == "closed"
            return True

    recorded = []

    def _close(self, session, case_id, **kwargs):
        recorded.append((case_id, kwargs))

    @contextmanager
    def _session():
        yield object()

    monkeypatch.setattr(tool_registry, "_data", lambda: _Cases())
    monkeypatch.setattr(CaseWorkflowService, "close_case", _close)
    monkeypatch.setattr("core.cases.agent_closure.service_session", _session)

    result, handled = await execute_backend_tool(
        "update_case", {"case_id": "case-1", "status": "closed"}
    )

    assert handled is True
    assert result == {"success": True, "case_id": "case-1"}
    assert recorded[0][0] == "case-1"
    assert recorded[0][1]["closure_category"] == ClosureCategory.UNSPECIFIED
    assert recorded[0][1]["closed_by_kind"] == ClosedByKind.AGENT
    assert recorded[0][1]["closed_by"] == "agent"


@pytest.mark.asyncio
async def test_a_note_is_stored_where_the_timeline_reads_it(monkeypatch):
    captured = {}

    class _Cases:
        def get_case(self, case_id):
            return {"case_id": case_id, "status": "open", "notes": []}

        def update_case(self, case_id, **updates):
            captured.update(updates)
            return True

    monkeypatch.setattr(tool_registry, "_data", lambda: _Cases())

    result, handled = await execute_backend_tool(
        "update_case", {"case_id": "case-1", "add_note": "beacon confirmed"}
    )

    assert handled is True
    assert result["success"] is True
    assert captured["notes"][0]["content"] == "beacon confirmed"
    assert "note" not in captured["notes"][0]


@pytest.mark.asyncio
async def test_the_model_cannot_name_the_approver():
    with pytest.raises(TypeError):
        await execute_backend_tool(
            "approve_action", {"action_id": "act-1", "approved_by": "mallory"}
        )


def _approval_spy(monkeypatch):
    called = []

    class _Approvals:
        def approve_action(self, action_id, approved_by):
            called.append((action_id, approved_by))

    monkeypatch.setattr(tool_registry, "_approvals", lambda: _Approvals())
    return called


@pytest.mark.asyncio
async def test_approve_action_refuses_when_no_principal_is_bound(monkeypatch):
    called = _approval_spy(monkeypatch)

    result, handled = await execute_backend_tool(
        "approve_action", {"action_id": "act-1"}
    )

    assert handled is True
    assert result == {"error": "Action cannot be approved: no principal is bound"}
    assert called == []


@pytest.mark.asyncio
async def test_approve_action_names_the_bound_principal(monkeypatch):
    called = _approval_spy(monkeypatch)

    with acting_as("analyst"):
        _result, handled = await execute_backend_tool(
            "approve_action", {"action_id": "act-1"}
        )

    assert handled is True
    assert called == [("act-1", "analyst")]


@pytest.mark.asyncio
async def test_list_findings_filters_cluster_in_sql(monkeypatch):
    class _Findings:
        def __init__(self):
            self.count_kwargs = None
            self.get_kwargs = None

        def count_findings(self, **kwargs):
            self.count_kwargs = kwargs
            return 4

        def get_findings(self, **kwargs):
            self.get_kwargs = kwargs
            return [
                {
                    "finding_id": "f-1",
                    "severity": "high",
                    "anomaly_score": 0.9,
                    "description": "beacon",
                    "data_source": "sysmon",
                    "cluster_id": "clu-1",
                    "timestamp": "t",
                    "status": "new",
                }
            ]

    fake = _Findings()
    monkeypatch.setattr(tool_registry, "_data", lambda: fake)

    result, handled = await execute_backend_tool(
        "list_findings", {"cluster_id": "clu-1", "min_anomaly_score": 0.5}
    )

    assert handled is True
    assert result["total"] == 4
    assert result["total"] != len(result["findings"])
    assert fake.count_kwargs["cluster_id"] == "clu-1"
    assert fake.count_kwargs["min_anomaly_score"] == 0.5
    assert fake.get_kwargs["cluster_id"] == "clu-1"
    assert fake.get_kwargs["limit"] == 20


@pytest.mark.asyncio
async def test_technique_rollup_reads_the_sql_rollup(monkeypatch):
    def rollup(min_confidence=0.0, time_range="all", service=None):
        assert min_confidence == 0.4
        assert time_range == "7d"
        assert service is not None
        return {"total_techniques": 0, "techniques": []}

    monkeypatch.setattr("core.threat_intel.occurrence_rollup.occurrence_rollup", rollup)
    monkeypatch.setattr(tool_registry, "_data", lambda: object())

    result, handled = await execute_backend_tool(
        "get_technique_rollup", {"min_confidence": 0.4, "time_range": "7d"}
    )

    assert handled is True
    assert result == {"total_techniques": 0, "techniques": []}
