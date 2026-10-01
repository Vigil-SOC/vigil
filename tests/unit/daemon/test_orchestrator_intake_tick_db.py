"""One intake tick against the throwaway Postgres: fixed work, same outcomes.

A tick expires, merges, launches and leaves waiting the same rows, in the same
rank order, as before the batching; a failed overlap lookup now holds its rows
instead of launching them. Its round trips don't grow with the queue.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import event

from core.time import utcnow
from services.daemon.config import OrchestratorConfig
from services.daemon.orchestrator import Orchestrator, _count_in_flight

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

FREE_SLOTS = 2


@pytest.fixture(autouse=True)
def clean():
    def wipe():
        from core.storage.connection import get_db_manager
        from core.storage.models import Case, Finding, IntakeTrigger, Investigation

        with get_db_manager().session_scope() as session:
            session.query(IntakeTrigger).filter(
                IntakeTrigger.finding_id.like("tick-%")
                | (IntakeTrigger.payload["workflow_id"].astext == "tick")
            ).delete(synchronize_session=False)
            session.query(Investigation).filter(
                Investigation.investigation_id.like("inv-tick-%")
            ).delete(synchronize_session=False)
            session.query(Case).filter(Case.case_id.like("case-tick-%")).delete(
                synchronize_session=False
            )
            session.query(Finding).filter(Finding.finding_id.like("tick-%")).delete(
                synchronize_session=False
            )

    wipe()
    yield
    wipe()


def _queue(*rows):
    """Detections as (name, severity, entity_context, age); human asks as (name,)."""
    from core.storage.connection import get_db_manager
    from core.storage.models import Finding, IntakeTrigger

    now = utcnow()
    ids = {}
    with get_db_manager().session_scope() as session:
        for name, *detection in rows:
            if not detection:
                trigger = IntakeTrigger(
                    kind="human_ask",
                    priority="medium",
                    payload={"workflow_id": "tick", "name": name},
                    created_at=now,
                )
            else:
                severity, context, age = detection
                session.add(
                    Finding(
                        finding_id=f"tick-{name}",
                        data_source="tick",
                        severity=severity,
                        entity_context=context,
                    )
                )
                trigger = IntakeTrigger(
                    kind="detection",
                    finding_id=f"tick-{name}",
                    priority=severity,
                    created_at=now - age,
                )
            session.add(trigger)
            session.flush()
            ids[name] = trigger.id
    return ids


def _live_work():
    """inv-tick-cased on a Case, inv-tick-hunt caseless, inv-tick-gone unreadable."""
    from core.storage.connection import get_db_manager
    from core.storage.models import Case, Investigation
    from core.storage.shared_ioc_repository import SharedIOCRepository, make_key

    now = utcnow()
    with get_db_manager().session_scope() as session:
        session.add(Case(case_id="case-tick-a", title="open work"))
        for inv_id, case_id in (
            ("inv-tick-cased", "case-tick-a"),
            ("inv-tick-hunt", None),
            ("inv-tick-gone", None),
        ):
            session.add(
                Investigation(
                    investigation_id=inv_id,
                    case_id=case_id,
                    workflow_id="incident-response",
                    trigger_type="finding",
                    status="executing",
                    workdir="/tmp/tick",
                    max_runtime_seconds=3600,
                    created_at=now,
                    started_at=now,
                    last_activity_at=now,
                )
            )
    with get_db_manager().session_scope() as session:
        repo = SharedIOCRepository(session)
        repo.record("inv-tick-cased", {make_key("ip", "10.9.0.1")})
        repo.record("inv-tick-hunt", {make_key("ip", "10.9.0.2")})
        repo.record("inv-tick-gone", {make_key("ip", "10.9.0.3")})


def _orchestrator(ours):
    """The real tick, with launching recorded rather than run."""
    from core.storage.database_data_service import DatabaseDataService
    from services.daemon.shared_intel import SharedIntelligence

    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig(intake_surge_depth=10**9)
    orch.config.max_concurrent_agents = _count_in_flight() + FREE_SLOTS
    orch.shared_intel = SharedIntelligence()
    orch._data_service = DatabaseDataService()
    orch.stats = {"dedup_prevented": 0, "investigations_created": 0}
    orch._intake_surge_active = False
    orch._hourly_budget_exhausted = MagicMock(return_value=False)

    launched = []

    async def launch(*_args, trigger_id=None, **_kwargs):
        launched.append(trigger_id)
        return f"inv-launched-{trigger_id}"

    orch._create_investigation = AsyncMock(side_effect=launch)
    orch._create_manual_investigation = AsyncMock(side_effect=launch)
    # Other tests leave queued rows behind; this tick only sees its own.
    queued = orch._queued_intake_triggers
    orch._queued_intake_triggers = lambda: [r for r in queued() if r["id"] in ours]
    return orch, launched


def _states():
    from core.storage.connection import get_db_manager
    from core.storage.models import IntakeTrigger

    with get_db_manager().session_scope() as session:
        return {
            row.id: (row.state, row.reason, row.merged_into)
            for row in session.query(IntakeTrigger).filter(
                IntakeTrigger.finding_id.like("tick-%")
                | (IntakeTrigger.payload["workflow_id"].astext == "tick")
            )
        }


def _case_findings(case_id):
    from core.storage.connection import get_db_manager
    from core.storage.models import Case

    with get_db_manager().session_scope() as session:
        return [f.finding_id for f in session.get(Case, case_id).findings]


@pytest.mark.asyncio
async def test_a_tick_expires_merges_launches_holds_and_leaves_waiting():
    _live_work()
    minute = timedelta(minutes=1)
    ids = _queue(
        ("expired", "critical", {"src_ip": "10.9.9.9"}, timedelta(hours=5)),
        ("merges", "high", {"src_ip": "10.9.0.1"}, 5 * minute),
        ("caseless", "high", {"src_ip": "10.9.0.2"}, 4 * minute),
        ("unreadable", "critical", {"src_ip": "10.9.0.3"}, 3 * minute),
        ("critical", "critical", {"src_ip": "10.9.9.1"}, 2 * minute),
        ("low", "low", {"src_ip": "10.9.9.2"}, minute),
    )
    orch, launched = _orchestrator(set(ids.values()))
    # inv-tick-gone vanishes between the overlap lookup and the read.
    read = orch._overlapping_case_ids
    orch._overlapping_case_ids = lambda inv_ids: {
        k: v for k, v in read(inv_ids).items() if k != "inv-tick-gone"
    }

    await orch._drain_intake(None)

    assert launched == [ids["critical"], ids["caseless"]]
    states = _states()
    assert states[ids["expired"]] == ("expired", "ttl_expired", None)
    assert states[ids["merges"]] == ("merged", "overlaps_open_work", "case-tick-a")
    assert _case_findings("case-tick-a") == ["tick-merges"]
    # Held, and the one without a slot waits: both still queued, neither decided.
    assert states[ids["unreadable"]] == ("queued", None, None)
    assert states[ids["low"]] == ("queued", None, None)
    assert orch.stats["dedup_prevented"] == 1


@pytest.mark.asyncio
async def test_a_failed_overlap_lookup_holds_rows_with_entities(monkeypatch):
    from core.storage.shared_ioc_repository import SharedIOCRepository

    def unreachable(self, keys):
        raise RuntimeError("shared_iocs unreadable")

    monkeypatch.setattr(SharedIOCRepository, "open_investigations_by_key", unreachable)
    ids = _queue(
        ("with-entity", "critical", {"src_ip": "10.9.0.1"}, timedelta(minutes=2)),
        ("no-entity", "high", {}, timedelta(minutes=1)),
        ("ask",),
    )
    orch, launched = _orchestrator(set(ids.values()))

    await orch._drain_intake(None)

    assert sorted(launched) == sorted([ids["no-entity"], ids["ask"]])
    assert _states()[ids["with-entity"]] == ("queued", None, None)


def _round_trips(orch):
    from core.storage.connection import get_db_manager

    statements = []

    def count(*_args):
        statements.append(1)

    engine = get_db_manager().engine
    event.listen(engine, "before_cursor_execute", count)
    return engine, count, statements


@pytest.mark.asyncio
async def test_round_trips_do_not_grow_with_the_queue():
    trips = {}
    for size in (20, 2000):
        ids = _queue(
            *(
                (
                    f"q{size}-{n}",
                    "high",
                    {"src_ip": f"10.{size % 250}.{n // 250}.{n % 250}"},
                    timedelta(seconds=n),
                )
                for n in range(size)
            )
        )
        orch, launched = _orchestrator(set(ids.values()))
        engine, count, statements = _round_trips(orch)
        try:
            await orch._drain_intake(None)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert len(launched) == FREE_SLOTS
        trips[size] = len(statements)

    assert trips[20] == trips[2000]


def _reads():
    """Round trips and rows read, counted on the engine."""
    from core.storage.connection import get_db_manager

    seen = {"trips": 0, "rows": 0}

    def count(_conn, cursor, *_args):
        seen["trips"] += 1
        if cursor.description is not None:
            seen["rows"] += cursor.rowcount

    engine = get_db_manager().engine
    event.listen(engine, "after_cursor_execute", count)
    return lambda: event.remove(engine, "after_cursor_execute", count), seen


def _grow_case(case_id, n):
    from sqlalchemy import insert

    from core.storage.connection import get_db_manager
    from core.storage.models import Finding, case_findings

    with get_db_manager().session_scope() as session:
        ids = [f"tick-pad-{i}" for i in range(n)]
        session.execute(
            insert(Finding), [{"finding_id": i, "data_source": "tick"} for i in ids]
        )
        session.execute(
            insert(case_findings),
            [{"case_id": case_id, "finding_id": i, "added_at": utcnow()} for i in ids],
        )


@pytest.mark.asyncio
async def test_a_merge_costs_the_same_however_big_the_case():
    # A surge on one host: every queued row merges into the same Case.
    _live_work()
    cost = {}
    for run in ("small", "big"):
        if run == "big":
            _grow_case("case-tick-a", 600)
        ids = _queue(
            *(
                (f"m{run}-{n}", "high", {"src_ip": "10.9.0.1"}, timedelta(seconds=n))
                for n in range(20)
            )
        )
        orch, launched = _orchestrator(set(ids.values()))
        stop, seen = _reads()
        try:
            await orch._drain_intake(None)
        finally:
            stop()
        assert launched == []
        assert all(_states()[i][0] == "merged" for i in ids.values())
        cost[run] = seen

    assert len(_case_findings("case-tick-a")) == 640
    assert cost["big"] == cost["small"]
