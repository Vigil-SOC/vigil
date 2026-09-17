"""The orchestrator drives an investigation by enqueuing a run and reading it back.

#629 moved execution to the agent worker. What is left here is the two ends: the
enqueue, and the reconcile that keeps the operator-visible row honest against the
projection the agent layer serves.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO))

from core.agents.projections import run_id_for
from core.workflows.workflows_service import WorkflowDefinition, WorkflowsService
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator, shadow_run_id_for
from services.daemon.workdir import WorkdirManager

pytestmark = pytest.mark.unit

INV = "inv-20260812-abc12345"
DEFINITIONS = REPO / "core" / "workflows" / "definitions"

# The bundled definitions, read rather than listed: what each declares is the
# fact under test, and a copy of it here would be one edit from disagreeing.
WORKFLOWS = WorkflowsService(workflows_dir=DEFINITIONS)
UNDECLARED = sorted(
    wf.id for wf in WORKFLOWS._cache.values() if not wf.metadata.get("run_kind")
)


def _orchestrator() -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch.workdir = MagicMock()
    orch.workdir.read_file.return_value = "three failed logons on FYODOR-L"
    orch._workflows = WORKFLOWS
    orch._update_investigation_status = MagicMock()
    orch._record_progress = MagicMock()
    return orch


def _record(**overrides):
    return {"investigation_id": INV, "workflow_id": "incident-response", **overrides}


# A real workdir, because what is being tested is that the keys survive the gap
# between an investigation being created and being enqueued -- which is a file on
# disk and a read of it, not a value held in the process.
def _opening(tmp_path: Path) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig(dry_run=True)
    orch.workdir = WorkdirManager(str(tmp_path))
    orch._workflows = WORKFLOWS
    orch.shared_intel = MagicMock()
    orch.stats = {"investigations_created": 0}
    orch._save_investigation = MagicMock()
    orch._update_investigation_status = MagicMock()
    orch._check_cross_correlations = AsyncMock()
    return orch


class TestEnqueue:
    @pytest.mark.asyncio
    async def test_addresses_the_run_deterministically(self):
        orch = _orchestrator()
        with patch(
            "services.daemon.orchestrator.enqueue_run", new=AsyncMock()
        ) as enqueued:
            await orch._enqueue_investigation(_record())

        job = enqueued.await_args[0][0]
        # Derived from the investigation id, so a resume addresses the same run
        # rather than starting a second one beside it.
        assert job["run_id"] == run_id_for(INV)
        assert job["run_kind"] == "investigate"

    @pytest.mark.asyncio
    async def test_names_the_workflow_as_a_reference_and_no_config_beside_it(self):
        orch = _orchestrator()
        with patch(
            "services.daemon.orchestrator.enqueue_run", new=AsyncMock()
        ) as enqueued:
            await orch._enqueue_investigation(_record())

        request = enqueued.await_args[0][0]["request"]
        assert request["playbook"] == "workflow:incident-response"
        # A reference resolves both layers, so a config path beside it would be a
        # second source for a layer that already has one.
        assert request["config"] == ""

    @pytest.mark.asyncio
    async def test_carries_the_orchestrator_ceilings_as_the_run_budget(self):
        orch = _orchestrator()
        orch.config.max_cost_per_investigation = 3.5
        orch.config.max_runtime_per_investigation = 900
        orch.config.max_iterations_per_agent = 12

        with patch(
            "services.daemon.orchestrator.enqueue_run", new=AsyncMock()
        ) as enqueued:
            await orch._enqueue_investigation(_record())

        # ORCHESTRATOR_MAX_COST and ORCHESTRATOR_MAX_RUNTIME keep their meaning:
        # the ceilings the budget seam refuses the next call at.
        assert enqueued.await_args[0][0]["request"]["overrides"]["budgets"] == {
            "max_calls": 12,
            "max_cost_usd": 3.5,
            "max_wall_ms": 900_000,
        }

    # The one place the vocabulary is observable. A key spelled the shared-IOC way
    # -- `hostname:` where memory writes `host:` -- returns no rows and every other
    # test still passes, so the read reads as an entity nobody has looked at.
    @pytest.mark.asyncio
    async def test_carries_the_trigger_entities_as_recall_keys(self, tmp_path):
        orch = _opening(tmp_path)
        findings = [
            {
                "finding_id": "f-1",
                "entity_context": {"src_ips": ["10.0.0.5"], "hostnames": ["DC01"]},
            },
            {
                "finding_id": "f-2",
                "entity_context": {"dest_ips": ["45.77.53.176"], "usernames": ["Root"]},
            },
        ]
        await orch._create_investigation(
            "incident-response", findings, "alert", "medium"
        )
        record = orch._save_investigation.call_args[0][0]

        with patch(
            "services.daemon.orchestrator.enqueue_run", new=AsyncMock()
        ) as enqueued:
            await orch._enqueue_investigation(record)

        # Every finding, not the first: an investigation opened over several is not
        # narrowed to whichever one happened to be first in the list.
        assert sorted(enqueued.await_args[0][0]["request"]["recall_keys"]) == [
            "host:dc01",
            "ip:10.0.0.5",
            "ip:45.77.53.176",
            "user:root",
        ]

    @pytest.mark.asyncio
    async def test_a_finding_with_no_entities_carries_no_keys(self, tmp_path):
        orch = _opening(tmp_path)
        await orch._create_investigation(
            "incident-response", [{"finding_id": "f-1"}], "alert", "medium"
        )
        record = orch._save_investigation.call_args[0][0]

        with patch(
            "services.daemon.orchestrator.enqueue_run", new=AsyncMock()
        ) as enqueued:
            await orch._enqueue_investigation(record)

        # Empty rather than absent-and-guessed: "nothing was asked" has to stay
        # distinguishable from "nothing is known".
        assert enqueued.await_args[0][0]["request"]["recall_keys"] == []

    @pytest.mark.asyncio
    async def test_marks_the_investigation_failed_when_the_queue_refuses(self):
        orch = _orchestrator()
        with patch(
            "services.daemon.orchestrator.enqueue_run",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ):
            await orch._enqueue_investigation(_record())

        # Not a crash and not silently queued: an investigation nobody will run
        # must not sit reading as if somebody would.
        status, reason = orch._update_investigation_status.call_args[0][1:3]
        assert status == "failed"
        assert "redis down" in reason


# The worker picks its loop from job.run_kind alone, so the kind the definition
# declares has to be the kind that reaches the queue: the scheduler's nightly
# threat-hunt ran on the lead loop while this said "investigate" for everyone.
class TestEnqueueRunKind:
    async def _enqueued_kind(self, orch, workflow_id):
        with patch(
            "services.daemon.orchestrator.enqueue_run", new=AsyncMock()
        ) as enqueued:
            await orch._enqueue_investigation(_record(workflow_id=workflow_id))
        return enqueued.await_args[0][0]["run_kind"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "workflow_id, kind",
        [("threat-hunt", "hunt"), ("root-cause-analysis", "root_cause")],
    )
    async def test_carries_the_kind_the_definition_declares(self, workflow_id, kind):
        assert WORKFLOWS.get_workflow(workflow_id).metadata["run_kind"] == kind
        assert await self._enqueued_kind(_orchestrator(), workflow_id) == kind

    # Not WorkflowDefinition.run_kind, which reads compose for these: a daemon
    # investigation on a definition that declares nothing stays the lead loop.
    @pytest.mark.asyncio
    @pytest.mark.parametrize("workflow_id", UNDECLARED)
    async def test_a_definition_that_declares_nothing_stays_investigate(
        self, workflow_id
    ):
        assert await self._enqueued_kind(_orchestrator(), workflow_id) == "investigate"

    @pytest.mark.asyncio
    async def test_a_definition_that_is_not_found_stays_investigate(self):
        assert await self._enqueued_kind(_orchestrator(), "no-such-workflow") == (
            "investigate"
        )

    # RUN_KINDS is the allow-list: a kind the worker has no loop for fails the
    # enqueue rather than being coerced into one it did not ask for.
    @pytest.mark.asyncio
    async def test_a_kind_outside_the_allow_list_fails_the_enqueue(self):
        orch = _orchestrator()
        orch._workflows = MagicMock()
        orch._workflows.get_workflow.return_value = WorkflowDefinition(
            "odd", None, {"run_kind": "wander"}, ""
        )
        with patch(
            "services.daemon.orchestrator.enqueue_run", new=AsyncMock()
        ) as enqueued:
            await orch._enqueue_investigation(_record(workflow_id="odd"))

        enqueued.assert_not_awaited()
        status, reason = orch._update_investigation_status.call_args[0][1:3]
        assert status == "failed"
        assert "wander" in reason


FINDING = {
    "finding_id": "f-1",
    "severity": "high",
    "description": "three failed logons on FYODOR-L",
    "entity_context": {"hostnames": ["FYODOR-L"], "usernames": ["fyodor"]},
    "mitre_predictions": {"T1110": 0.91, "T1078": 0.4},
}


# Shadow mode (#880): a second, independent run over the same finding, journaled
# beside the real one. Nothing here changes what the real run is handed.
class TestShadowAdjudication:
    async def _enqueue(self, orch, record):
        with patch(
            "services.daemon.orchestrator.enqueue_run", new=AsyncMock()
        ) as enqueued, patch("services.daemon.orchestrator.WorkflowRunService") as runs:
            orch._log_ai_decision = MagicMock()
            orch._save_investigation = MagicMock()
            await orch._enqueue_investigation(record)
        return enqueued, runs.return_value.begin_run

    def _shadowed(self):
        orch = _orchestrator()
        orch.config.shadow_adjudication = True
        return orch

    @pytest.mark.asyncio
    async def test_off_by_default_enqueues_one_run_and_no_row(self):
        orch = _orchestrator()
        assert orch.config.shadow_adjudication is False
        enqueued, begin_run = await self._enqueue(
            orch, _record(trigger_type="finding", findings=[FINDING])
        )
        enqueued.assert_awaited_once()
        begin_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_enqueues_exactly_one_adjudicate_run_beside_the_real_one(self):
        orch = self._shadowed()
        enqueued, begin_run = await self._enqueue(
            orch, _record(trigger_type="finding", findings=[FINDING])
        )

        assert enqueued.await_count == 2
        real, shadow = (call.args[0] for call in enqueued.await_args_list)
        assert real["run_id"] == run_id_for(INV)
        assert real["run_kind"] == "investigate"
        assert shadow["run_kind"] == "adjudicate"
        assert shadow["run_id"] == shadow_run_id_for(INV)
        assert shadow["run_id"] != real["run_id"]
        assert shadow["request"]["playbook"] == "workflow:shadow-adjudication"
        # The same brief plus one paragraph naming intake's choice; the same keys
        # and ceilings, so the two opinions were formed over the same inputs.
        assert shadow["request"]["prompt"].startswith(real["request"]["prompt"])
        assert "incident-response" in shadow["request"]["prompt"]
        assert shadow["request"]["recall_keys"] == real["request"]["recall_keys"]
        assert shadow["request"]["overrides"] == real["request"]["overrides"]
        # A stated hypothesis, because the definition declares none.
        (line,) = shadow["request"]["hypotheses"]
        assert "f-1" in line and "FYODOR-L" in line and "T1110" in line
        assert "incident-response" in line
        # The real request is untouched by the copy.
        assert real["request"]["playbook"] == "workflow:incident-response"

        # One workflow_runs row for the shadow, keyed by its run id, and nothing
        # else written: no second investigations row, no AIDecisionLog entry.
        begin_run.assert_called_once()
        row = begin_run.call_args.kwargs
        assert row["run_id"] == shadow_run_id_for(INV)
        assert row["workflow_id"] == "shadow-adjudication"
        assert row["trigger_context"]["run_kind"] == "adjudicate"
        assert row["trigger_context"]["investigation_id"] == INV
        orch._save_investigation.assert_not_called()
        orch._log_ai_decision.assert_not_called()
        assert orch._update_investigation_status.call_args[0][1] == "executing"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("trigger_type", ["manual", "scheduled", "case_review"])
    async def test_only_a_detection_finding_gets_a_shadow(self, trigger_type):
        orch = self._shadowed()
        enqueued, begin_run = await self._enqueue(
            orch, _record(trigger_type=trigger_type, findings=[FINDING])
        )
        enqueued.assert_awaited_once()
        begin_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_shadow_that_cannot_be_queued_leaves_the_real_run_executing(self):
        orch = self._shadowed()
        with patch(
            "services.daemon.orchestrator.enqueue_run",
            new=AsyncMock(side_effect=[None, RuntimeError("redis down")]),
        ), patch("services.daemon.orchestrator.WorkflowRunService"):
            await orch._enqueue_investigation(
                _record(trigger_type="finding", findings=[FINDING])
            )

        statuses = [c[0][1] for c in orch._update_investigation_status.call_args_list]
        assert statuses == ["executing"]

    # The derived id is a uuid like the real one: agent_events.run_id is a uuid
    # column, so a string suffix on the real id would be refused by the ledger.
    def test_shadow_run_id_is_a_distinct_deterministic_uuid(self):
        assert shadow_run_id_for(INV) == shadow_run_id_for(INV)
        assert shadow_run_id_for(INV) != run_id_for(INV)
        uuid.UUID(shadow_run_id_for(INV))


class TestReconcile:
    async def _reconcile(self, orch, projection):
        with patch(
            "services.daemon.orchestrator.read_projection",
            new=AsyncMock(return_value=projection),
        ):
            with patch("services.daemon.orchestrator.raise_for_checkpoint") as raised:
                await orch._reconcile(INV)
        return raised

    @pytest.mark.asyncio
    async def test_says_nothing_about_a_run_with_no_ledger_yet(self):
        orch = _orchestrator()
        await self._reconcile(orch, None)
        # Enqueued a moment ago is not failed, and must not be written as failed.
        orch._update_investigation_status.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_completed_run_goes_to_review(self):
        orch = _orchestrator()
        await self._reconcile(
            orch,
            {
                "status": "terminal",
                "outcome": "completed",
                "reason": "stale password",
                "iterations": 4,
            },
        )

        assert orch._update_investigation_status.call_args[0][1] == "review_submitted"

    @pytest.mark.asyncio
    async def test_a_failed_run_carries_its_reason(self):
        orch = _orchestrator()
        await self._reconcile(
            orch,
            {
                "status": "terminal",
                "outcome": "budget_exhausted",
                "reason": "the budget refused another iteration",
            },
        )

        status, reason = orch._update_investigation_status.call_args[0][1:3]
        assert status == "failed"
        assert reason == "the budget refused another iteration"

    @pytest.mark.asyncio
    async def test_a_parked_run_raises_an_approval_and_waits(self):
        orch = _orchestrator()
        checkpoint = {"checkpoint_id": "apr-c0ffee", "question": "isolate FYODOR-L?"}
        raised = await self._reconcile(
            orch, {"status": "waiting_approval", "open_checkpoint": checkpoint}
        )

        assert raised.call_args.kwargs["checkpoint_id"] == "apr-c0ffee"
        assert raised.call_args.kwargs["run_id"] == run_id_for(INV)
        assert orch._update_investigation_status.call_args[0][1] == "waiting_approval"

    @pytest.mark.asyncio
    async def test_records_progress_before_deciding_anything(self):
        orch = _orchestrator()
        await self._reconcile(
            orch, {"status": "running", "iterations": 3, "cost_usd": 0.4}
        )

        # The heartbeat lands even on a tick that changes no status, or the stale
        # check kills a run that is working.
        orch._record_progress.assert_called_once()
        assert orch._update_investigation_status.call_args[0][1] == "executing"
