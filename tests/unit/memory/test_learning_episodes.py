"""Learning episodes (#906): a window over Distil markers, and a JSONL export.

Real Postgres, seeded the way the Distil writes: a marker per investigation and
its Verdicts and Gaps. The read is a tuple-IN over two tables and a timestamptz
window, which a fake would only agree with by construction.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.agents.tool_registry import _MEMORY_TOOLS, execute_backend_tool
from core.llm.tool_schemas import ALL_TOOLS
from core.workflows.workflow_run_service import LIST_RUNS_MAX
from core.memory.learning_episodes import (
    export_learning_episodes,
    list_learning_episodes,
)
from core.storage.models import (
    EpisodicDistilMarker,
    EpisodicGap,
    EpisodicVerdict,
    EpisodicVerdictSource,
)

pytestmark = [pytest.mark.unit, pytest.mark.database, pytest.mark.external_service]

EPOCH = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
IP = "ip:10.0.0.7"
HOST = "host:dc01"


def marker(db, kind, ident, *, concluded, run=None):
    db.add(
        EpisodicDistilMarker(
            investigation_kind=kind,
            investigation_id=ident,
            origin_run_id=run,
            origin_seq=1 if run else None,
            origin_run_ids=[run] if run else [],
            distil_version=1,
            concluded_at=concluded,
        )
    )


def verdict(db, kind, ident, keys, *, concluded, sources=()):
    row = EpisodicVerdict(
        investigation_kind=kind,
        investigation_id=ident,
        hypothesis_id=f"h-{ident}",
        statement="beaconing to a known-bad host",
        outcome="proven",
        rationale="regular interval",
        subject_entities=list(keys),
        attacker_influenceable_only=False,
        trust="agent",
        first_seen=concluded - timedelta(hours=1),
        last_seen=concluded,
        window_source="observed",
        concluded_at=concluded,
    )
    db.add(row)
    db.flush()
    for system, stance in sources:
        db.add(
            EpisodicVerdictSource(
                verdict_id=row.id,
                source_system=system,
                stance=stance,
                source_tier="telemetry",
            )
        )


def gap(db, kind, ident, keys, *, concluded):
    db.add(
        EpisodicGap(
            investigation_kind=kind,
            investigation_id=ident,
            hypothesis_id=f"g-{ident}",
            statement="did anything else talk to it",
            disposition="budget_exhausted",
            reason="out of turns",
            subject_entities=list(keys),
            concluded_at=concluded,
        )
    )


@pytest.fixture
def seeded(episodic_session, monkeypatch, tmp_path):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    db = episodic_session
    run = uuid.uuid4()
    marker(db, "hunt", "hunt-a", concluded=EPOCH, run=run)
    verdict(
        db,
        "hunt",
        "hunt-a",
        [IP, HOST],
        concluded=EPOCH,
        sources=[("splunk", "supports"), ("crowdstrike", "weakens")],
    )
    gap(db, "hunt", "hunt-a", [IP], concluded=EPOCH)
    # A Case: no run behind it, and its Verdict names no entity at all.
    marker(db, "case", "case-1", concluded=EPOCH + timedelta(days=1))
    verdict(db, "case", "case-1", [], concluded=EPOCH + timedelta(days=1))
    # Concluded nothing, and still an episode.
    marker(
        db, "hunt", "hunt-empty", concluded=EPOCH + timedelta(days=2), run=uuid.uuid4()
    )
    # Outside the window, and an analyst marker inside it: neither is listed.
    marker(
        db, "hunt", "hunt-old", concluded=EPOCH - timedelta(days=30), run=uuid.uuid4()
    )
    marker(db, "analyst", "note-1", concluded=EPOCH)
    db.commit()
    return {"run": str(run), "exports": tmp_path / "exports"}


async def window(**extra):
    return await list_learning_episodes(
        {"start": "2026-08-01", "end": "2026-08-31", **extra}
    )


async def test_lists_hunt_and_case_episodes_in_the_window(seeded):
    result = await window()

    assert result["total"] == 3 and result["dropped"] == 0
    keys = [(e["kind"], e["investigation_id"]) for e in result["episodes"]]
    assert keys == [("hunt", "hunt-empty"), ("case", "case-1"), ("hunt", "hunt-a")]

    by_id = {e["investigation_id"]: e for e in result["episodes"]}
    hunt = by_id["hunt-a"]
    assert hunt["origin_run_id"] == seeded["run"]
    assert hunt["concluded_at"] == "2026-08-10T12:00:00Z"
    assert len(hunt["payload"]["verdicts"]) == 1 and len(hunt["payload"]["gaps"]) == 1
    stances = {
        s["source_system"]: s["stance"]
        for s in hunt["payload"]["verdicts"][0]["sources"]
    }
    assert stances == {"splunk": "supports", "crowdstrike": "weakens"}
    assert by_id["case-1"]["origin_run_id"] is None
    assert by_id["hunt-empty"]["payload"] == {"verdicts": [], "gaps": []}


async def test_window_bounds_are_inclusive_and_the_limit_reports_what_it_dropped(
    seeded,
):
    only_day = await list_learning_episodes(
        {"start": "2026-08-10T12:00:00Z", "end": "2026-08-10T12:00:00Z"}
    )
    assert [e["investigation_id"] for e in only_day["episodes"]] == ["hunt-a"]

    # A date-only end is the whole of that day: hunt-empty concluded at noon.
    to_date = await list_learning_episodes({"start": "2026-08-12", "end": "2026-08-12"})
    assert [e["investigation_id"] for e in to_date["episodes"]] == ["hunt-empty"]

    page = await window(limit=1)
    assert page["total"] == 3 and page["dropped"] == 2
    assert [e["investigation_id"] for e in page["episodes"]] == ["hunt-empty"]

    with pytest.raises(TypeError):
        await list_learning_episodes({"start": "2026-09-01", "end": "2026-08-01"})


async def test_empty_selection_writes_no_file(seeded):
    result = await export_learning_episodes({"episodes": [], "limit": 5})
    assert result == {"path": None, "written": 0, "identified": False, "missing": []}
    assert not seeded["exports"].exists()

    nothing = await export_learning_episodes(
        {"episodes": [{"kind": "case", "investigation_id": "hunt-a"}]}
    )
    assert nothing["path"] is None and nothing["written"] == 0
    assert nothing["missing"] == [{"kind": "case", "investigation_id": "hunt-a"}]
    assert not seeded["exports"].exists()


async def test_subset_export_is_redacted_by_default_and_only_the_selection(seeded):
    result = await export_learning_episodes(
        {
            "episodes": [
                {"kind": "case", "investigation_id": "case-1"},
                {"kind": "hunt", "investigation_id": "hunt-a"},
                {"kind": "hunt", "investigation_id": "not-here"},
            ],
            "name": "august",
        }
    )
    assert result["written"] == 2
    assert result["missing"] == [{"kind": "hunt", "investigation_id": "not-here"}]
    path = seeded["exports"] / "august.jsonl"
    assert result["path"] == str(path)

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(e["kind"], e["investigation_id"]) for e in lines] == [
        ("case", "case-1"),
        ("hunt", "hunt-a"),
    ]
    hunt = lines[1]
    assert hunt["payload"]["verdicts"][0]["subject_entities"] == ["ip:*", "host:*"]
    assert hunt["payload"]["gaps"][0]["subject_entities"] == ["ip:*"]
    assert (
        hunt["payload"]["verdicts"][0]["statement"] == "beaconing to a known-bad host"
    )
    assert hunt["payload"]["verdicts"][0]["outcome"] == "proven"
    assert len(hunt["payload"]["verdicts"][0]["sources"]) == 2
    # The entity-free Case verdict exports as-is.
    assert lines[0]["payload"]["verdicts"][0]["subject_entities"] == []
    assert lines[0]["origin_run_id"] is None
    assert "10.0.0.7" not in path.read_text()


async def test_identified_export_keeps_entity_values(seeded):
    result = await export_learning_episodes(
        {
            "episodes": [{"kind": "hunt", "investigation_id": "hunt-a"}],
            "identified": True,
            "name": "../named",
        }
    )
    path = seeded["exports"] / "named.jsonl"
    assert result["path"] == str(path) and result["identified"] is True
    line = json.loads(path.read_text())
    assert line["payload"]["verdicts"][0]["subject_entities"] == [IP, HOST]
    assert line["payload"]["gaps"][0]["subject_entities"] == [IP]


def test_registered_as_memory_tools_and_granted_to_the_reporter_only():
    names = {tool["name"] for tool in ALL_TOOLS}
    assert {"list_learning_episodes", "export_learning_episodes"} <= names
    assert {"list_learning_episodes", "export_learning_episodes"} <= set(_MEMORY_TOOLS)
    granted = {
        agent["id"]
        for agent in BUILTIN_AGENTS
        if "list_learning_episodes" in agent["recommended_tools"]
        or "export_learning_episodes" in agent["recommended_tools"]
    }
    assert granted == {"reporter"}
    export = next(
        tool for tool in ALL_TOOLS if tool["name"] == "export_learning_episodes"
    )
    kind = export["input_schema"]["properties"]["episodes"]["items"]["properties"][
        "kind"
    ]
    assert kind["enum"] == ["hunt", "case", "emulation"]
    listed = next(
        tool for tool in ALL_TOOLS if tool["name"] == "list_learning_episodes"
    )
    assert "execute trace" in listed["description"]
    assert "execute trace" in export["description"]


FIXTURE = (
    Path(__file__).parents[1] / "detections" / "fixtures" / "recorded_red_run.json"
)
EVIDENCE = ("hostname", "host", "computer_name", "src_ip", "user", "command")


def _recorded_trace():
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    steps = [dict(step) for step in recorded["steps"]]
    # Already missed (no Finding on dc01). String evidence is what export strips.
    steps[1].update(
        {
            "host": "dc01",
            "computer_name": "DC01",
            "src_ip": "10.8.8.8",
            "user": "nobody",
            "command": "sekurlsa::logonpasswords",
        }
    )
    return steps, recorded["findings"]


def _execute_result(steps):
    return {"ok": True, "rows": steps, "rowCount": len(steps), "capped": False}


def _run(run_id, finished, *, status="completed", trace=None, projection=None):
    if projection is None and trace is not None:
        projection = {"run_id": run_id, "results": [_execute_result(trace)]}
    return {
        "run_id": run_id,
        "status": status,
        "trigger_context": {"run_kind": "compose"},
        "finished_at": finished,
        "projection": projection,
    }


class _Runs:
    def __init__(self, runs):
        self.runs = {run["run_id"]: run for run in runs}
        self.calls = []

    def list_runs(self, **kwargs):
        self.calls.append(kwargs)
        return [
            run
            for run in self.runs.values()
            if run["status"] == kwargs.get("status")
            and (run.get("trigger_context") or {}).get("run_kind")
            == kwargs.get("run_kind")
        ]

    def get_run(self, run_id):
        return self.runs.get(run_id)


def _install(monkeypatch, runs):
    holder = _Runs(runs)
    monkeypatch.setattr(
        "core.memory.learning_episodes.WorkflowRunService", lambda: holder
    )
    reads = []

    async def _read(run_id):
        reads.append(run_id)
        run = holder.runs.get(run_id)
        return None if run is None else run.get("projection")

    monkeypatch.setattr("core.memory.learning_episodes.read_projection", _read)
    steps, findings = _recorded_trace()

    class _Store:
        def get_findings(self, **_kwargs):
            return findings

    monkeypatch.setattr("core.detections.tools.DatabaseDataService", lambda: _Store())
    return holder, reads, steps


async def test_compose_trace_lists_and_exports_as_emulation(monkeypatch, tmp_path):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    steps, _findings = _recorded_trace()
    finished = datetime(2026, 9, 10, 13, tzinfo=timezone.utc)
    kept = _run("wfr-red", finished, trace=steps)
    empty = _run(
        "wfr-empty",
        finished,
        projection={"run_id": "wfr-empty", "results": []},
    )
    unread = _run("wfr-none", finished)
    holder, reads, _steps = _install(monkeypatch, [kept, empty, unread])

    result, handled = await execute_backend_tool(
        "list_learning_episodes",
        {"start": "2026-09-01", "end": "2026-09-30"},
    )
    assert handled is True
    assert [call["status"] for call in holder.calls] == ["completed", "failed"]
    assert {call["run_kind"] for call in holder.calls} == {"compose"}
    assert holder.calls[0]["limit"] == LIST_RUNS_MAX
    assert holder.calls[0]["finished_after"] == datetime(
        2026, 9, 1, tzinfo=timezone.utc
    )
    assert holder.calls[0]["finished_at"] == datetime(
        2026, 9, 30, 23, 59, 59, 999999, tzinfo=timezone.utc
    )
    assert reads == ["wfr-red", "wfr-empty", "wfr-none"]
    assert result["total"] == 1 and result["dropped"] == 0
    episode = result["episodes"][0]
    assert episode["kind"] == "emulation"
    assert episode["investigation_id"] == episode["origin_run_id"] == "wfr-red"
    assert episode["concluded_at"] == "2026-09-10T13:00:00Z"
    verdicts = {
        row["technique_id"]: row["verdict"] for row in episode["payload"]["techniques"]
    }
    assert verdicts == {
        "T1059.001": "both",
        "T1003.001": "missed",
        "T1021.002": "missed",
        "T1047": "loglm",
    }
    missed = next(
        row
        for row in episode["payload"]["techniques"]
        if row["technique_id"] == "T1003.001"
    )["missed"][0]
    assert missed["hostname"] == "dc01.corp.local"
    assert set(EVIDENCE) <= set(missed)

    exported, handled = await execute_backend_tool(
        "export_learning_episodes",
        {
            "episodes": [
                {"kind": "emulation", "investigation_id": "wfr-red"},
                {"kind": "emulation", "investigation_id": "wfr-empty"},
                {"kind": "emulation", "investigation_id": "wfr-none"},
                {"kind": "emulation", "investigation_id": "wfr-missing"},
            ],
            "name": "red",
        },
    )
    assert handled is True
    assert exported["written"] == 1
    assert exported["missing"] == [
        {"kind": "emulation", "investigation_id": "wfr-empty"},
        {"kind": "emulation", "investigation_id": "wfr-none"},
        {"kind": "emulation", "investigation_id": "wfr-missing"},
    ]
    # List read each candidate once. Export reads the kept, empty, and unread
    # runs again, and does not read an unknown id.
    assert reads == [
        "wfr-red",
        "wfr-empty",
        "wfr-none",
        "wfr-red",
        "wfr-empty",
        "wfr-none",
    ]
    line = json.loads((tmp_path / "exports" / "red.jsonl").read_text())
    assert line["kind"] == "emulation"
    exported_missed = next(
        row
        for row in line["payload"]["techniques"]
        if row["technique_id"] == "T1003.001"
    )["missed"][0]
    assert not set(EVIDENCE) & set(exported_missed)
    assert {
        row["technique_id"]: row["verdict"] for row in line["payload"]["techniques"]
    } == verdicts
    both = next(
        row
        for row in line["payload"]["techniques"]
        if row["technique_id"] == "T1059.001"
    )
    cited = {item["finding_id"]: item for item in both["steps"][0]["citations"]}
    assert cited["elastic-enc-ps"]["description"] == "Encoded PowerShell command line"
    assert cited["elastic-enc-ps"]["rule_name"] == "Encoded PowerShell"
    assert "dc01.corp.local" not in (tmp_path / "exports" / "red.jsonl").read_text()
    assert "sekurlsa" not in (tmp_path / "exports" / "red.jsonl").read_text()


async def test_identified_emulation_export_keeps_missed_step_evidence(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    steps, _findings = _recorded_trace()
    finished = datetime(2026, 9, 10, 13, tzinfo=timezone.utc)
    _install(monkeypatch, [_run("wfr-red", finished=finished, trace=steps)])

    exported, handled = await execute_backend_tool(
        "export_learning_episodes",
        {
            "episodes": [{"kind": "emulation", "investigation_id": "wfr-red"}],
            "identified": True,
            "name": "named",
        },
    )
    assert handled is True and exported["identified"] is True
    line = json.loads((tmp_path / "exports" / "named.jsonl").read_text())
    missed = next(
        row
        for row in line["payload"]["techniques"]
        if row["technique_id"] == "T1003.001"
    )["missed"][0]
    assert missed["hostname"] == "dc01.corp.local"
    assert missed["command"] == "sekurlsa::logonpasswords"
    assert missed["user"] == "nobody"


async def test_emulation_merges_with_hunt_and_case_newest_first(seeded, monkeypatch):
    steps, _findings = _recorded_trace()
    # list_runs returns finished_at the way dump_summary does: offset, and a
    # fraction when the instant is not a whole second. Half a second after
    # hunt-a must sort newer than that marker. The failed run is newer than
    # every marker, so a short page has to displace a hunt rather than append.
    frac = (EPOCH + timedelta(microseconds=500_000)).isoformat()
    holder, _reads, _steps = _install(
        monkeypatch,
        [
            _run("wfr-red", frac, trace=steps),
            _run("wfr-new", EPOCH + timedelta(days=3), status="failed", trace=steps),
        ],
    )

    result, handled = await execute_backend_tool(
        "list_learning_episodes",
        {"start": "2026-08-01", "end": "2026-08-31", "limit": 10},
    )
    assert handled is True
    assert [call["status"] for call in holder.calls] == ["completed", "failed"]
    keys = [(e["kind"], e["investigation_id"]) for e in result["episodes"]]
    assert keys == [
        ("emulation", "wfr-new"),
        ("hunt", "hunt-empty"),
        ("case", "case-1"),
        ("emulation", "wfr-red"),
        ("hunt", "hunt-a"),
    ]
    assert result["total"] == 5 and result["dropped"] == 0
    red = next(e for e in result["episodes"] if e["investigation_id"] == "wfr-red")
    assert red["concluded_at"] == "2026-08-10T12:00:00.500000Z"

    page, _handled = await execute_backend_tool(
        "list_learning_episodes",
        {"start": "2026-08-01", "end": "2026-08-31", "limit": 2},
    )
    assert [e["investigation_id"] for e in page["episodes"]] == [
        "wfr-new",
        "hunt-empty",
    ]
    assert page["total"] == 5 and page["dropped"] == 3
