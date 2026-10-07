"""The RCA-on-handoff gate: a proven threat hunt tees up one root-cause run with
the finding as context; an RCA's own handoff spawns nothing."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from core.workflows import run_bridge_router as rbr
from core.workflows.run_bridge_router import (
    TerminalHandoff,
    _source_is_hunt,
    _start_root_cause,
)
from core.workflows.workflows_service import WorkflowsService

HANDOFF = TerminalHandoff(
    case_id="case-abc",
    title="IR case case-abc — threat-hunt",
    markdown="The internal host FYODOR-L (192.168.70.186) is beaconing to 45.77.53.176:443.",
)


def _run_service(existing=None):
    """``existing`` is the run row a prior tee-up left, or None when there is none."""
    svc = Mock()
    svc.find_run_by_trigger.return_value = existing
    return svc


def _with_ledger_kind(run_kind, raises=None):
    """Patch the lazily-imported ledger read so it reports run_kind for any run."""
    reader = Mock(side_effect=raises) if raises else Mock(return_value=run_kind)
    return patch("core.workflows.run_resume.run_kind_of", reader)


class TestTheSourceGuard:
    """Answered off the ledger's first event, which is the value the worker itself
    decided to push the handoff early from. The run's workflow row is a different
    question: a hunt started from file paths is filed under 'hunt' rather than
    'threat-hunt', so resolving a definition from it finds nothing."""

    def test_a_threat_hunt_source_is_a_hunt(self):
        with _with_ledger_kind("hunt"):
            assert _source_is_hunt("run-1") is True

    def test_a_root_cause_source_is_not_a_hunt(self):
        # An RCA's own handoff must not spawn another RCA.
        with _with_ledger_kind("root_cause"):
            assert _source_is_hunt("run-2") is False

    def test_a_run_with_no_ledger_is_not_a_hunt(self):
        with _with_ledger_kind(None):
            assert _source_is_hunt("run-3") is False

    def test_an_unreadable_ledger_is_not_a_hunt(self):
        with _with_ledger_kind(None, raises=RuntimeError("db gone")):
            assert _source_is_hunt("run-4") is False


class TestTeeingUpTheRootCause:
    def test_a_hunt_handoff_enqueues_exactly_one_rca_with_a_derived_hypothesis(self):
        # _process_handoff owns the dedup guard, so _start_root_cause is the sole
        # tee-up and takes no run_service of its own.
        enqueue = AsyncMock(return_value={"success": True, "run_id": "r-1"})
        with patch.object(rbr, "_enqueue_root_cause", enqueue):
            _start_root_cause("run-1", HANDOFF, "case-opened")

        enqueue.assert_awaited_once()
        params, triggered_by = enqueue.await_args.args
        assert triggered_by == "handoff:run-1:case-abc"
        # Only what the run reads. agent_id and source_run_id used to ride along
        # here unconsumed — the roster is rootcause.yaml's, and triggered_by above
        # already carries which run this traces back from.
        assert set(params) == {"context", "case_id"}
        assert "hypothesis" not in params
        assert "FYODOR-L" in params["context"]
        assert "approve_hypotheses" not in params
        assert params["case_id"] == "case-opened"


class TestProcessHandoff:
    """A handoff is filed the moment it lands and again on the terminal that carries
    it, so processing one must open its case and tee its RCA exactly once."""

    def test_a_hunt_handoff_opens_a_case_and_tees_the_rca(self):
        svc = _run_service()
        with patch.object(
            rbr, "_open_case", return_value="case-opened"
        ) as open_case, patch.object(rbr, "_start_root_cause") as start_rca:
            rbr._process_handoff("run-1", HANDOFF, "", True, svc)
        open_case.assert_called_once()
        start_rca.assert_called_once()
        # The RCA is teed onto the case that was just opened, not the agent-side id.
        assert start_rca.call_args.args[2] == "case-opened"

    def test_a_second_arrival_tees_no_second_rca(self):
        # The /handoff push already teed the RCA; the terminal re-carries the same
        # handoff and must tee nothing further. _open_case is still called -- it
        # keys on the handoff and finds the case the first arrival opened, which is
        # its own gate rather than this one's.
        svc = _run_service(existing={"run_id": "r-1", "status": "running"})
        with patch.object(rbr, "_open_case", return_value="case-opened"), patch.object(
            rbr, "_start_root_cause"
        ) as start_rca:
            rbr._process_handoff("run-1", HANDOFF, "", True, svc)
        start_rca.assert_not_called()

    def test_a_non_hunt_handoff_opens_a_case_but_tees_nothing(self):
        # A root-cause run's own handoff opens its case and spawns no further RCA.
        svc = _run_service()
        with patch.object(
            rbr, "_open_case", return_value="case-opened"
        ) as open_case, patch.object(rbr, "_start_root_cause") as start_rca:
            rbr._process_handoff("run-2", HANDOFF, "", False, svc)
        open_case.assert_called_once()
        start_rca.assert_not_called()


def test_the_finding_rides_in_context_and_there_is_no_hypothesis():
    noisy = TerminalHandoff(
        case_id="case-x",
        title="Exploitation of CVE-2024-21412 confirmed",
        markdown="payload SHA-256: 9f2c in region US-EAST-1, egress to 45.77.53.176",
    )
    enqueue = AsyncMock(return_value={"success": True, "run_id": "r-1"})
    with patch.object(rbr, "_enqueue_root_cause", enqueue):
        _start_root_cause("run-1", noisy, "case-opened")
    params, _triggered = enqueue.await_args.args
    assert "hypothesis" not in params
    assert "SHA-256" in params["context"]
    assert "45.77.53.176" in params["context"]


@pytest.mark.asyncio
async def test_a_root_cause_run_without_a_target_is_refused():
    result = await WorkflowsService().execute_workflow("root-cause-analysis", {})
    assert result["success"] is False
    assert "context, finding_id, or case_id" in result["error"]


@pytest.mark.asyncio
async def test_a_root_cause_run_with_context_is_queued_without_a_hypothesis():
    captured = {}

    async def _enqueue(job, job_id=None):
        captured["job"] = job
        return "job-1"

    with patch(
        "core.workflows.workflow_run_service.WorkflowRunService.begin_run",
        return_value="run-1",
    ), patch("core.agents.queue.enqueue_run", new=AsyncMock(side_effect=_enqueue)):
        result = await WorkflowsService().execute_workflow(
            "root-cause-analysis", {"context": "FYODOR-L is beaconing"}
        )

    assert result["success"] is True
    assert captured["job"]["run_kind"] == "root_cause"
    assert captured["job"]["request"].get("hypotheses") in (None, [])


def test_root_cause_resolves_off_the_hunt_grant():
    import yaml

    from core.workflows.playbook_resolver import resolve_root_cause
    from core.workflows.playbooks_router import _resolver_for

    workflows = WorkflowsService()
    assert (
        _resolver_for(workflows, "root-cause-analysis").__name__ == "resolve_root_cause"
    )
    assert _resolver_for(workflows, "threat-hunt").__name__ == "resolve_hunt"

    playbook, config_text = resolve_root_cause(
        "root-cause-analysis", workflows=workflows
    )
    document = yaml.safe_load(playbook)
    config = yaml.safe_load(config_text)
    assert "hypotheses" not in document
    assert document.get("phases") in (None, [])
    assert "checkpoints" not in config
    ids = [tool["id"] for tool in config["tools"]]
    assert ids[:2] == ["record", "finish"] or {"record", "finish"} <= set(ids)
    assert "search_findings" not in ids
    assert "lookup_indicators" not in ids
    assert "recall_entity" not in ids
    assert config["budgets"]["max_cost_usd"] == 15.0
    assert config["budgets"]["max_wall_ms"] == 5_400_000
    assert config["budgets"]["max_calls"] > 12
    assert config["runtime"]["max_turns"] > 8
