"""Rules imported from a rule source are linted, and a rule keyed to one
environment is no coverage (#4009).

``lint_record`` needs no database. The sync and catalog cases run against a
real Postgres (array and JSONB columns), so they run only when
``VIGIL_TEST_PG_DSN`` names a throwaway database, e.g.
``postgresql://postgres:t@127.0.0.1:55453/t``; the unit suite has none.
"""

import os
from urllib.parse import urlparse

import pytest
from sqlalchemy import text

from core.detections.inventory.record import RuleRecord
from core.detections.lint import lint_record

SIGMA_CLEAN = {
    "title": "Clean",
    "logsource": {"category": "process_creation"},
    "detection": {"sel": {"Image|endswith": "\\mimikatz.exe"}, "condition": "sel"},
}
SIGMA_BAD = {
    "title": "Pinned",
    "logsource": {"category": "firewall"},
    "detection": {"sel": {"DestinationIp": "10.20.30.40"}, "condition": "sel"},
}


def _sigma(key, content, techniques=("T1649",), tactics=("credential-access",)):
    return RuleRecord(
        native_key=key,
        name=key,
        language="sigma",
        path=f"{key}.yml",
        techniques=list(techniques),
        tactics=list(tactics),
        logic={"detection": content["detection"]},
        content=content,
    )


def _spl(key, query, techniques=("T1649",)):
    return RuleRecord(
        native_key=key,
        name=key,
        language="splunk",
        path=None,
        query=query,
        techniques=list(techniques),
        content={"search": query},
    )


def _eql(key, query, techniques=("T1649",), language="eql"):
    return RuleRecord(
        native_key=key,
        name=key,
        language="elastic",
        path=None,
        query=query,
        techniques=list(techniques),
        logic={"language": language},
        content={"query": query},
    )


# lint_record ---------------------------------------------------------------


@pytest.mark.parametrize(
    "clean, bad",
    [
        (
            _sigma("c", SIGMA_CLEAN),
            _sigma("b", SIGMA_BAD),
        ),
        (
            _spl("c", 'index=main EventCode=4688 Image="*\\\\mimikatz.exe"'),
            _spl("b", 'index=main dest="dc01.corp.local"'),
        ),
        (
            _eql("c", 'process where process.name == "mimikatz.exe"'),
            _eql("b", 'network where host.ip == "10.20.30.40"'),
        ),
        # Elastic's kuery (831 of 2,225 corpus rules) is linted as Lucene.
        (
            _eql("c", 'process.name : "mimikatz.exe"', language="kuery"),
            _eql("b", "source.ip : 10.20.30.40", language="kuery"),
        ),
        (
            _eql("c", 'event.action : "logged-in"', language="kuery"),
            _eql("b", 'user.name : "jdoe"', language="kuery"),
        ),
    ],
    ids=["sigma", "spl", "eql", "kuery-ip", "kuery-user"],
)
def test_lint_record_passes_clean_and_fails_one_environment(clean, bad):
    assert lint_record(clean)["passed"] is True
    report = lint_record(bad)
    assert report["passed"] is False
    assert report["findings"]


def test_lint_record_gives_kql_no_lint():
    kql = RuleRecord(native_key="k", name="k", language="kql", path=None, query="a")
    assert lint_record(kql) is None


# sync and catalog ----------------------------------------------------------

DSN = os.environ.get("VIGIL_TEST_PG_DSN")
needs_pg = pytest.mark.skipif(not DSN, reason="VIGIL_TEST_PG_DSN not set")


@pytest.fixture
def db(monkeypatch):
    """The detection tables, empty, on the throwaway Postgres."""
    url = urlparse(DSN)
    monkeypatch.setenv("POSTGRES_HOST", url.hostname)
    monkeypatch.setenv("POSTGRES_PORT", str(url.port))
    monkeypatch.setenv("POSTGRES_DB", url.path.lstrip("/"))
    monkeypatch.setenv("POSTGRES_USER", url.username)
    monkeypatch.setenv("POSTGRES_PASSWORD", url.password)
    monkeypatch.setenv("POSTGRES_SSL_MODE", "disable")
    from core.storage import connection

    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    manager = connection.get_db_manager()
    manager.initialize(force=True)
    manager.create_tables()
    with manager.session_scope() as session:
        session.execute(
            text("TRUNCATE detection_rules RESTART IDENTITY CASCADE"),
        )
    yield manager
    manager._engine.dispose()
    connection.DatabaseManager._instance = None


@pytest.fixture
def lints(monkeypatch):
    """Each record sync lints, in order."""
    from core.detections.inventory import sync

    seen = []
    real = sync._lint

    def spy(record, now):
        seen.append(record.native_key)
        return real(record, now)

    monkeypatch.setattr(sync, "_lint", spy)
    return seen


SOURCE = "repo:test"


def _sync(records):
    from core.detections.inventory import sync

    return sync.sync_source(SOURCE, records)


def _lint_results(db):
    with db.session_scope() as session:
        rows = session.execute(
            text(
                "SELECT r.native_key, v.checks->'lint'->>'result' "
                "FROM detection_rules r JOIN detection_rule_versions v "
                "ON v.rule_id = r.id AND v.version = r.latest_version"
            )
        ).all()
    return dict(rows)


RECORDS = [
    _sigma("good", SIGMA_CLEAN, techniques=("T1003",), tactics=("credential-access",)),
    _sigma("pinned", SIGMA_BAD, techniques=("T1649",), tactics=("persistence",)),
    _spl("pinned_spl", 'index=main dest="dc01.corp.local"', techniques=("T1649",)),
]


@needs_pg
def test_sync_lints_a_new_imported_rule_once(db, lints):
    _sync(RECORDS)
    assert sorted(lints) == ["good", "pinned", "pinned_spl"]
    assert _lint_results(db) == {
        "good": "pass",
        "pinned": "fail",
        "pinned_spl": "fail",
    }
    lints.clear()
    _sync(RECORDS)
    assert lints == []


@needs_pg
def test_sync_lints_a_stored_version_never_linted_once(db, lints):
    _sync(RECORDS)
    with db.session_scope() as session:
        session.execute(text("UPDATE detection_rule_versions SET checks = '{}'"))
    lints.clear()
    _sync(RECORDS)
    assert sorted(lints) == ["good", "pinned", "pinned_spl"]
    assert _lint_results(db)["pinned"] == "fail"
    lints.clear()
    _sync(RECORDS)
    assert lints == []


@needs_pg
def test_failing_rules_are_no_coverage_in_the_catalog_counts(db):
    from core.detections.inventory import catalog

    _sync(RECORDS)
    assert catalog.rules_by_technique().keys() == {"T1003"}
    assert catalog.rules_by_tactic() == {"credential-access": 1}
    assert catalog.techniques_per_language() == {"sigma": 1}


@needs_pg
def test_lint_failing_is_in_by_tool(db):
    from core.detections.inventory import catalog

    _sync(RECORDS)
    by_tool = catalog.rules_for_technique("T1649")["by_tool"]
    assert by_tool == {"repo": {"rules": 2, "lint_failing": 2}}


@needs_pg
def test_analyze_coverage_counts_only_credited_rules(db):
    import asyncio

    from core.detections.tools import SecurityDetectionsTools

    _sync(RECORDS)
    tools = SecurityDetectionsTools()
    out = asyncio.run(tools.analyze_coverage(techniques=["T1649", "T1003"]))
    assert out["T1649"]["count"] == 0
    assert out["T1649"]["by_source"] == {}
    assert out["T1649"]["by_tool"]["repo"]["lint_failing"] == 2
    assert out["T1003"]["count"] == 1
    assert out["T1003"]["by_source"] == {"sigma": 1}
