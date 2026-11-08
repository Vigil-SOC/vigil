"""A live Wazuh copy runs nowhere until the manager loads Vigil's file for it:
Vigil runs Wazuh copies only in shadow. Until then the listing row carries
``waiting``, with where its deploy got, so the rule list doesn't read its
shadow-run checks as passing. A Splunk or Elastic copy never waits: Vigil runs
it live until the SIEM's own copy takes over.

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

PR = "https://github.com/acme/wazuh-rules/pull/57"
PASSING = {
    "runs": {"result": "pass", "reason": "ran"},
    "sees": {"result": "pass", "reason": "wazuh-archives-* has events in 7d"},
}


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
    yield manager
    catalog._listing.clear()
    manager._engine.dispose()
    connection.DatabaseManager._instance = None


def _copy(db, key, language="wazuh", stage="live", checks=None):
    """An overlay copy at ``stage`` (shadow for 8 days first, if live)."""
    from core.detections.inventory import readers
    from core.storage.models import (
        DetectionRule,
        DetectionRuleStage,
        DetectionRuleVersion,
    )

    t0 = datetime.now(timezone.utc) - timedelta(days=9)
    moves = [("shadow", t0)] + (
        [("live", t0 + timedelta(days=8))] if stage == "live" else []
    )
    with db.session_scope() as session:
        rule = DetectionRule(
            source=readers.OVERLAY_SOURCE,
            native_key=key,
            name=f"probe {key}",
            language=language,
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
                checks={**PASSING, **(checks or {})},
            )
        )
        for st, at in moves:
            session.add(
                DetectionRuleStage(
                    rule_id=rule.id,
                    version=1,
                    stage=st,
                    actor="system",
                    proof={},
                    reason="",
                    at=at,
                )
            )
        return rule.id


def _manager_rule(db, origin, key, path):
    """The Wazuh manager's rule with that id, read from ``path``, linked to ``origin``."""
    with db.session_scope() as session:
        session.execute(
            text(
                "INSERT INTO detection_rules (source, native_key, name, language,"
                " status, latest_version, path, origin_rule_id, link_kind)"
                " VALUES ('wazuh', :k, :n, 'wazuh', 'active', 1, :p, :o, 'same_id')"
            ),
            {"k": key, "n": f"manager {key}", "p": path, "o": origin},
        )


def _rows(*rids):
    from core.detections.inventory import catalog

    rows = catalog.health_listing(rule_ids=list(rids))["rules"]
    return {r["rule_id"]: r for r in rows}


def test_a_live_wazuh_copy_waits_with_where_its_deploy_got(db, monkeypatch):
    from core.detections.inventory import catalog

    monkeypatch.setattr(catalog, "_SCAN_EVERY", 0)
    pr = _copy(db, "100101", checks={"deploy": {"version": 1, "pr_url": PR}})
    in_repo = _copy(
        db,
        "100102",
        checks={"deploy": {"version": 1, "in_repo": "rules/vigil_100102.xml on main"}},
    )
    failed = _copy(
        db, "100103", checks={"deploy": {"version": 1, "error": "no GitHub token"}}
    )
    none = _copy(db, "100104")
    shadow = _copy(db, "100105", stage="shadow", checks={"deploy": {"pr_url": PR}})
    splunk = _copy(db, "s1", language="splunk", checks={"deploy": {"pr_url": PR}})
    elastic = _copy(db, "e1", language="elastic")

    rows = _rows(pr, in_repo, failed, none, shadow, splunk, elastic)
    assert rows[pr]["waiting"] == {"pr_url": PR}
    assert rows[in_repo]["waiting"] == {"in_repo": "rules/vigil_100102.xml on main"}
    assert rows[failed]["waiting"] == {"error": "no GitHub token"}
    assert rows[none]["waiting"] == {}
    # Vigil runs a shadow Wazuh copy itself, and a Splunk or Elastic copy live.
    for rid in (shadow, splunk, elastic):
        assert rows[rid]["waiting"] is None, rid
    # Its health is untouched: the metrics card leaves it out of live health.
    assert rows[pr]["health"] == rows[splunk]["health"] == "no_fire_data"


def test_once_the_manager_loads_vigils_file_it_stops_waiting(db, monkeypatch):
    from core.detections.inventory import catalog

    monkeypatch.setattr(catalog, "_SCAN_EVERY", 0)
    rid = _copy(db, "100111", checks={"deploy": {"version": 1, "pr_url": PR}})
    other = _copy(db, "100112", checks={"deploy": {"version": 1, "pr_url": PR}})
    assert _rows(rid)[rid]["waiting"] == {"pr_url": PR}

    # A customer's own file with that id is another rule: still waiting.
    _manager_rule(db, other, "100112", "etc/rules/local_rules.xml")
    # The PR merged and the manager loaded vigil_<id>.xml: handed off.
    _manager_rule(db, rid, "100111", "etc/rules/vigil_100111.xml")
    rows = _rows(rid, other)
    assert rows[rid]["waiting"] is None
    assert rows[other]["waiting"] == {"pr_url": PR}


def test_a_sweep_promotion_alone_shows_it_waiting_at_once(db, monkeypatch):
    """A stage row with no other write takes the listing's patch path, inside
    the scan's throttle second: the moved row still gets ``waiting``."""
    from core.detections.inventory import catalog
    from core.storage.models import DetectionRuleStage

    monkeypatch.setattr(catalog, "_SCAN_EVERY", 3600)
    rid = _copy(
        db, "100121", stage="shadow", checks={"deploy": {"version": 1, "pr_url": PR}}
    )
    assert _rows(rid)[rid]["waiting"] is None  # warms the cache: shadow

    with db.session_scope() as session:
        session.add(
            DetectionRuleStage(
                rule_id=rid,
                version=1,
                stage="live",
                actor="system",
                proof={},
                reason="",
                at=datetime.now(timezone.utc),
            )
        )
    row = _rows(rid)[rid]
    assert (row["stage"], row["waiting"]) == ("live", {"pr_url": PR})
    # And a person's Revert takes it back: no longer waiting.
    from core.detections.inventory import stages

    monkeypatch.setattr(
        "core.agents.attribution.principal_id_for_username", lambda name: name
    )
    stages.revert(rid, ("ana", "ana"), "not yet")
    row = _rows(rid)[rid]
    assert (row["stage"], row["waiting"]) == ("shadow", None)
