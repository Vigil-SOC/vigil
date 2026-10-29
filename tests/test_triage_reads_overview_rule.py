"""Triage reads Overview's rule for an alert's state (#3485).

Needs a real Postgres: set ``VIGIL_TEST_POSTGRES_DSN`` to an empty database.
"""

import os
from collections import Counter
from contextlib import contextmanager

import pytest

DSN = os.environ.get("VIGIL_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="VIGIL_TEST_POSTGRES_DSN not set")


@pytest.fixture
def seeded(monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from core.storage.models import (
        Base,
        Case,
        Finding,
        IntakeTrigger,
        case_findings,
    )
    from core.time import utcnow

    engine = create_engine(DSN)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    class Manager:
        @contextmanager
        def session_scope(self):
            session = factory()
            try:
                yield session
                session.commit()
            finally:
                session.close()

    manager = Manager()
    for module in (
        "core.findings.alert_outcomes",
        "core.findings.arrival_counts",
        "core.federation.lag",
        "services.api.triage_read",
    ):
        monkeypatch.setattr(f"{module}.get_db_manager", lambda: manager)
    monkeypatch.setattr("core.findings.alert_outcomes.needs_you_case_ids", set)
    monkeypatch.setattr("core.config.is_demo_mode", lambda: False)

    now = utcnow()
    with manager.session_scope() as session:
        for fid in ("f-queued", "f-oncase", "f-noise", "f-none", "f-expired"):
            session.add(
                Finding(
                    finding_id=fid,
                    data_source="splunk",
                    severity="high",
                    noise_marked_at=now if fid == "f-noise" else None,
                )
            )
        session.add(Case(case_id="case-1", title="c", status="open"))
        session.flush()
        session.execute(
            case_findings.insert().values(case_id="case-1", finding_id="f-oncase")
        )
        session.add_all(
            [
                IntakeTrigger(kind="detection", state="queued", finding_id="f-queued"),
                # a hunt linked it to a case; its row stays queued until the daemon's next pass
                IntakeTrigger(kind="detection", state="queued", finding_id="f-oncase"),
                IntakeTrigger(kind="detection", state="queued", finding_id="f-noise"),
                IntakeTrigger(
                    kind="detection",
                    state="expired",
                    finding_id="f-expired",
                    decided_at=now,
                ),
                IntakeTrigger(kind="schedule", state="queued"),
                # the alert was deleted: Overview cannot see it, so Triage does not list it
                IntakeTrigger(kind="detection", state="queued", finding_id="f-gone"),
            ]
        )
    yield
    engine.dispose()


def test_overview_counts_do_not_move(seeded):
    from core.findings.alert_outcomes import terminal_states_today

    for kwargs in ({}, {"all_time": True}):
        counts = Counter(r["terminal_state"] for r in terminal_states_today(**kwargs))
        assert counts == {"scoring": 1, "working": 1, "dismissed": 1, "waiting": 2}


def test_triage_reads_overviews_groups(seeded):
    from services.api.triage_read import triage_payload

    payload = triage_payload(range="all")
    rows = {r["finding_id"]: r for r in payload["rows"] if r["finding_id"]}
    assert {fid: r["state"] for fid, r in rows.items()} == {
        "f-queued": "queued",
        "f-oncase": "merged",
        "f-noise": "dismissed",
        "f-none": "not_picked_up",
        "f-expired": "not_picked_up",
    }
    # on a case with its row still queued: Overview's case, not Waiting
    assert rows["f-oncase"]["state_label"] == "Added to a case"
    assert rows["f-oncase"]["case_door"] == "case-1"
    assert rows["f-noise"]["state_label"] == "Dismissed as noise"
    assert rows["f-noise"]["dismissed"] is True

    # the tile counts alerts; the queued schedule row is the sub-line and the chip
    assert payload["strip"]["waiting"] == 1
    assert payload["strip"]["waiting_other"] == 1
    assert payload["counts"]["state"]["queued"] == 2
    assert payload["counts"]["kind"] == {"detection": 5, "schedule": 1}

    waiting = triage_payload(range="all", kind="detection", state="queued")
    assert [r["finding_id"] for r in waiting["rows"]] == ["f-queued"]
    assert waiting["matched"] == 1
