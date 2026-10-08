"""The case pill is one function, and a shadow run is not the audit run."""

from core.agents.projections import run_id_for
from services.daemon.orchestrator import shadow_run_id_for
from core.cases.case_state import (
    budget_health,
    case_run_refs,
    closure_view,
    combined_state,
    detail_fields,
    investigation_ref,
)
from datetime import datetime, timezone

from core.storage.models import CaseClosureInfo, Investigation, WorkflowRun

INV = "inv-case-state"


def _inv(status: str, *, cost: float = 1, cap: float = 10) -> Investigation:
    return Investigation(
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
    assert fields["closure"]["closed_at"] is None
    assert fields["investigations"][0]["live"] is False


def _run(run_id: str, status: str, at: datetime, cost: float = 0.5) -> WorkflowRun:
    return WorkflowRun(
        run_id=run_id,
        workflow_id="threat-hunt",
        workflow_name="Threat hunt",
        status=status,
        trigger_context={"case_id": "case-1"},
        started_at=at,
        total_cost_usd=cost,
    )


def test_a_run_started_on_the_case_is_listed_newest_first_beside_investigations():
    old_inv = _inv("completed")
    old_inv.created_at = datetime(2026, 10, 1)
    runs = [_run("run-new", "running", datetime(2026, 10, 7))]
    refs = [ref for ref, _ in case_run_refs([old_inv], runs)]
    assert [ref["run_id"] for ref in refs] == ["run-new", run_id_for(INV)]
    assert refs[0]["investigation_id"] is None
    assert refs[0]["workflow_id"] == "threat-hunt"
    assert refs[0]["cost_usd"] == 0.5
    assert refs[0]["max_cost_usd"] == 0 and refs[0]["iteration_count"] == 0


def test_the_investigations_own_run_is_listed_once():
    inv = _inv("executing")
    inv.created_at = datetime(2026, 10, 7)
    twin = _run(run_id_for(INV), "running", datetime(2026, 10, 7))
    refs = [ref for ref, _ in case_run_refs([inv], [twin])]
    assert [ref["investigation_id"] for ref in refs] == [INV]


def test_an_in_flight_run_reads_as_live_and_a_finished_one_does_not():
    running = detail_fields(
        "open", [], None, [_run("r1", "running", datetime(2026, 10, 7))]
    )
    assert running["combined_state"] == "executing"
    assert running["investigations"][0]["live"] is True
    paused = detail_fields(
        "open", [], None, [_run("r2", "paused", datetime(2026, 10, 7))]
    )
    assert paused["combined_state"] == "paused"
    done = detail_fields(
        "open", [], None, [_run("r3", "completed", datetime(2026, 10, 7))]
    )
    assert done["combined_state"] == "open"
    assert done["investigations"][0]["live"] is False
    closed = detail_fields(
        "closed", [], None, [_run("r4", "running", datetime(2026, 10, 7))]
    )
    assert closed["combined_state"] == "closed"


def test_closure_view_carries_closed_at_as_iso():
    closed_at = datetime(2026, 6, 15, 15, 48, tzinfo=timezone.utc)
    closure = CaseClosureInfo(
        case_id="case-1",
        closure_category="resolved",
        closed_by="ada",
        closed_at=closed_at,
    )
    assert closure_view(closure)["closed_at"] == closed_at.isoformat()
