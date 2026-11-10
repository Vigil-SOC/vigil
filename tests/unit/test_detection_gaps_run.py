"""The daily detection gaps run reads a rolling window, counts only evidence (missed
runs, alerts) no earlier run raised, raises a gap only at or above a floor, ranks what
it raises by count, and does not raise a gap an open Case already carries. It
remembers what it raised only once the run is queued. Evidence that commits late, is
labelled late, or reaches the floor over several days is still raised."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core.detections import gap_run
from core.detections.gaps import missed_runs_report
from services.daemon import orchestrator, scheduler

T0 = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


class Store:
    """Alerts and missed runs with when they were stored, labelled or concluded.
    ``report(since)`` is identify_gaps' report over the window, as the run reads it."""

    def __init__(self):
        self.alerts = []  # dicts: id, at, technique (None: not labelled yet), visible
        self.runs = []  # (at, technique, run_id, confirmed)
        self.blind = {}
        self.calls = []

    def alert(self, technique, at, visible=True):
        a = {"id": f"f{len(self.alerts)}", "at": at, "technique": technique, "visible": visible}
        self.alerts.append(a)
        return a

    def _labelled(self, since):
        return [a for a in self.alerts if a["visible"] and a["technique"] and a["at"] >= since]

    def alert_ids(self, since):
        out = {}
        for a in self._labelled(since):
            out.setdefault(a["technique"], []).append(a["id"])
        return out

    def report(self, since):
        counts = {}
        for a in self._labelled(since):
            counts[a["technique"]] = counts.get(a["technique"], 0) + 1
        groups = {}
        for at, technique, run_id, confirmed in self.runs:
            if at >= since:
                g = groups.setdefault(technique, {"runs": [], "confirmed_runs": []})
                row = {"investigation_kind": "hunt", "investigation_id": run_id}
                g["runs"].append(row)
                if confirmed:
                    g["confirmed_runs"].append(row)
        listed = [
            {"technique": t, "confirmed": len(g["confirmed_runs"]), **g}
            for t, g in groups.items()
            if len(g["runs"]) >= 2 or g["confirmed_runs"]
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
    queue = {"result": 1, "fail": False}

    class Tools:
        async def identify_gaps(self, since_days=30, since=None, **_):
            store.calls.append({"since_days": since_days, "since": since})
            return store.report(since)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    monkeypatch.setattr(scheduler, "datetime", Clock)
    monkeypatch.setattr(scheduler, "is_enabled", lambda _w: True)
    monkeypatch.setattr("core.detections.tools.get_security_detection_tools", lambda: Tools())
    monkeypatch.setattr("core.integrations.mcp.client.process_mcp_client", lambda: None)
    monkeypatch.setattr(gap_run, "read_raised", lambda: saved.get("r") or gap_run.Raised())
    monkeypatch.setattr(gap_run, "write_raised", lambda r: saved.__setitem__("r", r))
    monkeypatch.setattr(gap_run, "open_case_techniques", lambda: set(covered))
    monkeypatch.setattr(gap_run, "alert_ids", store.alert_ids)

    def insert(**kwargs):
        if queue["fail"]:
            raise RuntimeError("intake is down")
        if queue["result"] is None:
            return None
        queued.append(kwargs["payload"])
        return queue["result"]

    monkeypatch.setattr(orchestrator, "insert_intake_trigger", insert)

    def go(at):
        clock["now"] = at
        before = len(queued)
        asyncio.run(scheduler.TaskScheduler._run_detection_gaps(None))
        return queued[before:]

    go.store, go.saved, go.covered, go.queue = store, saved, covered, queue
    return go


def _gaps(payload):
    return payload["hypothesis"].split("New gaps, biggest first: ")[1].split("; ")


def test_back_to_back_runs_raise_nothing_the_second_time(run):
    for h in (1, 2, 3):
        run.store.alert("T1059.001", T0 - timedelta(hours=h))
    run.store.runs += [(T0 - timedelta(hours=h), "T1003", f"h{h}", 0) for h in (4, 5)]
    run.store.blind["winlogbeat-*"] = 3
    (first,) = run(T0)
    assert "T1059.001" in first["hypothesis"] and "T1003" in first["hypothesis"]
    assert "winlogbeat-*" in first["hypothesis"]
    assert run(T0 + timedelta(hours=1)) == []
    assert run(T0 + DAY) == []


def test_a_below_floor_gap_is_not_raised(run):
    assert gap_run.GAP_FLOOR == 2
    run.store.alert("T1105", T0 - timedelta(hours=1))
    run.store.runs += [(T0 - timedelta(hours=2), "T1021", "h1", 0)]
    for h in (1, 2):
        run.store.alert("T1059.001", T0 - timedelta(hours=h))
    (queued,) = run(T0)
    assert "T1059.001" in queued["hypothesis"]
    for below in ("T1105", "T1021"):
        assert below not in queued["hypothesis"]


def test_one_a_day_reaches_the_floor_on_the_second_day(run):
    """Sub-floor sightings are not forgotten: they count again the next day."""
    run.store.alert("T1105", T0 - timedelta(hours=1))
    run.store.runs += [(T0 - timedelta(hours=1), "T1021", "h1", 0)]
    assert run(T0) == []
    run.store.alert("T1105", T0 + DAY - timedelta(hours=1))
    run.store.runs += [(T0 + DAY - timedelta(hours=1), "T1021", "h2", 0)]
    (queued,) = run(T0 + DAY)
    assert sorted(_gaps(queued)) == [
        "T1021 runs no rule caught (2 new runs)",
        "T1105 covered on paper only (2 new alerts)",
    ]


def test_an_alert_committed_after_the_run_read_is_raised_next_time(run):
    """Stored (created_at stamped) before the run, committed only after it read."""
    late = [run.store.alert("T1566", T0 - timedelta(minutes=5), visible=False) for _ in range(2)]
    assert run(T0) == []
    for a in late:
        a["visible"] = True
    (queued,) = run(T0 + DAY)
    assert _gaps(queued) == ["T1566 covered on paper only (2 new alerts)"]


def test_an_alert_labelled_after_it_was_stored_is_raised_next_time(run):
    unlabelled = [run.store.alert(None, T0 - timedelta(hours=3)) for _ in range(2)]
    assert run(T0) == []
    for a in unlabelled:
        a["technique"] = "T1218.011"  # labelled later (update_finding)
    (queued,) = run(T0 + DAY)
    assert _gaps(queued) == ["T1218.011 covered on paper only (2 new alerts)"]


def test_one_confirmed_missed_run_is_enough(run):
    run.store.runs += [(T0 - timedelta(hours=2), "T1021", "h1", 1)]
    (queued,) = run(T0)
    assert _gaps(queued) == ["T1021 runs no rule caught (1 new run, 1 confirmed)"]


def test_gaps_reach_the_agent_ranked_by_count_with_each_count(run):
    for m in range(2):
        run.store.alert("T1059.001", T0 - timedelta(minutes=m + 1))
    for m in range(7):
        run.store.alert("T1105", T0 - timedelta(minutes=m + 1))
    run.store.runs += [(T0 - timedelta(hours=h + 1), "T1003", f"h{h}", 0) for h in range(4)]
    run.store.blind["winlogbeat-*"] = 3
    (queued,) = run(T0)
    assert _gaps(queued) == [
        "T1105 covered on paper only (7 new alerts)",
        "T1003 runs no rule caught (4 new runs)",
        "winlogbeat-* blind spot (data source) (3 rules starved)",
        "T1059.001 covered on paper only (2 new alerts)",
    ]


def test_new_evidence_on_a_raised_gap_counts_only_what_is_new(run):
    for h in (1, 2):
        run.store.alert("T1105", T0 - timedelta(hours=h))
    run(T0)
    run.store.alert("T1105", T0 + timedelta(hours=1))
    assert run(T0 + DAY) == []  # one new alert: below the floor
    for h in (2, 3):
        run.store.alert("T1105", T0 + timedelta(hours=h))
    (queued,) = run(T0 + 2 * DAY)
    assert _gaps(queued) == ["T1105 covered on paper only (3 new alerts)"]


def test_a_gap_an_open_case_carries_is_not_raised_and_its_evidence_is_covered(run):
    run.covered.add("T1003")
    run.store.runs += [(T0 - timedelta(hours=h), "T1003", f"h{h}", 0) for h in (1, 2)]
    for h in (1, 2):
        run.store.alert("T1105", T0 - timedelta(hours=h))
    (queued,) = run(T0)
    assert "T1003" not in queued["hypothesis"]
    assert queued["case_techniques"] == ["T1105"]
    # The Case closes: what it covered is not raised again, new runs are.
    run.covered.clear()
    assert run(T0 + DAY) == []
    run.store.runs += [(T0 + DAY + timedelta(hours=h), "T1003", f"n{h}", 0) for h in (1, 2)]
    (again,) = run(T0 + 2 * DAY)
    assert _gaps(again) == ["T1003 runs no rule caught (2 new runs)"]


def test_a_failed_queue_remembers_nothing(run):
    for h in (1, 2):
        run.store.alert("T1105", T0 - timedelta(hours=h))
    run.queue["fail"] = True
    with pytest.raises(RuntimeError):
        run(T0)
    assert "r" not in run.saved
    run.queue["fail"] = False
    (queued,) = run(T0 + timedelta(hours=1))
    assert "T1105" in queued["hypothesis"]


def test_an_intake_row_not_queued_remembers_nothing(run):
    for h in (1, 2):
        run.store.alert("T1105", T0 - timedelta(hours=h))
    run.queue["result"] = None
    assert run(T0) == []
    assert "r" not in run.saved
    run.queue["result"] = 7
    (queued,) = run(T0 + timedelta(hours=1))
    assert "T1105" in queued["hypothesis"]


def test_each_run_reads_the_window(run):
    run(T0)
    run(T0 + 45 * DAY)
    assert run.store.calls[-1] == {
        "since_days": gap_run.WINDOW_DAYS,
        "since": T0 + 45 * DAY - timedelta(days=gap_run.WINDOW_DAYS),
    }


def test_what_was_raised_before_the_window_is_forgotten(run):
    for h in (1, 2):
        run.store.alert("T1105", T0 - timedelta(hours=h))
    run(T0)
    assert len(run.saved["r"].alerts) == 2
    run(T0 + (gap_run.WINDOW_DAYS + 1) * DAY)
    assert run.saved["r"].alerts == {}


def test_a_blind_spot_starving_one_rule_is_raised_once_until_it_clears(run):
    run.store.blind["auditd"] = 1
    (queued,) = run(T0)
    assert _gaps(queued) == ["auditd blind spot (data source) (1 rule starved)"]
    assert run(T0 + DAY) == []
    del run.store.blind["auditd"]
    assert run(T0 + 2 * DAY) == []
    run.store.blind["auditd"] = 1
    (again,) = run(T0 + 3 * DAY)
    assert "auditd" in again["hypothesis"]


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


def test_a_hunt_escalated_to_incident_response_counts_as_proven():
    escalated = {**_verdict("escalated", T0), "outcome": "handed_off"}
    report = missed_runs_report("", False, [escalated], lambda start, end: [], {}, 30)
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
