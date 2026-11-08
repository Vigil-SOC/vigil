"""The daily detection gaps run reads only what is new since its stored marker, raises
a gap only at or above a floor, ranks what it raises by count, and does not raise a
gap an open Case already carries. The marker moves to the run's start, and only when
the run succeeds."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core.detections import gap_run
from core.detections.gaps import missed_runs_report
from services.daemon import orchestrator, scheduler

T0 = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)


class Store:
    """Alerts on paper-only techniques and missed runs, each with when it arrived.
    ``report(since)`` is identify_gaps' report over what arrived at or after since."""

    def __init__(self):
        self.alerts = []  # (at, technique)
        self.runs = []  # (at, technique, run_id, confirmed)
        self.blind = {}  # data source -> rules starved
        self.during_run = []  # alerts that arrive while the report is being read
        self.calls = []

    def report(self, since):
        new_alerts = [a for a in self.alerts if since is None or a[0] >= since]
        counts = {}
        for _at, technique in new_alerts:
            counts[technique] = counts.get(technique, 0) + 1
        groups = {}
        for at, technique, run_id, confirmed in self.runs:
            if since is None or at >= since:
                g = groups.setdefault(technique, {"runs": [], "confirmed": 0})
                g["runs"].append({"kind": "hunt", "id": run_id})
                g["confirmed"] += confirmed
        listed = [
            {"technique": t, **g}
            for t, g in groups.items()
            if len(g["runs"]) >= 2 or g["confirmed"] >= 1
        ]
        return {
            "runs_rules_missed": {"groups": listed},
            "covered_on_paper_only": sorted(counts),
            "techniques": [{"technique": t, "alerts": n} for t, n in counts.items()],
            "blind_spots": [
                {"data_source": s, "rules": [{"rule_id": i} for i in range(n)]}
                for s, n in self.blind.items()
            ],
        }


@pytest.fixture
def run(monkeypatch):
    """One gaps run at a given time, against a Store: returns what it queued."""
    store = Store()
    saved = {}
    covered = set()
    queued = []
    clock = {"now": T0}

    class Tools:
        async def identify_gaps(self, since_days=30, since=None, **_):
            store.calls.append({"since_days": since_days, "since": since})
            report = store.report(since)
            store.alerts += [(clock["now"] + timedelta(minutes=5), t) for t in store.during_run]
            store.during_run = []
            return report

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(scheduler, "datetime", Clock)
    monkeypatch.setattr(scheduler, "is_enabled", lambda _w: True)
    monkeypatch.setattr(
        "core.detections.tools.get_security_detection_tools", lambda: Tools()
    )
    monkeypatch.setattr(
        "core.integrations.mcp.client.process_mcp_client", lambda: None
    )
    monkeypatch.setattr(gap_run, "read_marker", lambda now: saved.get("m") or gap_run.Marker(now - timedelta(days=gap_run.FIRST_RUN_DAYS)))
    monkeypatch.setattr(gap_run, "write_marker", lambda m: saved.__setitem__("m", m))
    monkeypatch.setattr(gap_run, "open_case_techniques", lambda: set(covered))

    def insert(**kwargs):
        if store.fail_queue:
            raise RuntimeError("intake is down")
        queued.append(kwargs["payload"])
        return 1

    store.fail_queue = False
    monkeypatch.setattr(orchestrator, "insert_intake_trigger", insert)

    def go(at):
        clock["now"] = at
        before = len(queued)
        asyncio.run(scheduler.TaskScheduler._run_detection_gaps(None))
        return queued[before:]

    go.store, go.saved, go.covered = store, saved, covered
    return go


def test_back_to_back_runs_raise_nothing_the_second_time(run):
    run.store.alerts += [(T0 - timedelta(hours=h), "T1059.001") for h in (1, 2, 3)]
    run.store.runs += [(T0 - timedelta(hours=h), "T1003", f"h{h}", 0) for h in (4, 5)]
    run.store.blind["winlogbeat-*"] = 3
    (first,) = run(T0)
    assert "T1059.001" in first["hypothesis"] and "T1003" in first["hypothesis"]
    assert "winlogbeat-*" in first["hypothesis"]
    assert run(T0 + timedelta(days=1)) == []


def test_a_below_floor_gap_is_not_raised(run):
    assert gap_run.GAP_FLOOR == 2
    run.store.alerts += [(T0 - timedelta(hours=1), "T1105")]
    run.store.runs += [(T0 - timedelta(hours=2), "T1021", "h1", 0)]
    run.store.blind["auditd"] = 1
    run.store.alerts += [(T0 - timedelta(hours=h), "T1059.001") for h in (1, 2)]
    (queued,) = run(T0)
    assert "T1059.001" in queued["hypothesis"]
    for below in ("T1105", "T1021", "auditd"):
        assert below not in queued["hypothesis"]


def test_one_confirmed_missed_run_is_enough(run):
    run.store.runs += [(T0 - timedelta(hours=2), "T1021", "h1", 1)]
    (queued,) = run(T0)
    assert "T1021 runs no rule caught (1 run, 1 confirmed)" in queued["hypothesis"]


def test_gaps_reach_the_agent_ranked_by_count_with_each_count(run):
    run.store.alerts += [(T0 - timedelta(minutes=m), "T1059.001") for m in range(2)]
    run.store.alerts += [(T0 - timedelta(minutes=m), "T1105") for m in range(7)]
    run.store.runs += [(T0 - timedelta(hours=h), "T1003", f"h{h}", 0) for h in range(4)]
    run.store.blind["winlogbeat-*"] = 3
    (queued,) = run(T0)
    listed = queued["hypothesis"].split("New gaps, biggest first: ")[1].split("; ")
    assert listed == [
        "T1105 covered on paper only (7 alerts)",
        "T1003 runs no rule caught (4 runs)",
        "winlogbeat-* blind spot (data source) (3 rules starved)",
        "T1059.001 covered on paper only (2 alerts)",
    ]


def test_a_gap_an_open_case_carries_is_not_raised_again(run):
    run.covered.add("T1003")
    run.store.runs += [(T0 - timedelta(hours=h), "T1003", f"h{h}", 0) for h in (1, 2)]
    run.store.alerts += [(T0 - timedelta(hours=h), "T1105") for h in (1, 2)]
    (queued,) = run(T0)
    assert "T1003" not in queued["hypothesis"]
    assert queued["case_techniques"] == ["T1105"]


def test_new_evidence_after_the_marker_raises_again(run):
    run.store.alerts += [(T0 - timedelta(hours=h), "T1105") for h in (1, 2)]
    run(T0)
    run.store.alerts += [(T0 + timedelta(hours=h), "T1105") for h in (1, 2, 3)]
    (queued,) = run(T0 + timedelta(days=1))
    assert "T1105 covered on paper only (3 alerts)" in queued["hypothesis"]


def test_the_marker_moves_to_the_runs_start_so_alerts_during_the_run_count_next_time(run):
    run.store.alerts += [(T0 - timedelta(hours=1), "T1105")] * 2
    run.store.during_run = ["T1566", "T1566"]
    run(T0)
    assert run.saved["m"].since == T0
    assert run.store.calls[-1]["since"] == T0 - timedelta(days=gap_run.FIRST_RUN_DAYS)
    (queued,) = run(T0 + timedelta(days=1))
    assert run.store.calls[-1]["since"] == T0
    assert "T1566 covered on paper only (2 alerts)" in queued["hypothesis"]


def test_a_failed_queue_leaves_the_marker(run):
    run.store.alerts += [(T0 - timedelta(hours=1), "T1105")] * 2
    run.store.fail_queue = True
    with pytest.raises(RuntimeError):
        run(T0)
    assert "m" not in run.saved
    run.store.fail_queue = False
    (queued,) = run(T0 + timedelta(hours=1))
    assert "T1105" in queued["hypothesis"]


def test_a_run_with_nothing_to_raise_still_moves_the_marker(run):
    assert run(T0) == []
    assert run.saved["m"].since == T0


def test_a_long_gap_since_the_marker_widens_the_ledger_window(run):
    run(T0)
    run(T0 + timedelta(days=45))
    assert run.store.calls[-1]["since_days"] == 46


def test_a_blind_spot_is_raised_again_only_after_it_cleared():
    marker = gap_run.Marker(T0)
    spot = [gap_run.Gap(gap_run.BLIND, "auditd", 2)]
    raised = gap_run.to_raise(spot, marker, set())
    marker = gap_run.next_marker(T0, marker, spot, raised)
    assert gap_run.to_raise(spot, marker, set()) == []
    marker = gap_run.next_marker(T0, marker, [], [])  # it cleared
    assert gap_run.to_raise(spot, marker, set()) == spot


def _verdict(run_id, concluded_at):
    return {
        "investigation_kind": "hunt",
        "investigation_id": run_id,
        "hypothesis_id": "h",
        "outcome": "proven",
        "trust": "agent",
        "techniques": {"T1003"},
        "subjects": [],
        "entities": {},
        "first_seen": None,
        "last_seen": None,
        "concluded_at": concluded_at,
    }


def test_identify_gaps_since_reconstructs_only_newer_proven_runs():
    seen = []

    def read_findings(start, end):
        seen.append((start, end))
        return []

    verdicts = [_verdict("old", T0 - timedelta(days=2)), _verdict("new", T0 + timedelta(hours=1))]
    report = missed_runs_report("", False, verdicts, read_findings, {}, 30, since=T0)
    assert report["proven_verdicts"] == 1


def test_the_schedule_intake_mints_its_case_with_the_techniques(monkeypatch):
    seen = {}

    async def capture(self, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(orchestrator.Orchestrator, "_create_investigation", capture, raising=False)
    item = {
        "workflow_id": "agent-run-mitre_analyst",
        "finding_ids": [],
        "hypothesis": "Turn the detection gaps into Cases.",
        "case_techniques": ["T1003", "T1105"],
    }
    cls = orchestrator.Orchestrator
    asyncio.run(cls._create_manual_investigation(object.__new__(cls), item, asyncio.Event()))
    assert seen["mint_case"].mitre_techniques == ["T1003", "T1105"]


def test_a_minted_case_stores_its_techniques():
    case = _mint_with(orchestrator.CaseSpec(title="t", finding_ids=[], priority="low", mitre_techniques=["T1003"]))
    assert case.mitre_techniques == ["T1003"]
    assert _mint_with(orchestrator.CaseSpec(title="t", finding_ids=[], priority="low")).mitre_techniques is None


def _mint_with(spec):
    added = []

    class Session:
        def add(self, row):
            added.append(row)

        def flush(self):
            pass

    orchestrator._mint_case(Session(), spec)
    return added[0]
