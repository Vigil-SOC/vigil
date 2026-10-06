"""A run records the version of the definition it ran (#1617)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.api.v1 import agent_runs_router
from core.workflows.workflows_service import (
    WorkflowDefinition,
    WorkflowsService,
    _custom_workflow_to_definition,
)

pytestmark = pytest.mark.unit


def _custom(version):
    return {
        "workflow_id": "wf-1",
        "name": "Ransom",
        "phases": [{"order": 1, "name": "Look", "agent_id": "triage"}],
        "version": version,
    }


class TestToDict:
    def test_file_workflow_reads_declared_version(self):
        wf = WorkflowDefinition("a", None, {"name": "A", "version": 4}, "")
        assert wf.to_dict()["version"] == 4

    @pytest.mark.parametrize("declared", [None, "2", True, 1.5])
    def test_file_workflow_without_a_usable_version_reads_1(self, declared):
        wf = WorkflowDefinition("a", None, {"name": "A", "version": declared}, "")
        assert wf.to_dict()["version"] == 1

    def test_custom_workflow_carries_its_row_version(self):
        wf = _custom_workflow_to_definition(_custom(3))
        assert wf.to_dict()["version"] == 3
        assert "version" not in wf.metadata


def _service(custom_rows=None):
    custom = MagicMock()
    custom.get.side_effect = lambda wid: (custom_rows or {}).get(wid)
    return WorkflowsService(custom_workflows=custom, workflow_runs=MagicMock())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workflow_id, rows, expected",
    [("incident-response", {}, 1), ("wf-1", {"wf-1": _custom(3)}, 3)],
)
async def test_execute_workflow_stores_the_version(workflow_id, rows, expected):
    service = _service(rows)
    service._workflow_runs.begin_run.return_value = "run-1"
    with patch("core.agents.queue.enqueue_run", new=AsyncMock(return_value="job-1")):
        result = await service.execute_workflow(workflow_id, {"finding_id": "f-1"})

    assert result["success"] is True
    assert (
        service._workflow_runs.begin_run.call_args.kwargs["workflow_version"]
        == expected
    )


class TestVersionOf:
    def test_unknown_id_is_none(self):
        assert _service().version_of("compose") is None

    def test_failed_lookup_is_none(self):
        service = _service()
        with patch.object(service, "get_workflow", side_effect=RuntimeError("db")):
            assert service.version_of("incident-response") is None


class TestAgentRunPath:
    @staticmethod
    def _begin(playbook, run_kind="compose", versions=None):
        request = agent_runs_router.StartRunRequest(
            run_kind=run_kind, playbook=playbook, config="", prompt="p"
        )
        runs = MagicMock()
        with patch(
            "core.workflows.workflow_run_service.WorkflowRunService", return_value=runs
        ), patch(
            "core.workflows.workflows_service.WorkflowsService.version_of",
            side_effect=versions or (lambda self, wid: 1),
            autospec=True,
        ):
            agent_runs_router._begin_run_row("r-1", request)
        return runs.begin_run.call_args.kwargs

    def test_named_workflow_stores_its_version(self):
        kwargs = self._begin("workflow:incident-response")
        assert kwargs["workflow_id"] == "incident-response"
        assert kwargs["workflow_version"] == 1

    def test_bare_run_kind_stores_none(self):
        assert self._begin("")["workflow_version"] is None

    def test_failed_lookup_still_begins_the_run(self):
        def boom(self, wid):
            raise RuntimeError("no db")

        assert self._begin("workflow:x", versions=boom)["workflow_version"] is None
