"""Unit tests for file-based workflow discovery.

Ensures that new workflows added to the workflows/ directory are correctly
parsed and exposed by WorkflowsService.
"""

import pytest

from services.workflows_service import WorkflowsService


def test_cloud_incident_workflow_is_discovered():
    """The cloud-incident workflow should load from disk with correct metadata."""
    service = WorkflowsService()

    wf = service.get_workflow("cloud-incident")
    assert wf is not None, "cloud-incident workflow should be discovered"
    assert wf.name == "cloud-incident"
    assert wf.id == "cloud-incident"
    assert "aws" in wf.description.lower() or "azure" in wf.description.lower()

    expected_agents = {"investigator", "correlator", "mitre_analyst", "responder", "reporter"}
    assert expected_agents.issubset(set(wf.agents)), f"Expected agents {expected_agents}, got {wf.agents}"

    # Basic tools should be declared in frontmatter
    assert "get_finding" in wf.tools_used
    assert "create_approval_action" in wf.tools_used

    # Body should contain cloud-specific guidance
    body_lower = wf.body.lower()
    assert "control-plane" in body_lower or "control plane" in body_lower
    assert "cross-account" in body_lower or "cross account" in body_lower
    assert "blast radius" in body_lower


def test_cloud_incident_in_list_workflows():
    """list_workflows should include the cloud-incident definition."""
    service = WorkflowsService()
    workflows = service.list_workflows()
    ids = [w["id"] for w in workflows]
    assert "cloud-incident" in ids

    cloud_wf = next(w for w in workflows if w["id"] == "cloud-incident")
    assert cloud_wf["name"] == "cloud-incident"
    assert "investigator" in cloud_wf["agents"]


def test_cloud_incident_workflow_dict():
    """get_workflow_dict should return serializable metadata and body."""
    service = WorkflowsService()
    d = service.get_workflow_dict("cloud-incident", include_body=True)
    assert d is not None
    assert d["id"] == "cloud-incident"
    assert "body" in d
    assert "cloud" in d["body"].lower()
