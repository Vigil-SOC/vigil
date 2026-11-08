"""The metrics report reads only the check changes its numbers come from (#4070):
the distinct reasons in the throttle window, and the first-week kinds. These
compare each narrowed read with the full scan it replaces, on the same rows.

Real Postgres only: set VIGIL_TEST_PG_DSN to a throwaway database to run these
(the fixture empties the detection tables).
"""

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
from sqlalchemy import select, text

from core.detections.inventory import catalog, metrics, readers

DSN = os.environ.get("VIGIL_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="VIGIL_TEST_PG_DSN not set")

NOW = datetime.now(timezone.utc)
START = NOW - timedelta(days=20)  # the first SIEM rule read: the week is days 0-7


@pytest.fixture
def db(monkeypatch):
    url = urlparse(DSN)
    for key, value in {
        "POSTGRES_HOST": url.hostname,
        "POSTGRES_PORT": str(url.port),
        "POSTGRES_DB": url.path.lstrip("/"),
        "POSTGRES_USER": url.username,
        "POSTGRES_PASSWORD": url.password,
        "POSTGRES_SSL_MODE": "disable",
    }.items():
        monkeypatch.setenv(key, value)
    from core.storage import connection

    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    manager = connection.get_db_manager()
    manager.initialize(force=True)
    manager.create_tables()
    with manager.session_scope() as session:
        session.execute(text("TRUNCATE detection_rules RESTART IDENTITY CASCADE"))
    yield manager
    manager._engine.dispose()
    connection.DatabaseManager._instance = None


REASONS = [
    "HTTP 429 Too Many Requests",
    "search was throttled by the SIEM",
    "The maximum number of concurrent historical searches has been reached",
    "es_rejected_execution_exception: rejected execution",
    "ſearch quota exceeded",  # Python's case-insensitive match reads ſ as s
    "no events in index=waf_rate_limit in 7 d",
    "index throttle_audit not found",
    "repo rule 429 not found",
    "no alerts in 30 d",
    "no events in 7 d",
    None,
]


def _seed(db):
    """Rules of each source with changes of each check, result and reason, some
    before the throttle window, in and after the first week."""
    from core.storage.models import DetectionRule, DetectionRuleCheckChange

    rows = []
    with db.session_scope() as session:
        for n, source in enumerate(
            ["splunk", "elastic", "wazuh", readers.OVERLAY_SOURCE, "repo:acme"]
        ):
            for i in range(6):
                rule = DetectionRule(
                    source=source,
                    native_key=f"{source}-{i}",
                    name=f"{source} {i}",
                    language="splunk",
                    status="active",
                    latest_version=1,
                )
                session.add(rule)
                session.flush()
                for j, reason in enumerate(REASONS):
                    for check in ("silent", "sees", "runs", "quiet"):
                        for result in ("fail", "unknown", "pass"):
                            days = (n * 7 + i * 3 + j * 5) % 45  # 0..44 days ago
                            rows.append(
                                DetectionRuleCheckChange(
                                    rule_id=rule.id,
                                    version=1,
                                    check_key=check,
                                    new_result=result,
                                    reason=reason,
                                    changed_at=NOW - timedelta(days=days, minutes=j),
                                )
                            )
        session.add_all(rows)
    return len(rows)


def test_the_throttle_read_finds_what_the_full_scan_found(db):
    from core.storage.models import DetectionRule
    from core.storage.models import DetectionRuleCheckChange as change

    _seed(db)
    since = NOW - timedelta(days=metrics.THROTTLE_DAYS)
    with db.session_scope() as session:
        full = session.execute(
            select(
                change.rule_id,
                DetectionRule.source,
                change.check_key,
                change.new_result,
                change.reason,
                change.changed_at,
            )
            .join(DetectionRule, DetectionRule.id == change.rule_id)
            .where(change.changed_at >= since, change.reason.isnot(None))
        ).all()
        scanned = [
            (r, catalog.tool_of(s), c, res, why, at)
            for r, s, c, res, why, at in full
            if metrics.throttle_error(why)
        ]
        narrowed = metrics._throttled(session, since)
    assert scanned and sorted(narrowed) == sorted(scanned)
    old, new = (metrics.search_budget({}, {}, rows) for rows in (scanned, narrowed))
    assert new["throttle_errors"] == old["throttle_errors"] > 0
    assert {r[4] for r in narrowed} >= {"ſearch quota exceeded"}


def test_the_first_week_read_gives_the_full_scans_numbers(db):
    from core.storage.models import DetectionRule
    from core.storage.models import DetectionRuleCheckChange as change

    _seed(db)
    with db.session_scope() as session:
        full = session.execute(
            select(
                change.rule_id,
                DetectionRule.name,
                change.check_key,
                change.new_result,
                change.reason,
                change.changed_at,
            )
            .join(DetectionRule, DetectionRule.id == change.rule_id)
            .where(
                DetectionRule.source.in_(metrics.SIEM_SOURCES),
                change.check_key.in_(("silent", "sees")),
            )
            .order_by(change.id)
        ).all()
        narrowed = metrics._siem_changes(session, START)
        unstarted = metrics._siem_changes(session, None)
    assert len(narrowed) < len(full)
    old = metrics.first_week(START, full, NOW)
    assert metrics.first_week(START, narrowed, NOW) == old
    assert all(old[kind] for kind in ("silent", "no_data", "never_firing"))
    assert metrics.first_week(None, unstarted, NOW) == metrics.first_week(
        None, full, NOW
    )
