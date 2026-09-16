"""Listing prior hunts by entity key and technique (#899), against real Postgres.

The overlap operators and the marker join are the thing under test, so the
concluded rows are seeded the way the Distil writes them. In-flight rows go
through ``WorkflowRunService`` unchanged; only ``list_runs`` is called.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from core.memory import prior_hunts, recall
from core.memory.prior_hunts import list_prior_hunts
from core.storage.models import (
    EpisodicDistilMarker,
    EpisodicReadLog,
    EpisodicVerdict,
    WorkflowRun,
)
from core.workflows import workflow_run_service
from core.workflows.workflow_run_service import WorkflowRunService

pytestmark = [pytest.mark.unit, pytest.mark.database, pytest.mark.external_service]

EPOCH = datetime(2026, 8, 1, tzinfo=timezone.utc)
ORIGIN = UUID("11111111-2222-3333-4444-555555555555")


@pytest.fixture
def session(episodic_session, monkeypatch):
    # The listing is a separate read from recall (epic #886, decision 3); any
    # route back through it would inherit the per-key cap this exists to avoid.
    # Patching alone is not proof -- a name bound at import escapes it -- so the
    # module's globals and the read log are checked too, below.
    def forbidden(*args, **kwargs):
        raise AssertionError("recall_entity was called")

    monkeypatch.setattr(recall, "recall_entity", forbidden)
    monkeypatch.setattr(recall, "recall", forbidden)
    episodic_session.query(EpisodicDistilMarker).delete()
    episodic_session.query(WorkflowRun).delete()
    episodic_session.commit()
    yield episodic_session
    assert not any(
        getattr(value, "__module__", None) == recall.__name__
        for value in vars(prior_hunts).values()
    ), "prior_hunts imports from recall"
    assert episodic_session.query(EpisodicReadLog).count() == 0


def verdict(db, keys, techniques=(), *, investigation, concluded=EPOCH, marker=None):
    db.add(
        EpisodicVerdict(
            investigation_kind="hunt",
            investigation_id=investigation,
            hypothesis_id=f"h-{investigation}",
            statement="beaconing to a known-bad host",
            outcome="proven",
            rationale="regular interval, low jitter",
            subject_entities=list(keys),
            techniques=list(techniques),
            attacker_influenceable_only=False,
            trust="agent",
            first_seen=concluded - timedelta(hours=2),
            last_seen=concluded,
            window_source="observed",
            concluded_at=concluded,
        )
    )
    if marker is not None:
        db.add(
            EpisodicDistilMarker(
                investigation_kind="hunt",
                investigation_id=investigation,
                origin_run_id=marker,
                origin_seq=1,
                origin_run_ids=[marker],
                distil_version=1,
                concluded_at=concluded,
            )
        )


def run(db, run_id, *, status="running", started=datetime(2026, 8, 2), **context):
    db.add(
        WorkflowRun(
            run_id=run_id,
            workflow_id="threat-hunt",
            workflow_name="Threat hunt",
            status=status,
            trigger_context={"run_kind": "hunt", **context},
            started_at=started,
        )
    )


def test_concluded_by_ip_carries_the_origin_run_and_needs_no_technique(session):
    verdict(
        session, ["ip:10.0.0.7"], ["T1071.001"], investigation="hunt-a", marker=ORIGIN
    )
    verdict(session, ["ip:10.0.0.8"], [], investigation="hunt-b")
    session.commit()

    result = list_prior_hunts(["IP:10.0.0.7"])

    assert result["keys"] == ["ip:10.0.0.7"]
    assert [row["investigation_id"] for row in result["concluded"]] == ["hunt-a"]
    row = result["concluded"][0]
    assert row["hypothesis_id"] == "h-hunt-a"
    assert row["outcome"] == "proven"
    assert row["concluded_at"] == "2026-08-01T00:00:00Z"
    assert row["origin_run_id"] == str(ORIGIN)
    assert row["matched_keys"] == ["ip:10.0.0.7"]
    assert row["matched_techniques"] == []
    assert result["in_flight"] == []


def test_concluded_by_technique_and_either_dimension_is_a_hit(session):
    verdict(session, ["host:dc01"], ["T1071.001"], investigation="by-technique")
    verdict(session, ["ip:10.0.0.9"], ["T1566"], investigation="by-key")
    verdict(session, ["host:web01"], ["T1566"], investigation="neither")
    session.commit()

    by_technique = list_prior_hunts([], ["t1071.001"])
    assert [r["investigation_id"] for r in by_technique["concluded"]] == [
        "by-technique"
    ]
    assert by_technique["concluded"][0]["matched_techniques"] == ["T1071.001"]
    # No marker written yet: the join is a LEFT one.
    assert by_technique["concluded"][0]["origin_run_id"] is None

    both = list_prior_hunts(["ip:10.0.0.9"], ["T1071.001"])
    assert sorted(r["investigation_id"] for r in both["concluded"]) == [
        "by-key",
        "by-technique",
    ]


def test_in_flight_matches_declared_subjects_and_hypothesis_text_only(session):
    run(
        session,
        "wfr-declared",
        hypothesis="Beaconing from 10.0.0.7 (T1071.001)",
        hypothesis_subjects={"Beaconing from 10.0.0.7 (T1071.001)": ["ip:10.0.0.7"]},
    )
    # Older, and paused: it must still sort by start time, not by status page.
    run(
        session,
        "wfr-paused",
        status="paused",
        started=datetime(2026, 8, 1),
        hypothesis="T1566 phishing wave",
    )
    # A scheduled hunt declares nothing, so nothing about it can match.
    run(session, "wfr-scheduled", hypothesis="", hypothesis_subjects={})
    # T1566 inside T15661 is a different id, not a hit.
    run(session, "wfr-near-miss", hypothesis="looks like T15661 to me")
    run(
        session,
        "wfr-done",
        status="completed",
        hypothesis_subjects={"x": ["ip:10.0.0.7"]},
    )
    session.commit()

    result = list_prior_hunts(["ip:10.0.0.7"], ["T1566"], runs=WorkflowRunService())

    assert [row["run_id"] for row in result["in_flight"]] == [
        "wfr-declared",
        "wfr-paused",
    ]
    by_id = {row["run_id"]: row for row in result["in_flight"]}
    assert by_id["wfr-declared"]["matched_keys"] == ["ip:10.0.0.7"]
    assert by_id["wfr-declared"]["matched_techniques"] == []
    assert by_id["wfr-declared"]["started_at"] == "2026-08-02T00:00:00Z"
    assert by_id["wfr-paused"]["status"] == "paused"
    assert by_id["wfr-paused"]["matched_techniques"] == ["T1566"]


def test_no_match_and_nothing_asked_are_empty_lists(session):
    verdict(session, ["ip:10.0.0.7"], ["T1071.001"], investigation="hunt-a")
    run(session, "wfr-a", hypothesis_subjects={"s": ["ip:10.0.0.7"]})
    session.commit()

    assert list_prior_hunts(["ip:203.0.113.9"], ["T9999"])["concluded"] == []
    assert list_prior_hunts(["ip:203.0.113.9"], ["T9999"])["in_flight"] == []
    assert list_prior_hunts([], ["not-a-technique"]) == {
        "keys": [],
        "techniques": [],
        "concluded": [],
        "in_flight": [],
    }


def test_concluded_rows_are_capped_newest_first(session, monkeypatch):
    # The runs listing's ceiling, not one of memory's own.
    assert prior_hunts.LIST_RUNS_MAX is workflow_run_service.LIST_RUNS_MAX
    monkeypatch.setattr(prior_hunts, "LIST_RUNS_MAX", 3)
    for index in range(5):
        verdict(
            session,
            ["ip:10.0.0.7"],
            investigation=f"hunt-{index}",
            concluded=EPOCH - timedelta(hours=index),
        )
    session.commit()

    rows = list_prior_hunts(["ip:10.0.0.7"])["concluded"]

    assert [r["investigation_id"] for r in rows] == ["hunt-0", "hunt-1", "hunt-2"]
