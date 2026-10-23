from __future__ import annotations

import pytest

from core.agents import tool_registry
from core.api.v1 import cases_router
from core.cases.case_automation_service import cluster_findings_by_attack_path
from core.storage.connection import get_db_session
from core.storage.database_data_service import DatabaseDataService
from core.storage.models import Case, CaseSLA, SLAPolicy
from core.workflows import run_bridge_router

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

POLICY = "sla-test-open-case-high"


@pytest.fixture
def default_high_policy():
    db = get_db_session()
    try:
        db.add(
            SLAPolicy(
                policy_id=POLICY,
                name=POLICY,
                priority_level="high",
                response_time_hours=1.0,
                resolution_time_hours=8.0,
                is_default=True,
                is_active=True,
            )
        )
        db.commit()
        yield
    finally:
        db.query(CaseSLA).filter(CaseSLA.sla_policy_id == POLICY).delete()
        db.query(Case).filter(Case.priority == "high").delete()
        db.query(SLAPolicy).filter(SLAPolicy.policy_id == POLICY).delete()
        db.commit()
        db.close()


def _sla_policy_of(case_id: str):
    db = get_db_session()
    try:
        sla = db.query(CaseSLA).filter(CaseSLA.case_id == case_id).first()
        return sla.sla_policy_id if sla else None
    finally:
        db.close()


def _via_route(monkeypatch) -> str:
    created = cases_router.create_case(
        cases_router.CaseCreate(title="route", finding_ids=[], priority="high")
    )
    return created["case_id"]


def _via_agent_tool(monkeypatch) -> str:
    return tool_registry.create_case(title="agent tool", priority="high")["case_id"]


def _via_vstrike(monkeypatch) -> str:
    finding = {
        "finding_id": "f-vstrike-sla",
        "severity": "high",
        "entity_context": {
            "vstrike": {"segment": "seg-a", "attack_path": ["gw-1", "srv-1"]}
        },
    }
    monkeypatch.setattr(
        DatabaseDataService, "get_finding", lambda self, finding_id: finding
    )
    (case_id,) = cluster_findings_by_attack_path(["f-vstrike-sla"])
    return case_id


def _via_handoff(monkeypatch) -> str:
    handoff = run_bridge_router.TerminalHandoff(case_id="h-sla", title="handoff")
    return run_bridge_router._open_case("run-sla", handoff)


@pytest.mark.parametrize(
    "open_one",
    [_via_route, _via_agent_tool, _via_vstrike, _via_handoff],
    ids=["route", "agent-tool", "vstrike", "workflow-handoff"],
)
def test_a_new_case_gets_the_default_sla_for_its_priority(
    default_high_policy, monkeypatch, open_one
):
    case_id = open_one(monkeypatch)

    assert _sla_policy_of(case_id) == POLICY
