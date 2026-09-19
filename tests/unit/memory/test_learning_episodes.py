"""Learning episodes (#906): a window over Distil markers, and a JSONL export.

Real Postgres, seeded the way the Distil writes: a marker per investigation and
its Verdicts and Gaps. The read is a tuple-IN over two tables and a timestamptz
window, which a fake would only agree with by construction.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.agents.builtins import BUILTIN_AGENTS
from core.agents.tool_registry import _MEMORY_TOOLS
from core.llm.tool_schemas import ALL_TOOLS
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


def window(**extra):
    return list_learning_episodes({"start": "2026-08-01", "end": "2026-08-31", **extra})


def test_lists_hunt_and_case_episodes_in_the_window(seeded):
    result = window()

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


def test_window_bounds_are_inclusive_and_the_limit_reports_what_it_dropped(seeded):
    only_day = list_learning_episodes(
        {"start": "2026-08-10T12:00:00Z", "end": "2026-08-10T12:00:00Z"}
    )
    assert [e["investigation_id"] for e in only_day["episodes"]] == ["hunt-a"]

    # A date-only end is the whole of that day: hunt-empty concluded at noon.
    to_date = list_learning_episodes({"start": "2026-08-12", "end": "2026-08-12"})
    assert [e["investigation_id"] for e in to_date["episodes"]] == ["hunt-empty"]

    page = window(limit=1)
    assert page["total"] == 3 and page["dropped"] == 2
    assert [e["investigation_id"] for e in page["episodes"]] == ["hunt-empty"]

    with pytest.raises(TypeError):
        list_learning_episodes({"start": "2026-09-01", "end": "2026-08-01"})


def test_empty_selection_writes_no_file(seeded):
    result = export_learning_episodes({"episodes": [], "limit": 5})
    assert result == {"path": None, "written": 0, "identified": False, "missing": []}
    assert not seeded["exports"].exists()

    nothing = export_learning_episodes(
        {"episodes": [{"kind": "case", "investigation_id": "hunt-a"}]}
    )
    assert nothing["path"] is None and nothing["written"] == 0
    assert nothing["missing"] == [{"kind": "case", "investigation_id": "hunt-a"}]
    assert not seeded["exports"].exists()


def test_subset_export_is_redacted_by_default_and_only_the_selection(seeded):
    result = export_learning_episodes(
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


def test_identified_export_keeps_entity_values(seeded):
    result = export_learning_episodes(
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
