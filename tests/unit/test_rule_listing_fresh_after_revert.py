"""A listing read straight after a person's Revert or release shows the new
stage, hold, mover and move count (#4053). Postgres's write statistics lag a
commit by seconds, so the cache also checks the newest stage row's id.

Real Postgres only: set VIGIL_TEST_PG_DSN to a throwaway database to run these,
e.g. postgresql://postgres:t@127.0.0.1:55453/t (the unit suite has none).
"""

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
from sqlalchemy import text

DSN = os.environ.get("VIGIL_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="VIGIL_TEST_PG_DSN not set")


@pytest.fixture
def db(monkeypatch):
    """The detection tables, empty, on the throwaway Postgres; a cold listing cache."""
    url = urlparse(DSN)
    monkeypatch.setenv("POSTGRES_HOST", url.hostname)
    monkeypatch.setenv("POSTGRES_PORT", str(url.port))
    monkeypatch.setenv("POSTGRES_DB", url.path.lstrip("/"))
    monkeypatch.setenv("POSTGRES_USER", url.username)
    monkeypatch.setenv("POSTGRES_PASSWORD", url.password)
    monkeypatch.setenv("POSTGRES_SSL_MODE", "disable")
    from core.detections.inventory import catalog
    from core.storage import connection

    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    manager = connection.get_db_manager()
    manager.initialize(force=True)
    manager.create_tables()
    with manager.session_scope() as session:
        session.execute(text("TRUNCATE detection_rules RESTART IDENTITY CASCADE"))
    catalog._listing.clear()
    monkeypatch.setattr(
        "core.agents.attribution.principal_id_for_username", lambda name: name
    )
    yield manager
    catalog._listing.clear()
    manager._engine.dispose()
    connection.DatabaseManager._instance = None


def _live_rule(db, key="r"):
    from core.detections.inventory import readers
    from core.storage.models import (
        DetectionRule,
        DetectionRuleStage,
        DetectionRuleVersion,
    )

    t0 = datetime.now(timezone.utc) - timedelta(days=9)
    with db.session_scope() as session:
        rule = DetectionRule(
            source=readers.OVERLAY_SOURCE,
            native_key=key,
            name=key,
            language="splunk",
            status="active",
            latest_version=1,
        )
        session.add(rule)
        session.flush()
        session.add(
            DetectionRuleVersion(
                rule_id=rule.id,
                version=1,
                fingerprint="f",
                content={},
                checks={},
            )
        )
        for stage, at in (("shadow", t0), ("live", t0 + timedelta(days=8))):
            session.add(
                DetectionRuleStage(
                    rule_id=rule.id,
                    version=1,
                    stage=stage,
                    actor="system",
                    proof={},
                    reason="",
                    at=at,
                )
            )
        return rule.id


def _row(rid):
    from core.detections.inventory import catalog

    (row,) = catalog.health_listing(rule_ids=[rid])["rules"]
    return row


def test_a_listing_read_straight_after_revert_and_release_is_current(db):
    from core.detections.inventory import stages

    rid = _live_rule(db)
    before = _row(rid)  # warms the cache
    assert (before["stage"], before["held"], before["stage_moves"]) == (
        "live",
        False,
        2,
    )

    stages.revert(rid, ("ana", "ana"), "too loud")
    after = _row(rid)  # no wait
    assert (after["stage"], after["held"], after["held_by"]) == ("shadow", True, "ana")
    assert (after["stage_actor"], after["stage_from"], after["stage_moves"]) == (
        "ana",
        "live",
        3,
    )

    stages.release(rid, ("ben", "ben"), "fixed")
    released = _row(rid)
    assert (released["held"], released["stage_actor"], released["stage_moves"]) == (
        False,
        "ben",
        4,
    )


def _siem_rule(db, name):
    from core.storage.models import DetectionRule, DetectionRuleVersion

    with db.session_scope() as session:
        rule = DetectionRule(
            source="elastic",
            native_key=name,
            name=name,
            language="elastic",
            status="active",
            latest_version=1,
        )
        session.add(rule)
        session.flush()
        session.add(
            DetectionRuleVersion(
                rule_id=rule.id,
                version=1,
                fingerprint="f",
                content={},
                checks={"runs": {"result": "pass"}},
            )
        )
        return rule.id


def _settled(db):
    """Wait until the setup's writes reach Postgres's write statistics (flushed
    about once a second per connection), so a later one can't force a rebuild."""
    import time

    from core.detections.inventory import catalog

    tables = catalog._LISTING_TABLES
    with db.session_scope() as session:
        marks = catalog._write_marks(session, tables)
    for _ in range(20):
        time.sleep(1.2)
        with db.session_scope() as session:
            now = catalog._write_marks(session, tables)
        if now == marks:
            return
        marks = now


def _rebuilt():
    """The listing as a cold cache builds it, every copy read again."""
    from core.detections.inventory import catalog

    catalog._listing.clear()
    return catalog._listing_now()


def test_a_stage_write_patches_the_listing_to_what_a_rebuild_gives(db, monkeypatch):
    from core.detections.inventory import catalog, stages
    from core.storage.models import DetectionRuleStage

    live, other = _live_rule(db), _live_rule(db, "r2")
    _siem_rule(db, "siem")
    _settled(db)
    catalog._listing_now()  # warm
    reads = []
    real = catalog._copies_listed
    monkeypatch.setattr(catalog, "_copies_listed", lambda s: reads.append(1) or real(s))

    def sweep_off(rid):
        with db.session_scope() as session:
            session.add(
                DetectionRuleStage(
                    rule_id=rid,
                    version=1,
                    stage="off",
                    actor="system",
                    proof={},
                    reason="fails lint",
                    at=datetime.now(timezone.utc),
                )
            )

    for write in (
        lambda: stages.revert(live, ("ana", "ana"), "too loud"),
        lambda: stages.release(live, ("ben", "ben"), "fixed"),
        lambda: sweep_off(other),
    ):
        write()
        patched = catalog._listing_now()
        assert reads == []  # no copy was read again
        monkeypatch.setattr(catalog, "_copies_listed", real)
        assert patched == _rebuilt()
        monkeypatch.setattr(
            catalog, "_copies_listed", lambda s: reads.append(1) or real(s)
        )

    counts = catalog.health_listing()["stage_counts"]
    assert counts == {"shadow": 1, "live": 0, "off": 1}
    (row,) = catalog.health_listing(rule_ids=[other])["rules"]
    assert (row["stage"], row["stage_actor"], row["stage_reason"]) == (
        "off",
        "system",
        "fails lint",
    )


def test_a_stage_write_with_a_new_version_or_checks_is_read_fresh(db):
    """A move written with other rule tables (a sync's new version, or a run's
    checks then the sweep's move) is read as a rebuild reads it, though the
    write statistics don't show those tables moved yet."""
    from core.detections.inventory import catalog
    from core.storage.models import (
        DetectionRule,
        DetectionRuleStage,
        DetectionRuleVersion,
    )

    synced, swept = _live_rule(db), _live_rule(db, "r2")
    with db.session_scope() as session:  # version 1 has run; version 2 won't have
        session.get(DetectionRuleVersion, (synced, 1)).checks = {
            "runs": {"result": "pass"}
        }
    _settled(db)
    catalog._listing_now()  # warm
    now = datetime.now(timezone.utc)

    with db.session_scope() as session:  # a sync: version 2 enters shadow
        rule = session.get(DetectionRule, synced)
        rule.latest_version = 2
        session.add(
            DetectionRuleVersion(
                rule_id=synced,
                version=2,
                fingerprint="g",
                content={},
                checks={},
            )
        )
        session.add(
            DetectionRuleStage(
                rule_id=synced,
                version=2,
                stage="shadow",
                actor="system",
                proof={},
                reason="new version",
                at=now,
            )
        )
    first = catalog._listing_now()
    assert first == _rebuilt()
    (row,) = catalog.health_listing(rule_ids=[synced])["rules"]
    assert (row["checks_run"], row["stage_reason"]) == (0, "new version")

    with db.session_scope() as session:  # a run's checks, then the sweep's move
        version = session.get(DetectionRuleVersion, (swept, 1))
        version.checks = {"runs": {"result": "fail", "reason": "boom"}}
    with db.session_scope() as session:
        session.add(
            DetectionRuleStage(
                rule_id=swept,
                version=1,
                stage="shadow",
                actor="system",
                proof={},
                reason="its runs failed",
                at=now,
            )
        )
    second = catalog._listing_now()
    assert second == _rebuilt()
    (row,) = catalog.health_listing(rule_ids=[swept])["rules"]
    assert (row["health"], row["stage"], row["stage_reason"]) == (
        "fails_to_run",
        "shadow",
        "its runs failed",
    )


def _rewrite_checks(db, rid, checks):
    """A run rewrites the version's checks in place, as sync._write_checks does."""
    from sqlalchemy import update

    from core.storage.models import DetectionRuleVersion

    with db.session_scope() as session:
        session.execute(
            update(DetectionRuleVersion)
            .where(
                DetectionRuleVersion.rule_id == rid, DetectionRuleVersion.version == 1
            )
            .values(checks=checks)
        )


def test_a_moved_rule_is_exact_inside_the_scan_window(db, monkeypatch):
    """Inside the second the table scan is reused for, a stage move written with
    its rule's new version, or right after a run rewrote its checks, is still
    read as a rebuild reads it: the moved rule's own copy is read again."""
    from core.detections.inventory import catalog
    from core.storage.models import (
        DetectionRule,
        DetectionRuleStage,
        DetectionRuleVersion,
    )

    synced, swept = _live_rule(db), _live_rule(db, "r2")
    _rewrite_checks(db, synced, {"runs": {"result": "pass"}})
    catalog._listing_now()  # warm: the scan is taken now
    monkeypatch.setattr(catalog, "_SCAN_EVERY", 3600.0)  # and reused from here on
    now = datetime.now(timezone.utc)

    with db.session_scope() as session:  # a sync: version 2 enters shadow
        session.get(DetectionRule, synced).latest_version = 2
        session.add(
            DetectionRuleVersion(
                rule_id=synced, version=2, fingerprint="g", content={}, checks={}
            )
        )
        session.flush()
        session.add(
            DetectionRuleStage(
                rule_id=synced,
                version=2,
                stage="shadow",
                actor="system",
                proof={},
                reason="new version",
                at=now,
            )
        )
    first = catalog._listing_now()
    assert first == _rebuilt()
    (row,) = catalog.health_listing(rule_ids=[synced])["rules"]
    assert (row["checks_run"], row["stage_reason"]) == (0, "new version")

    catalog._listing_now()  # the rebuild took a scan; reused again from here
    _rewrite_checks(db, swept, {"runs": {"result": "fail", "reason": "boom"}})
    with db.session_scope() as session:  # the sweep's move, its own commit
        session.add(
            DetectionRuleStage(
                rule_id=swept,
                version=1,
                stage="shadow",
                actor="system",
                proof={},
                reason="its runs failed",
                at=now,
            )
        )
    second = catalog._listing_now()
    assert second == _rebuilt()
    (row,) = catalog.health_listing(rule_ids=[swept])["rules"]
    assert (row["health"], row["stage"], row["stage_reason"]) == (
        "fails_to_run",
        "shadow",
        "its runs failed",
    )


def test_another_rules_checks_show_within_the_scan_window(db):
    """A run rewrites rule A's checks in place (no check-change row when the
    result holds); the sweep then moves rule B only. B's move shows at once; A's
    new checks show once the second is up, though the write statistics lag."""
    import time

    from core.detections.inventory import catalog
    from core.storage.models import DetectionRuleStage

    moved = _live_rule(db)
    bystander = _siem_rule(db, "siem")
    catalog._listing_now()  # warm
    _rewrite_checks(
        db, bystander, {"runs": {"result": "pass"}, "quiet": {"count": 7, "days": 3}}
    )
    with db.session_scope() as session:
        session.add(
            DetectionRuleStage(
                rule_id=moved,
                version=1,
                stage="off",
                actor="system",
                proof={},
                reason="fails lint",
                at=datetime.now(timezone.utc),
            )
        )
    (row,) = catalog.health_listing(rule_ids=[moved])["rules"]
    assert (row["stage"], row["stage_reason"]) == ("off", "fails lint")
    time.sleep(catalog._SCAN_EVERY + 0.05)
    assert catalog._listing_now() == _rebuilt()
    (row,) = catalog.health_listing(rule_ids=[bystander])["rules"]
    assert (row["siem_alerts"], row["siem_days"]) == (7, 3)


def test_a_checks_write_with_no_stage_move_shows_within_the_scan_window(db):
    """A run that moves no stage: its checks show once the second is up."""
    import time

    from core.detections.inventory import catalog

    rid = _siem_rule(db, "siem")
    assert _row(rid)["siem_alerts"] is None  # warms the cache
    _rewrite_checks(
        db, rid, {"runs": {"result": "pass"}, "quiet": {"count": 12, "days": 7}}
    )
    time.sleep(catalog._SCAN_EVERY + 0.05)
    row = _row(rid)
    assert (row["siem_alerts"], row["siem_days"]) == (12, 7)


def test_with_a_scan_on_every_read_a_checks_write_shows_at_once(db, monkeypatch):
    """The scan itself is exact: read every time, an in-place checks write shows
    on the very next read, where the write statistics would lag it by seconds."""
    from core.detections.inventory import catalog

    monkeypatch.setattr(catalog, "_SCAN_EVERY", 0.0, raising=False)
    rid = _siem_rule(db, "siem")
    assert _row(rid)["siem_alerts"] is None  # warms the cache
    _rewrite_checks(
        db, rid, {"runs": {"result": "pass"}, "quiet": {"count": 12, "days": 7}}
    )
    row = _row(rid)  # no wait
    assert (row["siem_alerts"], row["siem_days"]) == (12, 7)
