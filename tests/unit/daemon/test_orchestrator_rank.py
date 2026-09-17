"""Admission ranks by severity then age, expires on a TTL, and waits for a slot (#922)."""

from __future__ import annotations

from datetime import datetime, timedelta
from inspect import getsource
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.daemon.config import DaemonConfig, OrchestratorConfig
from services.daemon.orchestrator import (
    Orchestrator,
    intake_age_seconds,
    intake_severity_band,
    rank_intake_row,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 16, 12, 0, 0)
TTL = 4 * 3600
FRAC = 0.25


@pytest.fixture(autouse=True)
def freeze_intake_clock(monkeypatch):
    monkeypatch.setattr("services.daemon.orchestrator.utcnow", lambda: NOW)


def _rank(row, now=NOW):
    return rank_intake_row(row, now=now, ttl_seconds=TTL, promote_fraction=FRAC)


def _ordered(rows, now=NOW):
    return sorted(rows, key=lambda r: _rank(r, now=now))


def _detection(severity, *, age_s=60, finding_id="f", **extra):
    row = {
        "id": extra.pop("id", finding_id),
        "kind": "detection",
        "finding_id": finding_id,
        "priority": extra.pop("priority", "medium"),
        "created_at": extra.pop("created_at", NOW - timedelta(seconds=age_s)),
        "_finding": {"finding_id": finding_id, "severity": severity},
    }
    row.update(extra)
    return row


def _human_ask(priority, *, age_s=60, **extra):
    row = {
        "id": extra.pop("id", priority),
        "kind": "human_ask",
        "priority": priority,
        "payload": extra.pop("payload", {"workflow_id": "threat-hunt"}),
        "created_at": extra.pop("created_at", NOW - timedelta(seconds=age_s)),
    }
    row.update(extra)
    return row


def _orchestrator(**extra) -> Orchestrator:
    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig()
    orch.shared_intel = MagicMock()
    orch.shared_intel.check_overlap.return_value = None
    orch.stats = {"dedup_prevented": 0, "investigations_created": 0}
    orch._create_investigation = AsyncMock()
    orch._create_manual_investigation = AsyncMock()
    orch._decide_trigger = MagicMock()
    orch._data_service = MagicMock()
    orch._open_case_for_finding = MagicMock(return_value="case-1")
    orch._attach_finding_to_overlap = MagicMock(return_value="case-1")
    orch._in_flight = MagicMock(return_value=0)
    for key, value in extra.items():
        setattr(orch, key, value)
    return orch


def test_ttl_constants_are_not_settings():
    cfg = OrchestratorConfig()
    assert cfg.intake_ttl_seconds == TTL
    assert cfg.intake_ttl_promote_fraction == FRAC
    assert "intake_ttl" not in getsource(DaemonConfig.from_env)


def test_critical_launches_before_high_and_older_high_before_newer():
    older_high = _detection("high", age_s=120, finding_id="f-h1", id=1)
    critical = _detection("critical", age_s=30, finding_id="f-c", id=2)
    newer_high = _detection("high", age_s=10, finding_id="f-h2", id=3)

    assert [r["finding_id"] for r in _ordered([newer_high, critical, older_high])] == [
        "f-c",
        "f-h1",
        "f-h2",
    ]


def test_all_five_bands_and_medium_outranks_low():
    rows = [
        _detection(None, finding_id="f-none", id=1),
        _detection("low", finding_id="f-low", id=2),
        _detection("medium", finding_id="f-med", id=3),
        _detection("high", finding_id="f-high", id=4),
        _detection("critical", finding_id="f-crit", id=5),
    ]
    assert [r["finding_id"] for r in _ordered(rows)] == [
        "f-crit",
        "f-high",
        "f-med",
        "f-low",
        "f-none",
    ]
    # Alphabetically low < medium; the map must not use string order.
    assert _rank(rows[2]) < _rank(rows[1])


def test_a_finding_that_gains_severity_moves_band():
    row = _detection(None, finding_id="f-late")
    assert intake_severity_band("detection", finding_severity=None) == "unknown"
    assert _rank(row)[1] == _rank(_detection(None, finding_id="x"))[1]

    row["_finding"] = {"finding_id": "f-late", "severity": "high"}
    assert _rank(row)[1] == _rank(_detection("high", finding_id="x"))[1]


def test_human_ask_and_schedule_use_stated_priority():
    ask = _human_ask("critical", age_s=10)
    schedule = {
        "id": 2,
        "kind": "schedule",
        "priority": "low",
        "created_at": NOW - timedelta(seconds=120),
        "payload": {"workflow_id": "threat-hunt"},
    }
    unrated = _detection(None, age_s=5, finding_id="f-none")
    assert [r["id"] for r in _ordered([unrated, schedule, ask])] == [
        ask["id"],
        schedule["id"],
        unrated["id"],
    ]


def test_a_row_in_the_promotion_window_beats_a_fresher_critical():
    promoted = _detection(
        "low",
        age_s=int(TTL * 0.9),
        finding_id="f-old",
    )
    fresh_critical = _detection("critical", age_s=5, finding_id="f-crit")
    assert [r["finding_id"] for r in _ordered([fresh_critical, promoted])] == [
        "f-old",
        "f-crit",
    ]


def test_a_row_without_created_at_is_new_not_oldest():
    undated = _detection("high", finding_id="f-undated")
    del undated["created_at"]
    older = _detection("high", age_s=120, finding_id="f-old")

    assert intake_age_seconds(undated, NOW) == 0.0
    assert [r["finding_id"] for r in _ordered([undated, older])] == [
        "f-old",
        "f-undated",
    ]


def test_detection_ranks_from_finding_severity_not_row_priority():
    # Processor stamps unrated as medium on insert; ranking must not trust that.
    row = _detection(None, finding_id="f-none", priority="medium")
    assert (
        intake_severity_band("detection", finding_severity=None, priority="medium")
        == "unknown"
    )
    assert _rank(row)[1] == _rank(_detection(None, finding_id="x"))[1]


@pytest.mark.asyncio
async def test_one_slot_launches_critical_then_older_high():
    launched = []
    inflight = {"n": 0}

    async def create(**kwargs):
        launched.append(kwargs["trigger_id"])
        inflight["n"] += 1

    orch = _orchestrator()
    orch.config.max_concurrent_agents = 1
    orch._create_investigation = create
    orch._in_flight = lambda: inflight["n"]

    findings = {
        "f-h1": {
            "finding_id": "f-h1",
            "severity": "high",
            "entity_context": {},
        },
        "f-c": {
            "finding_id": "f-c",
            "severity": "critical",
            "entity_context": {},
        },
        "f-h2": {
            "finding_id": "f-h2",
            "severity": "high",
            "entity_context": {},
        },
    }
    orch._hydrate_detection_finding = MagicMock(
        side_effect=lambda row: findings[row["finding_id"]]
    )
    rows = [
        _detection("high", age_s=120, finding_id="f-h1", id=1),
        _detection("critical", age_s=60, finding_id="f-c", id=2),
        _detection("high", age_s=10, finding_id="f-h2", id=3),
    ]
    orch._queued_intake_triggers = lambda: [r for r in rows if r["id"] not in launched]

    await orch._drain_intake(None)
    assert launched == [2]
    inflight["n"] = 0

    await orch._drain_intake(None)
    assert launched == [2, 1]


@pytest.mark.asyncio
async def test_one_slot_launches_medium_before_low_before_unrated():
    launched = []
    inflight = {"n": 0}

    async def create(**kwargs):
        launched.append(kwargs["trigger_id"])
        inflight["n"] += 1

    orch = _orchestrator()
    orch.config.max_concurrent_agents = 1
    orch._create_investigation = create
    orch._in_flight = lambda: inflight["n"]
    findings = {
        "f-none": {"finding_id": "f-none", "severity": None, "entity_context": {}},
        "f-low": {"finding_id": "f-low", "severity": "low", "entity_context": {}},
        "f-med": {"finding_id": "f-med", "severity": "medium", "entity_context": {}},
    }
    orch._hydrate_detection_finding = MagicMock(
        side_effect=lambda row: findings[row["finding_id"]]
    )
    rows = [
        _detection(None, age_s=90, finding_id="f-none", id=1),
        _detection("low", age_s=60, finding_id="f-low", id=2),
        _detection("medium", age_s=30, finding_id="f-med", id=3),
    ]
    orch._queued_intake_triggers = lambda: [r for r in rows if r["id"] not in launched]

    await orch._drain_intake(None)
    assert launched == [3]
    inflight["n"] = 0
    await orch._drain_intake(None)
    assert launched == [3, 2]
    inflight["n"] = 0
    await orch._drain_intake(None)
    assert launched == [3, 2, 1]


@pytest.mark.asyncio
async def test_past_ttl_expires_with_a_reason_even_when_no_slot_is_free():
    orch = _orchestrator()
    orch._in_flight = MagicMock(return_value=orch.config.max_concurrent_agents)
    expired = _detection(
        "critical",
        age_s=TTL + 1,
        finding_id="f-old",
        id=1,
    )
    waiting = _detection("low", age_s=10, finding_id="f-low", id=2)
    orch._hydrate_detection_finding = MagicMock(
        side_effect=lambda row: {
            "finding_id": row["finding_id"],
            "severity": "critical" if row["id"] == 1 else "low",
            "entity_context": {},
        }
    )
    orch._queued_intake_triggers = MagicMock(return_value=[expired, waiting])

    await orch._drain_intake(None)

    orch._decide_trigger.assert_called_once_with(
        1, state="expired", reason="ttl_expired"
    )
    orch._create_investigation.assert_not_awaited()


@pytest.mark.asyncio
async def test_overlap_and_expiry_resolve_when_the_fleet_is_full():
    orch = _orchestrator()
    orch._in_flight = MagicMock(return_value=orch.config.max_concurrent_agents)
    orch.shared_intel.check_overlap.side_effect = lambda finding: (
        ["inv-1"] if finding["finding_id"] == "f-overlap" else None
    )
    expired = _detection("low", age_s=TTL + 60, finding_id="f-old", id=1)
    overlapping = _detection("high", age_s=10, finding_id="f-overlap", id=2)
    waiting = _detection("medium", age_s=5, finding_id="f-med", id=3)
    orch._hydrate_detection_finding = MagicMock(
        side_effect=lambda row: {
            "finding_id": row["finding_id"],
            "severity": {"f-old": "low", "f-overlap": "high", "f-med": "medium"}[
                row["finding_id"]
            ],
            "entity_context": {},
        }
    )
    orch._queued_intake_triggers = MagicMock(
        return_value=[expired, overlapping, waiting]
    )

    await orch._drain_intake(None)

    states = {c.args[0]: c.kwargs["state"] for c in orch._decide_trigger.call_args_list}
    assert states == {1: "expired", 2: "merged"}
    orch._create_investigation.assert_not_awaited()
    assert all(
        c.kwargs.get("state") != "shed" for c in orch._decide_trigger.call_args_list
    )


@pytest.mark.asyncio
async def test_create_investigation_always_saves_assigned(tmp_path):
    from services.daemon.workdir import WorkdirManager

    orch = object.__new__(Orchestrator)
    orch.config = OrchestratorConfig(dry_run=True)
    orch.workdir = WorkdirManager(str(tmp_path))
    orch._workflows = MagicMock()
    orch.shared_intel = MagicMock()
    orch.stats = {"investigations_created": 0}
    orch._save_investigation = MagicMock(return_value=True)
    orch._check_cross_correlations = AsyncMock()
    orch._in_flight = MagicMock(return_value=99)

    await orch._create_investigation(
        workflow_id="incident-response",
        findings=[{"finding_id": "f-1", "severity": "high"}],
        trigger_type="finding",
        priority="high",
    )

    assert orch._save_investigation.call_args[0][0]["status"] == "assigned"


@pytest.mark.asyncio
async def test_pickup_only_walks_assigned():
    orch = _orchestrator()
    seen = []
    orch._get_investigations_by_status = lambda status: seen.append(status) or []
    orch._enqueue_investigation = AsyncMock()
    orch._update_investigation_status = MagicMock()

    await orch._pickup_queued_investigations(None)

    assert seen == ["assigned"]
    orch._enqueue_investigation.assert_not_awaited()
