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


def _live_rule(db):
    from core.detections.inventory import readers
    from core.storage.models import (
        DetectionRule,
        DetectionRuleStage,
        DetectionRuleVersion,
    )

    t0 = datetime.now(timezone.utc) - timedelta(days=9)
    with db.session_scope() as session:
        rule = DetectionRule(
            source=readers.OVERLAY_SOURCE, native_key="r", name="r",
            language="splunk", status="active", latest_version=1,
        )
        session.add(rule)
        session.flush()
        session.add(DetectionRuleVersion(
            rule_id=rule.id, version=1, fingerprint="f", content={}, checks={},
        ))
        for stage, at in (("shadow", t0), ("live", t0 + timedelta(days=8))):
            session.add(DetectionRuleStage(
                rule_id=rule.id, version=1, stage=stage, actor="system",
                proof={}, reason="", at=at,
            ))
        return rule.id


def _row(rid):
    from core.detections.inventory import catalog

    (row,) = catalog.health_listing(rule_ids=[rid])["rules"]
    return row


def test_a_listing_read_straight_after_revert_and_release_is_current(db):
    from core.detections.inventory import stages

    rid = _live_rule(db)
    before = _row(rid)  # warms the cache
    assert (before["stage"], before["held"], before["stage_moves"]) == ("live", False, 2)

    stages.revert(rid, ("ana", "ana"), "too loud")
    after = _row(rid)  # no wait
    assert (after["stage"], after["held"], after["held_by"]) == ("shadow", True, "ana")
    assert (after["stage_actor"], after["stage_from"], after["stage_moves"]) == (
        "ana", "live", 3,
    )

    stages.release(rid, ("ben", "ben"), "fixed")
    released = _row(rid)
    assert (released["held"], released["stage_actor"], released["stage_moves"]) == (
        False, "ben", 4,
    )
