"""The catalog row says which turned-off agents a workflow needs."""

from unittest.mock import MagicMock

from core.workflows import catalog
from core.workflows.workflows_service import WorkflowDefinition


def _definition(workflow_id, run_kind, agents):
    meta = {"run_kind": run_kind, "phases": [{"agent": a} for a in agents]}
    return WorkflowDefinition(workflow_id, None, meta, "")


def test_blocked_by_lists_off_phase_agents_where_run_would_refuse(monkeypatch):
    defs = {
        "threat-hunt": _definition("threat-hunt", "hunt", ["threat_hunter", "intel"]),
        "shadow": _definition("shadow", "adjudicate", ["threat_hunter"]),
        "custom": _definition("custom", "compose", ["triage", "threat_hunter"]),
        "lead": _definition("lead", "investigate", []),
        "trace": _definition("trace", "root_cause", ["threat_hunter"]),
        "clear": _definition("clear", "compose", ["triage"]),
    }
    service = MagicMock()
    service.list_workflows.return_value = [{"id": i} for i in defs]
    service.get_workflow.side_effect = defs.get
    monkeypatch.setattr(catalog, "_week_run_stats", lambda now: {})
    monkeypatch.setattr(catalog, "disabled_workflow_ids", lambda: set())
    monkeypatch.setattr(catalog, "disabled_agent_ids", lambda: {"threat_hunter"})

    rows = {r["id"]: r for r in catalog.listing(service)["workflows"]}

    assert rows["custom"]["blocked_by"] == ["threat_hunter"]
    # a hunt-like run is its plugin's agent and a trace has no phases, so neither
    # is refused; the rest name nothing that is off
    for workflow_id in ("threat-hunt", "shadow", "lead", "trace", "clear"):
        assert rows[workflow_id]["blocked_by"] == []


def test_blocked_by_is_empty_when_nothing_is_off(monkeypatch):
    service = MagicMock()
    service.list_workflows.return_value = [{"id": "w"}]
    monkeypatch.setattr(catalog, "_week_run_stats", lambda now: {})
    monkeypatch.setattr(catalog, "disabled_workflow_ids", lambda: set())
    monkeypatch.setattr(catalog, "disabled_agent_ids", lambda: set())

    assert catalog.listing(service)["workflows"][0]["blocked_by"] == []
    service.get_workflow.assert_not_called()
