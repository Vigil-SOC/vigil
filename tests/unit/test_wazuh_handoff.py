"""When the customer's Wazuh manager loads a Vigil rule (its PR merged), the
manager's rule is linked to it by id and the overlay copy is handed off, as a
Kibana rule or Splunk saved search takes over an Elastic or Splunk copy: it is
no longer offered for deploy and the two count as one rule."""

import os
import random
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from core.detections.inventory import rollout, sync, wazuh


def test_the_wazuh_link_is_by_id_on_the_managers_rules(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        sync, "_link", lambda *args: seen.setdefault("args", args) and (0, 0)
    )
    sync._link_wazuh_id()
    source, language, kind, key_of, _origin_key, _reason = seen["args"]
    assert (source, language, kind) == (wazuh.SOURCE, "wazuh", "same_id")
    assert key_of(SimpleNamespace(native_key="100100")) == "100100"


@pytest.mark.parametrize(
    "report, links",
    [
        (sync.SourceReport(source="wazuh"), True),
        (sync.SourceReport(source="wazuh", skipped="not enabled"), False),
        (sync.SourceReport(source="wazuh", error="unreachable"), False),
    ],
    ids=["read", "skipped", "unread"],
)
def test_the_sync_links_wazuh_rules_only_after_reading_them(monkeypatch, report, links):
    linked = []
    monkeypatch.setattr(sync, "_sync_wazuh", lambda: report)
    monkeypatch.setattr(sync, "_link_wazuh_id", lambda: linked.append(1) or (2, 1))
    out = sync._wazuh_synced()
    assert bool(linked) is links
    assert (out.linked, out.unlinked) == ((2, 1) if links else (0, 0))


# --- real Postgres: the link and hand-off queries -----------------------------

DSN = os.environ.get("VIGIL_TEST_PG_DSN")


@pytest.fixture
def pg(monkeypatch):
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
    key = str(random.randint(119000, 119999))
    made = []

    def rule(source, status="active"):
        with manager.session_scope() as session:
            rid = session.execute(
                text(
                    "INSERT INTO detection_rules (source, native_key, name, language,"
                    " status, latest_version) VALUES (:s, :k, 'Handoff probe', 'wazuh',"
                    " :st, 1) RETURNING id"
                ),
                {"s": source, "k": key, "st": status},
            ).scalar()
        made.append(rid)
        return rid

    def origin_of(rid):
        with manager.session_scope() as session:
            return session.execute(
                text(
                    "SELECT origin_rule_id, link_kind FROM detection_rules WHERE id=:i"
                ),
                {"i": rid},
            ).one()

    yield SimpleNamespace(rule=rule, origin_of=origin_of, key=key)
    with manager.session_scope() as session:
        session.execute(
            text(
                "UPDATE detection_rules SET origin_rule_id = NULL, link_kind = NULL,"
                " linked_by = NULL, link_reason = NULL, linked_at = NULL"
                " WHERE id = ANY(:i)"
            ),
            {"i": made},
        )
        session.execute(
            text("DELETE FROM detection_rules WHERE id = ANY(:i)"), {"i": made}
        )


def test_the_managers_rule_links_to_the_overlay_rule_and_hands_it_off(pg):
    overlay = pg.rule("repo:overlay")
    loaded = pg.rule("wazuh")
    assert rollout.handed_off([overlay]) == set()
    sync._link_wazuh_id()
    assert tuple(pg.origin_of(loaded)) == (overlay, "same_id")
    assert rollout.handed_off([overlay]) == {overlay}


def test_once_the_customers_repo_carries_the_rule_it_is_the_origin(pg):
    overlay = pg.rule("repo:overlay")
    repo = pg.rule("repo:customer-wazuh")
    loaded = pg.rule("wazuh")
    sync._link_wazuh_id()
    assert tuple(pg.origin_of(loaded)) == (repo, "same_id")
    # The overlay copy is still handed off: a repo rule of its language holds its key.
    assert rollout.handed_off([overlay]) == {overlay}


def test_a_disabled_manager_rule_takes_nothing_over(pg):
    overlay = pg.rule("repo:overlay")
    pg.rule("wazuh", status="disabled")
    sync._link_wazuh_id()
    assert rollout.handed_off([overlay]) == set()
