"""The case pill is one function, and a shadow run is not the audit run."""

from core.agents.projections import run_id_for
from services.daemon.orchestrator import shadow_run_id_for
from core.cases.case_state import (
    budget_health,
    combined_state,
    detail_fields,
    investigation_ref,
)
from core.storage.models import CaseClosureInfo, Investigation

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
    assert fields["investigations"][0]["live"] is False
