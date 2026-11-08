"""When the customer's Wazuh manager loads a Vigil rule (its PR merged), the
manager's rule is linked to it by id and the overlay copy is handed off, as a
Kibana rule or Splunk saved search takes over an Elastic or Splunk copy: it is
no longer offered for deploy and the two count as one rule."""

import os
import random
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from sqlalchemy import text

from core.detections.inventory import rollout, sync, wazuh
from core.storage.connection import get_db_manager


def test_the_wazuh_link_is_by_id_on_the_managers_rules(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        sync,
        "_link",
        lambda *args, **kw: seen.update(args=args, kw=kw) or (0, 0),
    )
    sync._link_wazuh_id()
    source, language, kind, key_of, _origin_key, _reason = seen["args"][:6]
    assert (source, language, kind) == (wazuh.SOURCE, "wazuh", "same_id")
    assert key_of(SimpleNamespace(native_key="100100")) == "100100"
    assert seen["kw"]["audited"] is True
    overlay_ok = seen["kw"]["overlay_ok"]
    assert overlay_ok(
        SimpleNamespace(native_key="100100", path="etc/rules/vigil_100100.xml")
    )
    assert overlay_ok(SimpleNamespace(native_key="100100", path="vigil_100100.xml"))
    for path in (
        "etc/rules/local_rules.xml",
        "etc/rules/xvigil_100100.xml",
        "etc/rules/vigil_100101.xml",
        None,
    ):
        assert not overlay_ok(SimpleNamespace(native_key="100100", path=path)), path


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

    vigil_file = f"etc/rules/vigil_{key}.xml"

    def rule(source, status="active", path="vigil"):
        # A manager rule is loaded from Vigil's merged file unless told otherwise.
        path = (vigil_file if source == "wazuh" else None) if path == "vigil" else path
        with manager.session_scope() as session:
            rid = session.execute(
                text(
                    "INSERT INTO detection_rules (source, native_key, name, language,"
                    " status, latest_version, path) VALUES (:s, :k, 'Handoff probe',"
                    " 'wazuh', :st, 1, :p) RETURNING id"
                ),
                {"s": source, "k": key, "st": status, "p": path},
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

    from core.audit import trail

    # The audit stores are never emptied, and rule ids restart: read only this test's rows.
    with manager.session_scope() as session:
        on_trail = trail.in_use(session)
        newest = session.execute(
            text(
                "SELECT coalesce(max(seq), 0) FROM audit_trail"
                if on_trail
                else "SELECT coalesce(max(id), 0) FROM config_audit_log"
            )
        ).scalar()

    def audited(rid):
        """(verb, actor, old origin, new origin) of each link audit row for ``rid``."""
        with manager.session_scope() as session:
            if on_trail:
                rows = session.execute(
                    text(
                        "SELECT payload->>'verb', payload->>'actor',"
                        " payload->'old_value'->>'origin_rule_id',"
                        " payload->'new_value'->>'origin_rule_id'"
                        " FROM audit_trail WHERE payload->>'action' = 'config.change'"
                        " AND payload->>'config_key' = :k AND seq > :n ORDER BY seq"
                    ),
                    {"k": f"detection_rule:{rid}", "n": newest},
                ).all()
            else:
                rows = session.execute(
                    text(
                        "SELECT action, changed_by, old_value->>'origin_rule_id',"
                        " new_value->>'origin_rule_id' FROM config_audit_log"
                        " WHERE config_key = :k AND id > :n ORDER BY id"
                    ),
                    {"k": f"detection_rule:{rid}", "n": newest},
                ).all()
        return [tuple(r) for r in rows]

    yield SimpleNamespace(rule=rule, origin_of=origin_of, key=key, audited=audited)
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


@pytest.mark.parametrize(
    "path",
    [
        "etc/rules/local_rules.xml",
        "etc/rules/xvigil_{key}.xml",
        "etc/rules/vigil_{key}0.xml",
        "etc/rules/vigil-{key}.xml",
        "etc/rules/vigilX{key}.xml",
        "vigil-{key}.xml",
        None,
    ],
    ids=[
        "local-rules",
        "prefixed",
        "other-id",
        "dash",
        "any-char",
        "dash-alone",
        "no-path",
    ],
)
def test_a_customer_file_loading_the_id_takes_nothing_over(pg, path):
    overlay = pg.rule("repo:overlay")
    loaded = pg.rule("wazuh", path=path and path.format(key=pg.key))
    sync._link_wazuh_id()
    assert tuple(pg.origin_of(loaded)) == (None, None)
    assert rollout.handed_off([overlay]) == set()
    assert pg.audited(loaded) == []


@pytest.mark.parametrize(
    "path",
    ["etc/rules/vigil-{key}.xml", "etc/rules/vigilX{key}.xml", "vigil-{key}.xml"],
    ids=["dash", "any-char", "dash-alone"],
)
def test_a_near_named_customer_file_never_hands_the_vigil_copy_off(pg, path):
    # The customer's repo rule carries the id, so the manager rule links to it;
    # its file is not Vigil's, whatever "_" a LIKE would have matched.
    overlay = pg.rule("repo:overlay")
    repo = pg.rule("repo:customer-wazuh")
    loaded = pg.rule("wazuh", path=path.format(key=pg.key))
    sync._link_wazuh_id()
    assert tuple(pg.origin_of(loaded)) == (repo, "same_id")
    assert rollout.handed_off([overlay]) == set()


def test_a_customers_own_rule_links_to_its_repo_rule_but_takes_no_vigil_copy(pg):
    # The customer's local_rules.xml rule is its repo rule's SIEM copy; the
    # Vigil copy with the id is another rule and stays deployable.
    overlay = pg.rule("repo:overlay")
    repo = pg.rule("repo:customer-wazuh")
    loaded = pg.rule("wazuh", path="etc/rules/local_rules.xml")
    sync._link_wazuh_id()
    assert tuple(pg.origin_of(loaded)) == (repo, "same_id")
    assert rollout.handed_off([overlay]) == set()


def test_each_wazuh_link_change_is_audited(pg):
    overlay = pg.rule("repo:overlay")
    loaded = pg.rule("wazuh")
    sync._link_wazuh_id()
    sync._link_wazuh_id()  # unchanged: no second row
    assert pg.audited(loaded) == [("link", "system", None, str(overlay))]
    with get_db_manager().session_scope() as session:
        session.execute(
            text(
                "UPDATE detection_rules SET path = 'etc/rules/local_rules.xml' WHERE id = :i"
            ),
            {"i": loaded},
        )
    sync._link_wazuh_id()
    assert pg.audited(loaded)[-1] == ("unlink", "system", str(overlay), None)
    assert rollout.handed_off([overlay]) == set()
