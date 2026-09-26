"""Unit tests for file-based workflow discovery.

Ensures that new workflows added to the workflows/ directory are correctly
parsed and exposed by WorkflowsService.
"""

from core.workflows.workflows_service import WorkflowsService, is_hunt_like


# adjudicate is the hunt loop with a lead framed as a shadow adjudicator; the
# console and the resolver both ask is_hunt_like rather than naming kinds, so the
# definition has to answer as hunt-like or it walks phases it does not have.
def test_shadow_adjudication_is_a_hunt_like_definition():
    assert is_hunt_like("adjudicate")

    wf = WorkflowsService().get_workflow("shadow-adjudication")
    assert wf is not None
    assert wf.run_kind == "adjudicate"
    # The caller states the hypothesis, as threat-hunt does.
    assert wf.metadata.get("hypotheses") == []
    assert wf.metadata.get("attack_techniques")
    assert wf.metadata.get("data_domains")

    listed = next(
        w
        for w in WorkflowsService().list_workflows()
        if w["id"] == "shadow-adjudication"
    )
    assert listed["run_kind"] == "adjudicate"
    assert listed["hunt_like"] is True


def test_cloud_incident_workflow_is_discovered():
    """The cloud-incident workflow should load from disk with correct metadata."""
    service = WorkflowsService()

    wf = service.get_workflow("cloud-incident")
    assert wf is not None, "cloud-incident workflow should be discovered"
    assert wf.name == "cloud-incident"
    assert wf.id == "cloud-incident"
    assert wf.run_kind == "investigate"
    assert wf.phases == []
    assert "aws" in wf.description.lower() or "azure" in wf.description.lower()

    # The body states the job the objectives already state.
    body = wf.body.lower()
    assert "blast radius" in body
    assert "cross-account" in body or "cross account" in body


def test_cloud_incident_in_list_workflows():
    """list_workflows should include the cloud-incident definition."""
    service = WorkflowsService()
    workflows = service.list_workflows()
    ids = [w["id"] for w in workflows]
    assert "cloud-incident" in ids

    cloud_wf = next(w for w in workflows if w["id"] == "cloud-incident")
    assert cloud_wf["name"] == "cloud-incident"
    assert cloud_wf["run_kind"] == "investigate"
    assert cloud_wf["agents"] == []


def test_cloud_incident_workflow_dict():
    """get_workflow_dict should return serializable metadata and body."""
    service = WorkflowsService()
    d = service.get_workflow_dict("cloud-incident", include_body=True)
    assert d is not None
    assert d["id"] == "cloud-incident"
    assert "body" in d
    assert "cloud" in d["body"].lower()
