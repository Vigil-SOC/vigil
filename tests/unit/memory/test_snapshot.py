"""Freezing the tier for an eval run (#736), against a real Postgres.

A snapshot is a schema copy selected by ``search_path``, so nothing here can be
asserted against a fake: what is being tested is that Postgres resolves the
tier's tables to the copy, that the copy stops moving when live moves, and that
a read against it touches no live row. A double would agree with whatever this
module happened to do.

The eval process is simulated by putting ``PGOPTIONS`` in the environment and
rebuilding the engine, which is what a separate process does at startup. It is
rebuilt again on the way out, so a test that fails does not leave the manager
pointing at a snapshot.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from core.memory.recall import recall_entity
from core.memory.snapshot import (
    CORPUS_TABLES,
    SNAPSHOT_PREFIX,
    SnapshotError,
    SnapshotExists,
    create_snapshot,
    drop_snapshot,
    get_snapshot,
    list_snapshots,
    pgoptions_for,
)
from core.storage.connection import get_db_manager, get_db_session
from core.storage.models import (
    EpisodicGap,
    EpisodicReadLog,
    EpisodicSighting,
    EpisodicVerdict,
    EpisodicVerdictSource,
)

pytestmark = [pytest.mark.unit, pytest.mark.database, pytest.mark.external_service]

EPOCH = datetime(2026, 8, 1, tzinfo=timezone.utc)
KEY = "ip:198.51.100.7"
OTHER = "ip:198.51.100.8"


@pytest.fixture
def live():
    """A session on the live tier, emptied first.

    Every test here counts whole tables on both sides of a copy, so each starts
    from nothing rather than filtering its own rows out.
    """
    db = get_db_session()
    try:
        for model in (
            EpisodicVerdictSource,
            EpisodicVerdict,
            EpisodicSighting,
            EpisodicGap,
            EpisodicReadLog,
        ):
            db.query(model).delete()
        db.commit()
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture
def snapshots():
    """Names created by a test, dropped however it ends."""
    made = []

    def make(name, **kwargs):
        made.append(name)
        return create_snapshot(name, **kwargs)

    try:
        yield make
    finally:
        for name in made:
            try:
                drop_snapshot(name, lock_timeout="5s")
            except SnapshotError:
                pass


@contextmanager
def reading(snapshot: str):
    """What an eval process does at startup, and undoes by exiting.

    ``PGOPTIONS`` is read by libpq when a connection is opened, so the engine is
    rebuilt to force new ones. Rebuilding again on the way out is not tidiness:
    the manager is a singleton and every later test in the session would other-
    wise read the snapshot.

    The manager's own config is handed back rather than letting ``retarget()``
    rebuild one, which would re-read ``DatabaseConfig()`` from the environment
    and land on the origin database -- not the throwaway one ``conftest`` put
    this process on. Only the engine is meant to change here.
    """
    manager = get_db_manager()
    target = manager.config
    before = os.environ.get("PGOPTIONS")
    os.environ["PGOPTIONS"] = pgoptions_for(snapshot)
    manager.retarget(target)
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("PGOPTIONS", None)
        else:
            os.environ["PGOPTIONS"] = before
        manager.retarget(target)


def sighting(db, key: str, *, investigation: str, concluded: datetime):
    db.add(
        EpisodicSighting(
            entity_key=key,
            investigation_kind="hunt",
            investigation_id=investigation,
            source_system="splunk",
            hit_count=3,
            attacker_influenceable=True,
            first_seen=concluded - timedelta(hours=1),
            last_seen=concluded,
            concluded_at=concluded,
        )
    )


def verdict(db, keys, *, investigation: str, concluded: datetime):
    row = EpisodicVerdict(
        investigation_kind="hunt",
        investigation_id=investigation,
        hypothesis_id=f"h-{investigation}",
        statement="beaconing to a known-bad host",
        outcome="proven",
        rationale="regular interval, low jitter",
        subject_entities=list(keys),
        attacker_influenceable_only=False,
        trust="agent",
        first_seen=concluded - timedelta(hours=2),
        last_seen=concluded,
        window_source="observed",
        concluded_at=concluded,
    )
    db.add(row)
    db.flush()
    db.add(
        EpisodicVerdictSource(
            verdict_id=row.id,
            source_system="splunk",
            stance="supports",
            source_tier="telemetry",
        )
    )
    return row


def gap(db, keys, *, investigation: str, concluded: datetime):
    db.add(
        EpisodicGap(
            investigation_kind="hunt",
            investigation_id=investigation,
            hypothesis_id=f"g-{investigation}",
            statement="did anything else talk to it",
            disposition="budget_exhausted",
            reason="the hunt ran out of turns",
            subject_entities=list(keys),
            concluded_at=concluded,
        )
    )


def seeded(db, *, investigation="hunt-1", key=KEY):
    sighting(db, key, investigation=investigation, concluded=EPOCH)
    verdict(db, [key], investigation=investigation, concluded=EPOCH)
    gap(db, [key], investigation=investigation, concluded=EPOCH)
    db.commit()


def test_a_named_snapshot_holds_a_copy_of_the_live_tier(live, snapshots):
    seeded(live)

    held = snapshots("copy_of_live")

    assert held.schema == SNAPSHOT_PREFIX + "copy_of_live"
    assert held.rows == {
        "episodic_sightings": 1,
        "episodic_verdicts": 1,
        "episodic_verdict_sources": 1,
        "episodic_gaps": 1,
    }
    assert not held.is_empty


def test_the_copy_matches_the_rows_it_was_taken_from(live, snapshots):
    """The spec's own statement of what correctness is here.

    Counting rows would pass a copy that garbled every column, and
    ``INSERT ... SELECT`` matches by position -- so a column reordered between
    the live table and the copy is exactly the failure this catches.
    """
    seeded(live)
    held = snapshots("faithful")

    try:
        for table in CORPUS_TABLES:
            original = live.execute(
                text(f"SELECT * FROM public.{table} ORDER BY id")
            ).fetchall()
            copied = live.execute(
                text(f"SELECT * FROM {held.schema}.{table} ORDER BY id")
            ).fetchall()
            assert copied == original, table
            assert original, f"{table} seeded nothing, so this asserted nothing"
    finally:
        # Reading the snapshot through this session takes a lock on its tables
        # and holds it until the transaction ends. The fixture drops the schema
        # from a different session, and DROP ... CASCADE waits on that lock
        # rather than failing -- so without this the suite hangs, it does not
        # fail.
        live.rollback()


def test_a_listing_survives_a_schema_nobody_here_built(live, snapshots):
    """A stray prefixed schema must not hide every real snapshot.

    The same reason the creation stamp is optional: a listing is how an operator
    finds what exists, and one that raises tells them nothing.
    """
    snapshots("real_one")
    live.execute(text(f"CREATE SCHEMA {SNAPSHOT_PREFIX}handmade"))
    live.commit()
    try:
        listed = {held.name: held for held in list_snapshots()}
        assert "real_one" in listed
        assert listed["handmade"].rows == {table: 0 for table in CORPUS_TABLES}
    finally:
        live.execute(text(f"DROP SCHEMA {SNAPSHOT_PREFIX}handmade CASCADE"))
        live.commit()


def test_an_eval_reads_a_named_snapshot_without_changing_a_query(live, snapshots):
    """The point of the schema copy: recall's SQL is untouched."""
    seeded(live)
    snapshots("read_by_an_eval")

    with reading("read_by_an_eval"):
        result = recall_entity({"entity_key": KEY})

    assert len(result["sightings"]) == 1
    assert len(result["verdicts"]) == 1
    assert len(result["gaps"]) == 1


def test_an_empty_snapshot_recalls_nothing(live, snapshots):
    """The control the whole measurement rests on."""
    seeded(live)
    held = snapshots("the_control", empty=True)
    assert held.is_empty

    with reading("the_control"):
        result = recall_entity({"entity_key": KEY})

    assert result["sightings"] == []
    assert result["verdicts"] == []
    assert result["gaps"] == []


def test_two_reads_of_one_snapshot_return_the_same_rows(live, snapshots):
    """``as_of`` is pinned because it defaults to now and would differ per call.

    Two eval runs comparing scores hold it fixed for the same reason: the
    freshness filter is part of what a run read, and letting it float would
    make a rerun a different query rather than the same one.
    """
    seeded(live)
    snapshots("read_twice")
    asked = {"entity_key": KEY, "as_of": "2026-09-01T00:00:00Z"}

    with reading("read_twice"):
        first = recall_entity(dict(asked))
        second = recall_entity(dict(asked))

    assert first == second


def test_a_write_to_the_live_tier_does_not_reach_a_snapshot(live, snapshots):
    seeded(live)
    snapshots("frozen")

    sighting(live, KEY, investigation="hunt-2", concluded=EPOCH + timedelta(days=1))
    verdict(live, [KEY], investigation="hunt-2", concluded=EPOCH + timedelta(days=1))
    live.commit()

    assert get_snapshot("frozen").rows["episodic_sightings"] == 1
    with reading("frozen"):
        result = recall_entity({"entity_key": KEY})
    assert len(result["sightings"]) == 1

    # The live tier did move; the snapshot is what did not.
    assert len(recall_entity({"entity_key": KEY})["sightings"]) == 2


def test_snapshots_coexist_and_are_selected_by_name(live, snapshots):
    seeded(live)
    snapshots("populated")
    snapshots("empty_one", empty=True)

    names = {held.name for held in list_snapshots()}
    assert {"populated", "empty_one"} <= names

    with reading("populated"):
        assert len(recall_entity({"entity_key": KEY})["sightings"]) == 1
    with reading("empty_one"):
        assert recall_entity({"entity_key": KEY})["sightings"] == []


def test_a_read_against_a_snapshot_leaves_the_live_journal_alone(live, snapshots):
    """The failure with no error message.

    ``search_path`` falls through, so a table missing from the copy resolves to
    ``public`` -- and recall writes. If the journal were not copied, an eval's
    reads would be logged against production and nothing would say so.
    """
    seeded(live)
    snapshots("journal_stays_put")
    before = live.query(EpisodicReadLog).count()

    with reading("journal_stays_put"):
        recall_entity({"entity_key": KEY})

    live.rollback()  # a fresh read of what the other connection committed
    assert live.query(EpisodicReadLog).count() == before


def test_a_snapshot_owns_its_sequences(live, snapshots):
    """A copy sharing the live sequence advances live state as the eval reads."""
    seeded(live)
    snapshots("own_sequences")
    before = live.execute(
        text("SELECT last_value FROM public.episodic_read_log_id_seq")
    ).scalar()

    with reading("own_sequences"):
        recall_entity({"entity_key": KEY})

    live.rollback()
    after = live.execute(
        text("SELECT last_value FROM public.episodic_read_log_id_seq")
    ).scalar()
    assert after == before


def test_a_snapshot_taken_from_inside_a_snapshot_binds_its_own_keys(live, snapshots):
    """The failure that is a valid constraint pointing at the wrong table.

    pg_get_constraintdef renders against whatever search_path it finds, so with
    a snapshot selected it qualifies the reference as public.episodic_verdicts
    -- and replaying that text hands the new copy a foreign key into the live
    tier. Nothing errors; the copy simply is not one.
    """
    seeded(live)
    snapshots("taken_from")

    with reading("taken_from"):
        inner = snapshots("taken_inside")

    referenced = live.execute(
        text(
            "SELECT confrelid::regclass::text FROM pg_constraint "
            "WHERE contype = 'f' AND conrelid = CAST(:relation AS regclass)"
        ),
        {"relation": f"{inner.schema}.episodic_verdict_sources"},
    ).scalar()
    live.rollback()
    assert referenced == f"{inner.schema}.episodic_verdicts"


def test_the_copy_is_taken_at_one_instant(live, snapshots, monkeypatch):
    """Four INSERT ... SELECT statements have to see the same tier.

    Asserted on the isolation level rather than by interleaving a write, because
    the copy loop offers nothing to interleave against -- which is the point,
    but leaves this the honest way to pin the guarantee. Under the default READ
    COMMITTED each statement takes its own snapshot and a Distil re-derive
    landing mid-copy separates Verdicts from their sources.
    """
    import core.memory.snapshot as module

    seen = {}
    original = module._row_counts

    def record(db, schema):
        seen["isolation"] = db.execute(text("SHOW transaction_isolation")).scalar()
        return original(db, schema)

    monkeypatch.setattr(module, "_row_counts", record)
    seeded(live)
    snapshots("one_instant")

    assert seen["isolation"] == "repeatable read"


def test_a_snapshot_is_not_overwritten(live, snapshots):
    snapshots("kept")
    with pytest.raises(SnapshotExists):
        create_snapshot("kept")


def test_a_name_that_would_need_quoting_is_refused():
    for name in ("2026-09-01", "Sept", "with space", "", "with;semicolon"):
        with pytest.raises(SnapshotError):
            pgoptions_for(name)


def test_dropping_a_snapshot_that_is_not_there_says_so():
    with pytest.raises(SnapshotError):
        drop_snapshot("never_made")
