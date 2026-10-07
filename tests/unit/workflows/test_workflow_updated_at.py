"""updated_at is a custom-workflow field. File workflows omit the key."""

from datetime import datetime

from core.workflows.workflows_service import (
    WorkflowDefinition,
    _custom_workflow_to_definition,
)


def test_file_workflow_omits_updated_at():
    wf = WorkflowDefinition("cloud-incident", None, {"name": "Cloud"}, "body")
    assert "updated_at" not in wf.to_dict()


def test_custom_workflow_carries_updated_at():
    wf = _custom_workflow_to_definition(
        {
            "workflow_id": "wf-1",
            "name": "Ransom",
            "description": "Contain it",
            "phases": [{"order": 1, "name": "Look", "agent_id": "triage"}],
            "updated_at": datetime(2026, 6, 15, 8, 30, 0),
        }
    )
    assert wf.to_dict()["updated_at"] == "2026-06-15T08:30:00"
    assert wf.to_dict()["agents"] == ["triage"]
