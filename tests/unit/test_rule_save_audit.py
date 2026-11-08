"""A rule saved through Vigil leaves a trail entry naming who saved it (#4097).

The agent tool path (a run's signed claims) and a signed-in person each write
one ``detections.rule_saved`` entry per copy and version, in the sync's own
transaction. A file put in the overlay folder by hand gets none, so the two are
told apart; an unchanged re-save makes no version and no entry.

Real Postgres: set VIGIL_TEST_PG_DSN to a throwaway database. On a database
with the audit trail in use the entry is a trail entry; on a fresh create_all
database it is a ``config_audit_log`` row of the same fields.
"""

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from sqlalchemy import text

os.environ.setdefault("DEV_MODE", "true")

from core.audit import trail, words  # noqa: E402
from core.auth.tool_principal import Claims, bound_claims  # noqa: E402
from core.detections import overlay  # noqa: E402
from core.detections.inventory import sync  # noqa: E402
from core.integrations.mcp.surface import acting_as  # noqa: E402

DSN = os.environ.get("VIGIL_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="VIGIL_TEST_PG_DSN not set")

SPLUNK = """name: Encoded PowerShell
id: 11111111-1111-4111-8111-111111111111
description: x
search: index=win EventCode=4688 CommandLine="*-enc*" | stats count by host
tags:
  mitre_attack_id: [T1059.001]
"""
SIGMA = """title: Encoded PowerShell
id: 22222222-2222-4222-8222-222222222222
status: test
logsource: {product: windows, category: process_creation}
detection:
  sel: {CommandLine|contains: '-enc'}
  condition: sel
"""
SPLUNK_V2 = SPLUNK.replace("*-enc*", "*-encodedcommand*")
DROPPED = SPLUNK.replace("11111111-1111", "33333333-3333").replace(
    "Encoded PowerShell", "Dropped by hand"
)


@pytest.fixture
def db(monkeypatch, tmp_path):
    url = urlparse(DSN)
    for key, value in {
        "POSTGRES_HOST": url.hostname,
        "POSTGRES_PORT": str(url.port),
        "POSTGRES_DB": url.path.lstrip("/"),
        "POSTGRES_USER": url.username,
        "POSTGRES_PASSWORD": url.password,
        "POSTGRES_SSL_MODE": "disable",
        "VIGIL_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    from core.storage import connection

    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    manager = connection.get_db_manager()
    manager.initialize(force=True)
    manager.create_tables()
    with manager.session_scope() as session:
        # A throwaway database: fail rather than hang on a lock.
        session.execute(text("SET LOCAL lock_timeout = '10s'"))
        session.execute(text("SET LOCAL statement_timeout = '60s'"))
        session.execute(text("TRUNCATE detection_rules RESTART IDENTITY CASCADE"))
        session.execute(text("DELETE FROM config_audit_log"))
        if trail.in_use(session):
            # The trail is append-only for the app role; a superuser may clear
            # the saves of an earlier test so each starts with none.
            session.execute(text("SET LOCAL session_replication_role = replica"))
            session.execute(
                text(
                    "DELETE FROM audit_trail "
                    "WHERE payload->>'action' = 'detections.rule_saved'"
                )
            )
    monkeypatch.setattr(
        "core.agents.attribution.principal_id_for_username",
        lambda name: {"ana": "p-ana"}.get(name),
    )
    yield manager
    manager._engine.dispose()
    connection.DatabaseManager._instance = None


def _saves(db):
    """Every save entry, as one dict each, from the trail or the config log."""
    with db.session_scope() as session:
        if trail.in_use(session):
            rows = session.execute(
                text(
                    "SELECT payload FROM audit_trail "
                    "WHERE payload->>'action' = 'detections.rule_saved' ORDER BY seq"
                )
            ).all()
            return [r[0] for r in rows]
        rows = session.execute(
            text(
                "SELECT new_value, changed_by FROM config_audit_log "
                "WHERE config_type = 'detection_rule_save' ORDER BY id"
            )
        ).all()
        return [{**v, "actor": who} for v, who in rows]


def _by_language(entries):
    return {e["language"]: e for e in entries}


def _agent(**more):
    claims = dict(
        username=None,
        person_id=None,
        agent_id="agent:detect",
        run_id="run-9",
        origin="alert",
    )
    return bound_claims(Claims(**{**claims, **more}))


def test_an_agent_save_names_its_run_and_agent_per_copy(db):
    with _agent():
        result = overlay.save_rule(splunk_yaml=SPLUNK, sigma_yaml=SIGMA)
    assert not result.get("error"), result
    found = _by_language(_saves(db))
    assert set(found) == {"splunk", "sigma"}
    for language, key in (
        ("splunk", "11111111-1111-4111-8111-111111111111"),
        ("sigma", "22222222-2222-4222-8222-222222222222"),
    ):
        entry = found[language]
        assert entry["by"] == "agent"
        assert entry["actor"] == "agent:detect"
        assert (entry["agent_id"], entry["run_id"]) == ("agent:detect", "run-9")
        assert entry["native_key"] == key and entry["version"] == 1
        assert entry["rule_id"] == int(entry["object"])
        assert len(entry["fingerprint"]) == 64
    assert len(_saves(db)) == 2


@pytest.mark.unbound
def test_a_persons_save_names_their_principal_id(db):
    with acting_as("ana"):
        overlay.save_rule(splunk_yaml=SPLUNK)
    (entry,) = _saves(db)
    assert entry["by"] == "person" and entry["actor"] == "p-ana"
    assert "run_id" not in entry and "agent_id" not in entry
    assert entry["language"] == "splunk" and entry["version"] == 1


def test_a_person_in_a_chat_run_is_the_actor_and_the_agent_is_named(db):
    with _agent(username="ana", person_id="p-ana", origin="person"):
        with acting_as("ana"):
            overlay.save_rule(splunk_yaml=SPLUNK)
    (entry,) = _saves(db)
    assert entry["actor"] == "p-ana" and entry["by"] == "agent"
    assert entry["run_id"] == "run-9"


@pytest.mark.unbound
def test_a_new_version_gets_its_own_entry(db):
    with _agent():
        overlay.save_rule(splunk_yaml=SPLUNK)
    with acting_as("ana"):
        overlay.save_rule(splunk_yaml=SPLUNK_V2)
    first, second = _saves(db)
    assert (first["version"], first["by"]) == (1, "agent")
    assert (second["version"], second["by"]) == (2, "person")
    assert first["fingerprint"] != second["fingerprint"]


def test_saving_the_same_content_again_makes_no_version_and_no_entry(db):
    with _agent():
        overlay.save_rule(splunk_yaml=SPLUNK)
        again = overlay.save_rule(splunk_yaml=SPLUNK)
    assert again["saved"][0]["version"] == 1
    assert len(_saves(db)) == 1


def test_a_file_dropped_by_hand_has_no_save_entry(db, tmp_path):
    with _agent():
        overlay.save_rule(splunk_yaml=SPLUNK)
    service = overlay.DetectionRulesService()
    source = service.overlay_source()
    target = Path(source["local_path"], "splunk", "dropped.yml")
    target.write_text(DROPPED, encoding="utf-8")
    report = sync._sync_one(service, service.overlay_source())
    assert report.error is None and report.new == 1
    with db.session_scope() as session:
        keys = dict(
            session.execute(text("SELECT native_key, id FROM detection_rules")).all()
        )
    assert "33333333-3333-4111-8111-111111111111" in keys
    saved = {e["native_key"] for e in _saves(db)}
    # The saved rule is recognisable by its entry; the dropped one is not.
    assert saved == {"11111111-1111-4111-8111-111111111111"}


def test_an_edited_file_is_a_new_version_with_no_save_entry(db):
    with _agent():
        overlay.save_rule(splunk_yaml=SPLUNK)
    service = overlay.DetectionRulesService()
    path = Path(service.overlay_source()["local_path"]) / (
        "splunk/11111111-1111-4111-8111-111111111111.yml"
    )
    path.write_text(SPLUNK_V2, encoding="utf-8")
    sync._sync_one(service, service.overlay_source())
    with db.session_scope() as session:
        latest = session.execute(
            text("SELECT latest_version FROM detection_rules")
        ).scalar()
    assert latest == 2
    assert [e["version"] for e in _saves(db)] == [1]


def test_the_entry_reads_as_a_sentence():
    entry = {
        "action": "detections.rule_saved",
        "rule_id": 4,
        "version": 2,
        "language": "splunk",
    }
    assert words.sentence(entry) == "Saved rule 4 v2 (splunk copy)"
