"""Unit tests for starting a workflow on the agent layer (#630).

``execute_workflow`` used to be a loop: it built a composite prompt (or
walked phases) and called ``ClaudeService.chat`` itself. It now enqueues
a compose run and hands back the id, so these lock in what reaches the
queue rather than what reaches a model.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.workflows.workflows_service import WorkflowDefinition, WorkflowsService


def _make_workflow(workflow_id: str = "wf-test"):
    return WorkflowDefinition(
        workflow_id=workflow_id,
        file_path=None,
        metadata={
            "name": "Test Workflow",
            "description": "test",
            "use_case": "test",
            "trigger_examples": [],
            "phases": [
                {
                    "id": "triage",
                    "agent": "triage",
                    "name": "Triage",
                    "instructions": "Look at it.",
                }
            ],
        },
        body="An overview.",
        source="file",
    )


@pytest.mark.asyncio
async def test_execute_workflow_enqueues_a_compose_run(monkeypatch):
    monkeypatch.setattr(
        WorkflowsService, "get_workflow", lambda self, wid: _make_workflow(wid)
    )
    captured = {}

    async def _enqueue(job, job_id=None):
        captured["job"] = job
        return "job-1"

    with patch(
        "core.workflows.workflow_run_service.WorkflowRunService.begin_run",
        return_value="run-1",
    ), patch("core.agents.queue.enqueue_run", new=AsyncMock(side_effect=_enqueue)):
        result = await WorkflowsService().execute_workflow(
            "incident-response", {"finding_id": "f-1"}, triggered_by="tester"
        )

    assert result["success"] is True
    assert result["status"] == "queued"
    assert result["run_id"] == "run-1"

    job = captured["job"]
    assert job["run_kind"] == "compose"
    assert job["reason"] == "start"
    assert job["run_id"] == "run-1"


@pytest.mark.asyncio
async def test_the_job_names_the_workflow_rather_than_a_path(monkeypatch):
    """A reference, so an edited definition reaches the next run."""
    monkeypatch.setattr(
        WorkflowsService, "get_workflow", lambda self, wid: _make_workflow(wid)
    )
    captured = {}

    async def _enqueue(job, job_id=None):
        captured["job"] = job
        return "job-1"

    with patch(
        "core.workflows.workflow_run_service.WorkflowRunService.begin_run",
        return_value="run-1",
    ), patch("core.agents.queue.enqueue_run", new=AsyncMock(side_effect=_enqueue)):
        await WorkflowsService().execute_workflow("cloud-incident", {})

    request = captured["job"]["request"]
    assert request["playbook"] == "workflow:cloud-incident"
    assert request["config"] == ""
    assert request["arch"] == ""


@pytest.mark.asyncio
async def test_the_prompt_carries_what_the_run_is_about(monkeypatch):
    monkeypatch.setattr(
        WorkflowsService, "get_workflow", lambda self, wid: _make_workflow(wid)
    )
    captured = {}

    async def _enqueue(job, job_id=None):
        captured["job"] = job
        return "job-1"

    with patch(
        "core.workflows.workflow_run_service.WorkflowRunService.begin_run",
        return_value="run-1",
    ), patch("core.agents.queue.enqueue_run", new=AsyncMock(side_effect=_enqueue)):
        await WorkflowsService().execute_workflow(
            "incident-response", {"context": "a suspicious login"}
        )

    assert "a suspicious login" in captured["job"]["request"]["prompt"]


@pytest.mark.asyncio
async def test_an_unknown_workflow_is_refused_before_the_queue(monkeypatch):
    monkeypatch.setattr(WorkflowsService, "get_workflow", lambda self, wid: None)

    with patch("core.agents.queue.enqueue_run", new=AsyncMock()) as enqueue:
        result = await WorkflowsService().execute_workflow("nope", {})

    assert result["success"] is False
    assert "not found" in result["error"].lower()
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_a_queue_outage_fails_the_run_rather_than_leaving_it_running(monkeypatch):
    monkeypatch.setattr(
        WorkflowsService, "get_workflow", lambda self, wid: _make_workflow(wid)
    )

    with patch(
        "core.workflows.workflow_run_service.WorkflowRunService.begin_run",
        return_value="run-1",
    ), patch(
        "core.workflows.workflow_run_service.WorkflowRunService.finalize_run"
    ) as finalize, patch(
        "core.agents.queue.enqueue_run",
        new=AsyncMock(side_effect=RuntimeError("redis is down")),
    ):
        result = await WorkflowsService().execute_workflow("incident-response", {})

    assert result["success"] is False
    finalize.assert_called_once()
    assert finalize.call_args.kwargs["status"] == "failed"


# The run modal collects a hypothesis and the board came only from the definition,
# so what an operator typed was stringified into the prompt and tested by nobody.
@pytest.mark.asyncio
async def test_the_job_carries_the_hypothesis_the_operator_asked_about(monkeypatch):
    monkeypatch.setattr(
        WorkflowsService, "get_workflow", lambda self, wid: _make_workflow(wid)
    )
    captured = {}

    async def _enqueue(job, job_id=None):
        captured["job"] = job
        return "job-1"

    with patch(
        "core.workflows.workflow_run_service.WorkflowRunService.begin_run",
        return_value="run-1",
    ), patch("core.agents.queue.enqueue_run", new=AsyncMock(side_effect=_enqueue)):
        await WorkflowsService().execute_workflow(
            "threat-hunt", {"hypothesis": "lateral movement over SMB"}
        )

    assert captured["job"]["request"]["hypotheses"] == ["lateral movement over SMB"]


@pytest.mark.asyncio
async def test_one_hypothesis_per_line_so_a_run_can_put_up_several(monkeypatch):
    monkeypatch.setattr(
        WorkflowsService, "get_workflow", lambda self, wid: _make_workflow(wid)
    )
    captured = {}

    async def _enqueue(job, job_id=None):
        captured["job"] = job
        return "job-1"

    with patch(
        "core.workflows.workflow_run_service.WorkflowRunService.begin_run",
        return_value="run-1",
    ), patch("core.agents.queue.enqueue_run", new=AsyncMock(side_effect=_enqueue)):
        await WorkflowsService().execute_workflow(
            "threat-hunt", {"hypothesis": "one\n\n  two  \n"}
        )

    assert captured["job"]["request"]["hypotheses"] == ["one", "two"]


@pytest.mark.asyncio
async def test_a_run_that_asked_nothing_puts_up_nothing(monkeypatch):
    monkeypatch.setattr(
        WorkflowsService, "get_workflow", lambda self, wid: _make_workflow(wid)
    )
    captured = {}

    async def _enqueue(job, job_id=None):
        captured["job"] = job
        return "job-1"

    with patch(
        "core.workflows.workflow_run_service.WorkflowRunService.begin_run",
        return_value="run-1",
    ), patch("core.agents.queue.enqueue_run", new=AsyncMock(side_effect=_enqueue)):
        await WorkflowsService().execute_workflow("threat-hunt", {"finding_id": "f-1"})

    assert captured["job"]["request"]["hypotheses"] == []


# What an operator is willing to spend on one question rides the job. The count
# was a constant in the resolver before, so the only way to change it was an edit.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asked,expected",
    [
        ({"iterations": 3}, 3),
        ({"iterations": "5"}, 5),
        ({}, None),
        ({"iterations": "ten"}, None),
    ],
)
async def test_the_turn_count_rides_the_job(monkeypatch, asked, expected):
    monkeypatch.setattr(
        WorkflowsService, "get_workflow", lambda self, wid: _make_workflow(wid)
    )
    captured = {}

    async def _enqueue(job, job_id=None):
        captured["job"] = job
        return "job-1"

    with patch(
        "core.workflows.workflow_run_service.WorkflowRunService.begin_run",
        return_value="run-1",
    ), patch("core.agents.queue.enqueue_run", new=AsyncMock(side_effect=_enqueue)):
        await WorkflowsService().execute_workflow(
            "threat-hunt", {"finding_id": "f-1", **asked}
        )

    assert captured["job"]["request"].get("iterations") == expected
    assert ("iterations" in captured["job"]["request"]) is (expected is not None)


# The harness has always taken an overrides block naming budgets; this side dropped
# it, so the only way to change what a hunt may spend was to edit the resolver.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asked,expected",
    [
        ({"max_cost_usd": 25}, {"budgets": {"max_cost_usd": 25.0}}),
        ({"max_cost_usd": "7.5"}, {"budgets": {"max_cost_usd": 7.5}}),
        ({}, None),
        ({"max_cost_usd": "lots"}, None),
        ({"max_cost_usd": 0}, None),
    ],
)
async def test_the_cost_ceiling_rides_the_job(monkeypatch, asked, expected):
    monkeypatch.setattr(
        WorkflowsService, "get_workflow", lambda self, wid: _make_workflow(wid)
    )
    captured = {}

    async def _enqueue(job, job_id=None):
        captured["job"] = job
        return "job-1"

    with patch(
        "core.workflows.workflow_run_service.WorkflowRunService.begin_run",
        return_value="run-1",
    ), patch("core.agents.queue.enqueue_run", new=AsyncMock(side_effect=_enqueue)):
        await WorkflowsService().execute_workflow(
            "threat-hunt", {"finding_id": "f-1", **asked}
        )

    assert captured["job"]["request"].get("overrides") == expected
    # withOverrides refuses anything but budgets or runtime, so a stray key here
    # would fail the run at spec assembly rather than be ignored.
    if expected is not None:
        assert set(captured["job"]["request"]["overrides"]) == {"budgets"}


# A hunt argues the null against a claim, so the shape gate is deliberate. It is a
# heuristic though, and "ed " recognised only a regular past tense: a real claim that
# spelled its verb irregularly was refused alongside the subject labels it is for.
@pytest.mark.parametrize(
    "statement",
    [
        "data left the estate over DNS",
        "HOST-42 sent 4GB to 45.77.53.176",
        "the attacker stole the signing key",
        "an operator took the backup offsite",
        "credentials taken from HOST-42 were reused elsewhere",
    ],
)
def test_a_claim_is_a_claim_however_its_verb_is_spelled(statement):
    from core.workflows.workflows_service import _not_a_claim

    assert _not_a_claim(statement) is False


# Still refused, which is the point: widening the verbs must not admit a subject.
@pytest.mark.parametrize(
    "statement",
    [
        "credential access",
        "credential access and escalation",
        "idk",
        "lateral movement via RDP",
    ],
)
def test_a_subject_is_still_not_a_claim(statement):
    from core.workflows.workflows_service import _not_a_claim

    assert _not_a_claim(statement) is True


# The run is where the belief has to exist, because the run is where a person is
# there to be told. The definition ships none on purpose.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "asked,refused",
    [
        ({"context": "beaconing"}, True),
        ({"finding_id": "f-1"}, True),
        ({"context": "beaconing", "hypothesis": "  \n "}, True),
        (
            {
                "context": "beaconing",
                "hypothesis": "a host is beaconing to external C2",
            },
            False,
        ),
        ({"hypothesis": "a host is beaconing to external C2"}, False),
    ],
)
async def test_a_hunt_needs_a_belief_from_someone(monkeypatch, asked, refused):
    hunt = WorkflowDefinition(
        workflow_id="threat-hunt",
        file_path=None,
        metadata={"name": "Threat Hunt", "run_kind": "hunt", "hypotheses": []},
        body="",
    )
    monkeypatch.setattr(WorkflowsService, "get_workflow", lambda self, wid: hunt)

    with patch(
        "core.workflows.workflow_run_service.WorkflowRunService.begin_run",
        return_value="run-1",
    ), patch("core.agents.queue.enqueue_run", new=AsyncMock(return_value="job-1")):
        result = await WorkflowsService().execute_workflow("threat-hunt", asked)

    if refused:
        assert result["success"] is False
        assert "needs a hypothesis" in result["error"]
    else:
        assert result.get("success") is True
