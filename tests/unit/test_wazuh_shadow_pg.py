"""Wazuh shadow runs through the real sync, runner and sweep on Postgres with the
audit trail in use. Set VIGIL_TEST_PG_DSN to a throwaway database restored from a
Vigil database that has the trail (postgresql://postgres:t@127.0.0.1:55461/t)."""

import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import pytest
from sqlalchemy import event, select, text, update

from tests.unit.test_wazuh_shadow import _rule, _Service

DSN = os.environ.get("VIGIL_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="VIGIL_TEST_PG_DSN not set")

GOOD = _rule("<decoded_as>sshd</decoded_as>")
CHAIN = _rule("<if_sid>5700</if_sid><decoded_as>sshd</decoded_as>")


@pytest.fixture
def db(monkeypatch):
    url = urlparse(DSN)
    for key, value in {
        "HOST": url.hostname,
        "PORT": str(url.port),
        "DB": url.path.lstrip("/"),
        "USER": url.username,
        "PASSWORD": url.password,
        "SSL_MODE": "disable",
    }.items():
        monkeypatch.setenv(f"POSTGRES_{key}", value)
    from core.storage import connection

    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    manager = connection.get_db_manager()
    manager.initialize(force=True)
    manager.create_tables()

    @event.listens_for(manager._engine, "connect")
    def _timeouts(conn, _record):
        cur = conn.cursor()
        cur.execute("SET lock_timeout = '5s'; SET statement_timeout = '30s'")
        cur.close()

    from core.audit.trail import in_use

    with manager.session_scope() as session:
        trail = in_use(session)
    if not trail:
        manager._engine.dispose()
        connection.DatabaseManager._instance = None
        pytest.skip("needs a database with the audit trail in use")
    with manager.session_scope() as session:
        session.execute(text("TRUNCATE detection_rules RESTART IDENTITY CASCADE"))
        session.execute(text("DELETE FROM siem_searches"))
        session.execute(
            text("DELETE FROM approval_actions WHERE action_type = 'promote_rule'")
        )
    _forget_settings(manager)
    yield manager
    _forget_settings(manager)
    manager._engine.dispose()
    connection.DatabaseManager._instance = None


def _forget_settings(manager):
    """The stored settings the sweep reads (the rollout limits, the autonomy
    ceiling), gone before and after each test: an earlier test's limits must
    not hold a copy in shadow here, nor this test's change a later one."""
    with manager.session_scope() as session:
        session.execute(
            text(
                "DELETE FROM system_config "
                "WHERE key IN ('autonomy.ceiling', 'rollout.limits')"
            )
        )


@pytest.fixture
def lab(db, monkeypatch):
    from core.detections.inventory import readers, rollout, sync

    ids = {}
    for name, xml, key in (("good", GOOD, "100100"), ("chain", CHAIN, "100101")):
        record = readers._wazuh(xml.replace("100100", key), f"wazuh/{key}.xml")
        record.native_key = key
        ids[name] = record
    sync.sync_source(readers.OVERLAY_SOURCE, list(ids.values()))
    monkeypatch.setattr(rollout, "_stopped", lambda: False)
    monkeypatch.setattr(rollout, "_deploy_targets", lambda: {})
    monkeypatch.setattr(
        rollout.wazuh_sees, "indexer", lambda: (None, {"max_searches_per_hour": 30})
    )
    return rollout


def _run(lab, monkeypatch, service):
    monkeypatch.setattr(lab.kibana, "service_of", lambda config: service)
    return lab.run_copies(live=False)


def _by_key(lab):
    return {c.key: c for c in lab._copies()}


def _age_a_clean_week(db, lab, key):
    """Eight days in shadow and a clean week of passing runs for rule ``key``."""
    from core.storage.models import (
        DetectionRule,
        DetectionRuleStage,
        DetectionRuleVersion,
    )

    now = datetime.now(timezone.utc)
    week = lab._week(now)
    with db.session_scope() as session:
        rid = session.scalar(
            select(DetectionRule.id).where(DetectionRule.native_key == key)
        )
        session.execute(
            update(DetectionRuleStage)
            .where(DetectionRuleStage.rule_id == rid)
            .values(at=now - timedelta(days=8))
        )
        checks = session.scalar(
            select(DetectionRuleVersion.checks).where(
                DetectionRuleVersion.rule_id == rid
            )
        )
        checks = {
            **checks,
            "runs": {
                **checks["runs"],
                "days": {d: {"pass": 3, "fail": 0} for d in week},
            },
            "shadow": {**checks.get("shadow", {}), "days": {d: 1 for d in week}},
        }
        session.execute(
            update(DetectionRuleVersion)
            .where(DetectionRuleVersion.rule_id == rid)
            .values(checks=checks)
        )
    return now


def test_a_shadow_wazuh_copy_is_run_counted_and_promoted_but_a_chain_is_not(
    db, lab, monkeypatch
):
    service = _Service(total=4)
    report = _run(lab, monkeypatch, service)
    copies = _by_key(lab)
    good, chain = copies["100100"], copies["100101"]
    assert (good.stage, chain.stage) == ("shadow", "shadow")
    today = datetime.now(timezone.utc).date().isoformat()
    assert good.checks["runs"]["result"] == "pass"
    assert good.checks["runs"]["days"][today] == {"pass": 1, "fail": 0}
    assert good.checks["shadow"]["days"][today] == 4
    assert good.checks["sees"]["result"] == "pass"
    assert chain.checks["runs"]["result"] == "unknown"
    assert (
        "can't validate: <if_sid> depends on another rule"
        in chain.checks["runs"]["reason"]
    )
    assert chain.checks["sees"]["result"] == "unknown"
    assert "shadow" not in chain.checks
    assert len(service.searched) == 1 and report.searched == 2

    now = _age_a_clean_week(db, lab, "100100")
    _age_a_clean_week(db, lab, "100101")
    monkeypatch.setattr(lab, "_asks_first", lambda: False)  # Act
    lab.sweep(now, lab.RolloutReport())
    after = _by_key(lab)
    assert after["100100"].stage == "live"
    assert after["100101"].stage == "shadow"


def test_under_assist_a_validated_wazuh_copy_is_asked_for_not_promoted(
    db, lab, monkeypatch
):
    from core.storage.models import ApprovalAction

    _run(lab, monkeypatch, _Service(total=4))
    now = _age_a_clean_week(db, lab, "100100")
    _age_a_clean_week(db, lab, "100101")
    monkeypatch.setattr(lab, "_asks_first", lambda: True)  # Assist
    report = lab.RolloutReport()
    lab.sweep(now, report)
    after = _by_key(lab)
    assert after["100100"].stage == "shadow"
    assert after["100101"].stage == "shadow"
    assert report.asked == [f"{after['100100'].rule_id} v1"]
    with db.session_scope() as session:
        asked = session.scalars(
            select(ApprovalAction).where(ApprovalAction.action_type == "promote_rule")
        ).all()
        assert [a.parameters.get("rule_id") for a in asked] == [after["100100"].rule_id]
        assert all(a.status == "pending" for a in asked)


def test_with_the_archive_off_the_copy_stays_in_shadow_cant_validate(
    db, lab, monkeypatch
):
    _run(lab, monkeypatch, _Service())
    now = _age_a_clean_week(db, lab, "100100")
    # The archive goes away: the week's passes are history, the rule is not validated now.
    _run(lab, monkeypatch, _Service(names=()))
    good = _by_key(lab)["100100"]
    assert good.checks["runs"]["result"] == "unknown"
    assert good.checks["runs"]["reason"].startswith("can't validate: archives off")
    assert good.checks["sees"]["result"] == "unknown"
    lab.sweep(now, lab.RolloutReport())
    assert _by_key(lab)["100100"].stage == "shadow"
