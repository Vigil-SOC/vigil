"""The ATT&CK coverage heat map (#4103): each technique's state (proven when an
active SIEM copy passes Runs and Sees), its split by SIEM, a rule counted once
however many copies it has, and the same numbers the Rules page and
siem_checks_by_technique give for the same rows."""

import os
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from core.detections.inventory import catalog, coverage


def _copy(cid, source, techniques, runs=None, sees=None, language="splunk", **kw):
    return SimpleNamespace(
        id=cid,
        status=kw.get("status", "active"),
        source=source,
        language=language,
        techniques=techniques,
        tactics=kw.get("tactics"),
        runs=runs,
        sees=sees,
        replay=kw.get("replay"),
    )


@pytest.fixture
def inventory(monkeypatch):
    box = {"copies": [], "seen": {}, "rule_of": {}}

    class _Db:
        @contextmanager
        def session_scope(self):
            yield None

    monkeypatch.setattr(coverage, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(coverage, "_copies", lambda s: box["copies"])
    monkeypatch.setattr(coverage, "_seen", lambda s: box["seen"])
    monkeypatch.setattr(catalog, "_rule_of_copy", lambda s: box["rule_of"])
    return box


def _row(out, technique):
    return next(r for r in out["techniques"] if r["technique"] == technique)


def test_a_technique_is_proven_by_a_siem_copy_passing_runs_and_sees(inventory):
    inventory["copies"] = [
        _copy(1, "elastic", ["T1059.001"], "pass", "pass", language="kuery"),
        _copy(2, "splunk", ["T1059.001"], "pass", "fail"),
        _copy(3, "splunk", ["T1003"], "pass", "unknown"),
        _copy(4, "repo:acme", ["T1021"]),
    ]
    inventory["seen"] = {"T1566": 4}
    out = coverage.coverage()
    assert _row(out, "T1059.001")["state"] == "proven"
    assert _row(out, "T1003")["state"] == "unproven"  # Sees unknown is not proven
    assert _row(out, "T1021")["state"] == "unproven"  # a repo rule, no SIEM copy
    assert _row(out, "T1566")["state"] == "no_rule"  # alerts only
    assert out["summary"] == {"proven": 1, "unproven": 2, "no_rule": 1}


def test_each_technique_is_split_by_siem(inventory):
    inventory["copies"] = [
        _copy(1, "elastic", ["T1059.001"], "pass", "pass", language="kuery"),
        _copy(2, "splunk", ["T1059.001"], "pass", "fail"),
        _copy(3, "splunk", ["T1059.001"], "pass", "pass"),
        _copy(4, "wazuh", ["T1003"], "fail", None, language="wazuh"),
    ]
    out = coverage.coverage()
    assert _row(out, "T1059.001")["siems"] == {
        "elastic": {"copies": 1, "proven": 1, "failing": 0, "state": "proven"},
        "splunk": {"copies": 2, "proven": 1, "failing": 1, "state": "proven"},
    }
    assert _row(out, "T1003")["siems"] == {
        "wazuh": {"copies": 1, "proven": 0, "failing": 1, "state": "unproven"}
    }


def test_a_rule_counts_once_however_many_copies_it_has(inventory):
    # A Kibana copy linked to its repo origin, and the overlay's two copies of one rule.
    inventory["copies"] = [
        _copy(1, "repo:acme", ["T1059"]),
        _copy(2, "elastic", ["T1059"], "pass", "pass", language="kuery"),
        _copy(3, "repo:overlay", ["T1059"], language="splunk"),
        _copy(4, "repo:overlay", ["T1059"], language="elastic"),
    ]
    inventory["rule_of"] = {
        1: (1,),
        2: (1,),
        3: ("repo:overlay", "x"),
        4: ("repo:overlay", "x"),
    }
    out = coverage.coverage()
    assert _row(out, "T1059")["rules"] == 2
    assert out["rule_total"] == 2


def test_techniques_sit_in_attack_tactic_columns(inventory):
    inventory["copies"] = [
        _copy(1, "splunk", ["T1003"]),
        _copy(2, "splunk", ["T9999"], tactics=["Persistence"]),  # not in ATT&CK
        _copy(3, "splunk", ["T9998"]),
    ]
    out = coverage.coverage()
    assert _row(out, "T1003")["tactic"] == "Credential Access"
    assert _row(out, "T9999")["tactic"] == "Persistence"
    assert _row(out, "T9998")["tactic"] == coverage.UNMAPPED_TACTIC
    assert out["tactics"] == ["Persistence", "Credential Access", "Unmapped"]


def test_a_disabled_rule_is_counted_but_is_no_coverage(inventory):
    inventory["copies"] = [
        _copy(1, "splunk", ["T1003"], "pass", "pass", status="disabled"),
        _copy(2, "splunk", ["T1059"], "pass", "pass"),
        _copy(3, "splunk", ["T1059"], status="disabled"),
    ]
    out = coverage.coverage()
    assert (_row(out, "T1003")["state"], _row(out, "T1003")["disabled"]) == (
        "no_rule",
        1,
    )
    assert _row(out, "T1003")["siems"] == {}
    assert (_row(out, "T1059")["rules"], _row(out, "T1059")["disabled"]) == (1, 1)
    assert (out["rule_total"], out["mapped"]) == (1, 3)


def test_a_passing_replay_is_a_detail_not_proof(inventory):
    inventory["copies"] = [
        _copy(1, "repo:overlay", ["T1218.011"], language="sigma", replay="pass")
    ]
    row = _row(coverage.coverage(), "T1218.011")
    assert (row["state"], row["replayed"]) == ("unproven", 1)


def test_the_route_returns_the_map(inventory):
    from services.api.routers.detection_rules import get_coverage

    inventory["copies"] = [_copy(1, "splunk", ["T1003"], "pass", "pass")]
    assert get_coverage()["summary"]["proven"] == 1


# --- real Postgres: the same numbers as the Rules page and siem_checks ---------

DSN = os.environ.get("VIGIL_TEST_PG_DSN")


@pytest.fixture
def pg(monkeypatch):
    """The detection tables, emptied, on the throwaway Postgres (set POSTGRES_* to match)."""
    if not DSN:
        pytest.skip("VIGIL_TEST_PG_DSN not set")
    from sqlalchemy import text

    from core.storage import connection

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


def _seed(manager):
    from core.storage.models import DetectionRule, DetectionRuleVersion

    rows = [
        # (source, language, techniques, runs, sees, status, link to rule index)
        ("repo:acme", "kuery", ["T1059.001"], None, None, "active", None),
        ("elastic", "kuery", ["T1059.001"], "pass", "pass", "active", 0),
        ("splunk", "splunk", ["T1059.001", "T1003"], "pass", "fail", "active", None),
        ("splunk", "splunk", ["T1003"], "pass", "pass", "active", None),
        ("wazuh", "wazuh", ["T1021"], "fail", None, "active", None),
        ("wazuh", "wazuh", ["T1021"], "pass", "pass", "disabled", None),
        ("repo:overlay", "splunk", ["T1218"], None, None, "active", None),
        ("repo:overlay", "elastic", ["T1218"], None, None, "active", None),
    ]
    ids = []
    with manager.session_scope() as session:
        for i, (source, language, techniques, runs, sees, status, link) in enumerate(
            rows
        ):
            checks = {
                k: {"result": v} for k, v in (("runs", runs), ("sees", sees)) if v
            }
            rule = DetectionRule(
                source=source,
                native_key=f"k{i}",
                name="Overlay rule" if source == "repo:overlay" else f"Rule {i}",
                language=language,
                status=status,
                latest_version=1,
                techniques=techniques,
                origin_rule_id=ids[link] if link is not None else None,
                link_kind="same_id" if link is not None else None,
            )
            session.add(rule)
            session.flush()
            session.add(
                DetectionRuleVersion(
                    rule_id=rule.id,
                    version=1,
                    fingerprint=f"f{i}",
                    content={},
                    checks=checks,
                )
            )
            ids.append(rule.id)


def test_the_map_agrees_with_siem_checks_and_the_rules_page(pg):
    _seed(pg)
    out = coverage.coverage()
    checks = catalog.siem_checks_by_technique()
    for row in out["techniques"]:
        mine = checks.get(row["technique"], {"rules": 0, "proven": 0, "failing": 0})
        split = row["siems"].values()
        assert sum(s["copies"] for s in split) == mine["rules"], row
        assert sum(s["proven"] for s in split) == mine["proven"], row
        assert sum(s["failing"] for s in split) == mine["failing"], row
        assert (row["state"] == "proven") == (mine["proven"] > 0), row
    assert {r["technique"]: r["state"] for r in out["techniques"]} == {
        "T1059.001": "proven",
        "T1003": "proven",
        "T1021": "unproven",  # the passing Wazuh copy is disabled
        "T1218": "unproven",
    }
    assert _row(out, "T1021")["disabled"] == 1
    # One rule per linked pair and per overlay name, as the Rules page counts
    # active rules (its own total also counts the disabled copy).
    assert _row(out, "T1059.001")["rules"] == 2
    assert _row(out, "T1218")["rules"] == 1
    listed = catalog.health_listing()
    assert out["mapped"] == listed["mapped"]
    assert out["rule_total"] == listed["rule_total"] - 1
