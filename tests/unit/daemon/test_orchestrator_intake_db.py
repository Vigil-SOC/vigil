"""Durable intake rows, against the throwaway Postgres (#918)."""

from __future__ import annotations

import pytest

from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator, insert_intake_trigger

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]


def _row(trigger_id: int) -> dict:
    from core.storage.connection import get_db_manager
    from core.storage.models import IntakeTrigger
    from core.storage.schemas import IntakeTriggerSchema

    with get_db_manager().session_scope() as session:
        row = session.get(IntakeTrigger, trigger_id)
        return IntakeTriggerSchema.dump(row)


def test_a_second_queued_row_for_the_same_finding_is_rejected():
    first = insert_intake_trigger(
        kind="detection", finding_id="f-dup-918", priority="high"
    )
    second = insert_intake_trigger(
        kind="detection", finding_id="f-dup-918", priority="high"
    )
    assert first is not None
    assert second is None
    assert _row(first)["state"] == "queued"


def test_human_ask_rows_do_not_collide_on_a_null_finding_id():
    a = insert_intake_trigger(
        kind="human_ask",
        priority="medium",
        payload={"workflow_id": "incident-response"},
    )
    b = insert_intake_trigger(
        kind="human_ask",
        priority="low",
        payload={"workflow_id": "threat-hunt"},
    )
    assert a and b and a != b


def test_queued_rows_drain_oldest_first():
    older = insert_intake_trigger(
        kind="human_ask", priority="high", payload={"workflow_id": "a"}
    )
    newer = insert_intake_trigger(
        kind="human_ask", priority="low", payload={"workflow_id": "b"}
    )
    orch = object.__new__(Orchestrator)
    ids = [row["id"] for row in orch._queued_intake_triggers()]
    assert ids.index(older) < ids.index(newer)


def test_launch_cas_rolls_back_a_second_investigation():
    trigger_id = insert_intake_trigger(
        kind="human_ask",
        priority="medium",
        payload={"workflow_id": "incident-response"},
    )
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()

    def record(inv_id: str) -> dict:
        return {
            "investigation_id": inv_id,
            "case_id": None,
            "workflow_id": "incident-response",
            "trigger_type": "manual",
            "trigger_ids": [],
            "status": "assigned",
            "workdir": "/tmp/inv",
            "current_step": 1,
            "total_steps": 1,
            "priority": "medium",
            "max_iterations": 50,
            "max_cost_usd": 5.0,
            "max_runtime_seconds": 3600,
        }

    assert orch._save_investigation(record("inv-918-a"), trigger_id=trigger_id) is True
    assert orch._save_investigation(record("inv-918-b"), trigger_id=trigger_id) is False
    dumped = _row(trigger_id)
    assert dumped["state"] == "launched"
    assert dumped["investigation_id"] == "inv-918-a"
    assert dumped["decided_at"] is not None

    from core.storage.connection import get_db_manager
    from core.storage.models import Investigation

    with get_db_manager().session_scope() as session:
        assert session.get(Investigation, "inv-918-a") is not None
        assert session.get(Investigation, "inv-918-b") is None


@pytest.mark.asyncio
async def test_intake_list_returns_an_inserted_row():
    from services.api.routers.orchestrator import list_intake_triggers

    trigger_id = insert_intake_trigger(
        kind="human_ask",
        priority="medium",
        payload={"workflow_id": "threat-hunt"},
    )

    result = await list_intake_triggers(state=None, limit=100)

    assert result["count"] >= 1
    assert any(row["id"] == trigger_id for row in result["triggers"])
    listed = next(row for row in result["triggers"] if row["id"] == trigger_id)
    assert listed["state"] == "queued"
    assert listed["kind"] == "human_ask"
    assert listed["case_id"] is None


def _finding(finding_id: str) -> None:
    from core.storage.connection import get_db_manager
    from core.storage.models import Finding

    with get_db_manager().session_scope() as session:
        session.add(
            Finding(
                finding_id=finding_id,
                data_source="test",
                severity="high",
                description="three failed logons on FYODOR-L",
            )
        )


def _claim_record(inv_id: str, case_id: str, finding_id: str) -> dict:
    return {
        "investigation_id": inv_id,
        "case_id": case_id,
        "workflow_id": "incident-response",
        "trigger_type": "finding",
        "trigger_ids": [finding_id],
        "status": "assigned",
        "workdir": "/tmp/inv",
        "current_step": 1,
        "total_steps": 1,
        "priority": "high",
        "max_iterations": 50,
        "max_cost_usd": 5.0,
        "max_runtime_seconds": 3600,
    }


def test_claim_mints_case_then_launches():
    from core.storage.connection import get_db_manager
    from core.storage.models import Case, Investigation
    from services.daemon.orchestrator import CaseSpec

    finding_id = "f-claim-1000"
    case_id = "case-claim-1000"
    inv_id = "inv-claim-1000"
    _finding(finding_id)
    trigger_id = insert_intake_trigger(
        kind="detection", finding_id=finding_id, priority="high"
    )
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    spec = CaseSpec(
        title="three failed logons on FYODOR-L",
        finding_ids=[finding_id],
        priority="high",
        case_id=case_id,
    )

    assert (
        orch._save_investigation(
            _claim_record(inv_id, case_id, finding_id),
            trigger_id=trigger_id,
            mint_case=spec,
        )
        is True
    )

    dumped = _row(trigger_id)
    assert dumped["state"] == "launched"
    assert dumped["case_id"] == case_id
    assert dumped["investigation_id"] == inv_id
    with get_db_manager().session_scope() as session:
        inv = session.get(Investigation, inv_id)
        case = session.get(Case, case_id)
        assert inv is not None and inv.case_id == case_id
        assert case is not None
        assert [f.finding_id for f in case.findings] == [finding_id]


def test_mint_failure_leaves_row_queued_and_no_investigation_and_no_case(monkeypatch):
    from core.storage.connection import get_db_manager
    from core.storage.models import Case, Investigation
    from services.daemon.orchestrator import CaseSpec

    finding_id = "f-mintfail-1000"
    case_id = "case-mintfail-1000"
    inv_id = "inv-mintfail-1000"
    trigger_id = insert_intake_trigger(
        kind="detection", finding_id=finding_id, priority="high"
    )
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()

    def boom(session, spec):
        raise RuntimeError("mint failed")

    monkeypatch.setattr("services.daemon.orchestrator._mint_case", boom)
    spec = CaseSpec(
        title="t", finding_ids=[finding_id], priority="high", case_id=case_id
    )

    assert (
        orch._save_investigation(
            _claim_record(inv_id, case_id, finding_id),
            trigger_id=trigger_id,
            mint_case=spec,
        )
        is False
    )

    dumped = _row(trigger_id)
    assert dumped["state"] == "queued"
    assert dumped["investigation_id"] is None
    assert dumped["case_id"] is None
    with get_db_manager().session_scope() as session:
        assert session.get(Investigation, inv_id) is None
        assert session.get(Case, case_id) is None


def test_claim_cas_loses_race_writes_neither_case_nor_investigation():
    from core.storage.connection import get_db_manager
    from core.storage.models import Case, Investigation
    from services.daemon.orchestrator import CaseSpec

    finding_id = "f-cas-1000"
    trigger_id = insert_intake_trigger(
        kind="detection", finding_id=finding_id, priority="high"
    )
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    spec_a = CaseSpec(
        title="a", finding_ids=[finding_id], priority="high", case_id="case-cas-a"
    )
    spec_b = CaseSpec(
        title="b", finding_ids=[finding_id], priority="high", case_id="case-cas-b"
    )

    assert (
        orch._save_investigation(
            _claim_record("inv-cas-a", "case-cas-a", finding_id),
            trigger_id=trigger_id,
            mint_case=spec_a,
        )
        is True
    )
    assert (
        orch._save_investigation(
            _claim_record("inv-cas-b", "case-cas-b", finding_id),
            trigger_id=trigger_id,
            mint_case=spec_b,
        )
        is False
    )

    dumped = _row(trigger_id)
    assert dumped["state"] == "launched"
    assert dumped["case_id"] == "case-cas-a"
    assert dumped["investigation_id"] == "inv-cas-a"
    with get_db_manager().session_scope() as session:
        assert session.get(Investigation, "inv-cas-a") is not None
        assert session.get(Investigation, "inv-cas-b") is None
        assert session.get(Case, "case-cas-a") is not None
        assert session.get(Case, "case-cas-b") is None
