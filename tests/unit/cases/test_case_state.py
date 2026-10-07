"""The case pill is one function, and a shadow run is not the audit run."""

from datetime import datetime, timedelta, timezone

from core.agents.projections import run_id_for
from core.cases.case_state import (
    budget_health,
    combined_state,
    detail_fields,
    investigation_ref,
    workflow_run_ref,
)
from core.storage.models import CaseClosureInfo, Investigation, WorkflowRun
from services.daemon.orchestrator import shadow_run_id_for

INV = "inv-case-state"
EPOCH = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _inv(status: str, *, cost: float = 1, cap: float = 10) -> Investigation:
    inv = Investigation(
        investigation_id=INV,
        case_id="case-1",
        workflow_id="incident-response",
        trigger_type="manual",
        trigger_ids=[],
        status=status,
        workdir="/tmp",
        cost_usd=cost,
        max_cost_usd=cap,
        iteration_count=2,
    )
    inv.created_at = EPOCH
    return inv


def _run(
    status: str, *, run_id: str = "wfr-1", started_at=EPOCH, cost: float = 0.5
) -> WorkflowRun:
    run = WorkflowRun(
        run_id=run_id,
        workflow_id="threat-hunt",
        workflow_name="Threat hunt",
        status=status,
        trigger_context={"case_id": "case-1", "hypothesis": "a claim"},
        total_cost_usd=cost,
    )
    run.started_at = started_at
    return run


def test_closed_case_stays_closed_even_with_a_live_investigation():
    assert combined_state("closed", ["executing", "assigned"]) == "closed"


def test_a_live_investigation_contributes_its_status_newest_first():
    assert combined_state("open", ["completed", "executing"]) == "executing"
    assert combined_state("open", ["waiting_approval"]) == "waiting_approval"


def test_a_finished_run_leaves_the_case_status_standing():
    assert combined_state("open", ["completed", "failed"]) == "open"
    assert combined_state("new", []) == "new"


def test_budget_health_uses_the_sla_cuts():
    assert budget_health(7, 10) == "healthy"
    assert budget_health(7.5, 10) == "warning"
    assert budget_health(9, 10) == "critical"
    assert budget_health(1, 0) == "healthy"


def test_the_audit_run_is_run_id_for_and_not_the_shadow():
    ref = investigation_ref(_inv("executing"))
    assert ref["run_id"] == run_id_for(INV)
    assert ref["run_id"] != shadow_run_id_for(INV)
    assert ref["live"] is True
    assert ref["budget_health"] == "healthy"


def test_detail_fields_carry_the_closure_the_summary_reads():
    closure = CaseClosureInfo(
        case_id="case-1",
        closure_category="false_positive",
        closed_by="ada",
        closed_by_kind="analyst",
        false_positive_reason="the scanner",
    )
    fields = detail_fields("closed", [_inv("completed")], closure)
    assert fields["combined_state"] == "closed"
    assert fields["closure"]["closed_by_kind"] == "analyst"
    assert fields["closure"]["verdict"] == "the scanner"
    assert fields["investigations"][0]["live"] is False


def test_a_workflow_run_ref_has_no_investigation_and_reads_its_own_fields():
    ref = workflow_run_ref(_run("running"))
    assert ref["investigation_id"] is None
    assert ref["run_id"] == "wfr-1"
    assert ref["workflow_id"] == "threat-hunt"
    assert ref["status"] == "running"
    assert ref["live"] is True
    assert ref["cost_usd"] == 0.5
    assert ref["max_cost_usd"] == 0
    assert ref["iteration_count"] == 0
    assert ref["created_at"] == EPOCH.isoformat()


def test_a_finished_workflow_run_is_not_live():
    assert workflow_run_ref(_run("completed"))["live"] is False
    assert workflow_run_ref(_run("paused"))["live"] is True


def test_an_in_flight_hunt_reads_as_live_not_open():
    assert combined_state("open", ["running"]) == "running"
    assert combined_state("open", ["paused"]) == "paused"
    assert combined_state("open", ["completed"]) == "open"
    assert combined_state("closed", ["running"]) == "closed"


def test_detail_fields_show_a_run_only_case_its_run():
    fields = detail_fields("open", [], None, workflow_runs=[_run("running")])
    assert fields["combined_state"] == "running"
    assert [ref["run_id"] for ref in fields["investigations"]] == ["wfr-1"]
    assert fields["investigations"][0]["investigation_id"] is None


def test_detail_fields_merge_investigations_and_runs_newest_first():
    older = _run("completed", run_id="wfr-old", started_at=EPOCH - timedelta(hours=2))
    newer = _run("running", run_id="wfr-new", started_at=EPOCH + timedelta(hours=1))
    fields = detail_fields(
        "open", [_inv("completed")], None, workflow_runs=[older, newer]
    )
    assert [ref["run_id"] for ref in fields["investigations"]] == [
        "wfr-new",
        run_id_for(INV),
        "wfr-old",
    ]
    # The newest entry is the live hunt, so the pill follows it.
    assert fields["combined_state"] == "running"


def test_detail_fields_dedupe_a_run_that_is_also_an_investigation():
    duplicate = _run("running", run_id=run_id_for(INV))
    fields = detail_fields("open", [_inv("executing")], None, workflow_runs=[duplicate])
    assert len(fields["investigations"]) == 1
    # The investigation ref wins: it carries the cap and the iterations.
    assert fields["investigations"][0]["investigation_id"] == INV
    assert fields["investigations"][0]["max_cost_usd"] == 10
