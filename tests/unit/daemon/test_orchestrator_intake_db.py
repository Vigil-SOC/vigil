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
