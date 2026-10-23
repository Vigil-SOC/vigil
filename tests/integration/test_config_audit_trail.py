"""Settings and integration changes commit with one trail entry (#2762).

Same scratch database as ``tests/integration/test_audit_trail.py``: provision,
then ``python -m core.upgrade``.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from core.audit import trail
from core.integrations.integration_secrets import redact_secrets
from core.storage.config_service import ConfigService
from tests.integration.test_upgrade_command import _run, _scratch

pytestmark = [pytest.mark.integration, pytest.mark.database]


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    with _scratch(False) as engine:
        code, report = _run(engine, tmp_path_factory.mktemp("up"), restore_point="rp")
        assert code == 0, report
        yield engine


@pytest.fixture
def db(engine, monkeypatch):
    factory = sessionmaker(bind=engine)

    @contextmanager
    def get_session():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr("core.storage.config_service.get_session", get_session)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE audit_trail, audit_trail_held, config_audit_log"))
        conn.execute(text("UPDATE audit_trail_anchor SET seq = 0, entry_hash = ''"))
        conn.execute(
            text("UPDATE alembic_version SET version_num = :v"),
            {"v": trail.REVISION},
        )
        conn.execute(text("DELETE FROM integration_configs"))
        conn.execute(text("DELETE FROM system_config WHERE key LIKE 'audit.t.%'"))
    return engine


def _payloads(engine):
    with engine.connect() as conn:
        return list(
            conn.execute(text("SELECT payload FROM audit_trail ORDER BY seq")).scalars()
        )


def _integration(engine, integration_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT config FROM integration_configs WHERE integration_id = :i"),
            {"i": integration_id},
        ).scalar_one()


def _system(engine, key):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT value FROM system_config WHERE key = :k"),
            {"k": key},
        ).scalar_one()


def test_integration_update_and_its_trail_row_commit_together(db):
    svc = ConfigService(user_id="user-7")
    assert svc.set_integration_config(
        "github", {"org": "old"}, integration_name="GitHub"
    )
    assert svc.set_integration_config("github", {"org": "new"}, change_reason="rename")

    assert _integration(db, "github") == {"org": "new"}
    payloads = _payloads(db)
    assert len(payloads) == 2
    update = payloads[-1]
    assert update["action"] == "config.change"
    assert update["actor"] == "user-7"
    assert update["verb"] == "update"
    assert update["config_type"] == "integration"
    assert update["config_key"] == "github"
    assert update["change_reason"] == "rename"
    assert update["old_value"] == {"org": "old"}
    assert update["new_value"] == {"org": "new"}
    with db.connect() as conn:
        assert (
            conn.execute(text("SELECT count(*) FROM config_audit_log")).scalar_one()
            == 0
        )
        beside = conn.execute(
            text(
                "SELECT source_ip, actor_text, operator_text, attempted_name, personal "
                "FROM audit_trail WHERE seq = 2"
            )
        ).one()
    assert list(beside) == [None, None, None, None, None]


def test_record_audit_commits_one_trail_entry(db):
    ConfigService(user_id="user-9").record_audit(
        "cases",
        "cases",
        "delete_all",
        {"count": 2},
        None,
        "cleared",
    )
    payloads = _payloads(db)
    assert len(payloads) == 1
    assert payloads[0]["actor"] == "user-9"
    assert payloads[0]["verb"] == "delete_all"
    assert payloads[0]["old_value"] == {"count": 2}
    assert payloads[0]["new_value"] is None
    assert payloads[0]["change_reason"] == "cleared"


def test_append_failure_leaves_the_config_rows_unchanged(db, monkeypatch):
    svc = ConfigService(user_id="user-7")
    assert svc.set_integration_config(
        "github", {"org": "kept"}, integration_name="GitHub"
    )
    assert svc.set_system_config("audit.t.system", {"name": "kept"})

    def _fail(*_args, **_kwargs):
        raise RuntimeError("forced")

    monkeypatch.setattr(trail, "append", _fail)
    assert (
        svc.set_integration_config(
            "github", {"org": "changed"}, integration_name="GitHub"
        )
        is False
    )
    assert svc.set_system_config("audit.t.system", {"name": "changed"}) is False

    assert _integration(db, "github") == {"org": "kept"}
    assert _system(db, "audit.t.system") == {"name": "kept"}
    assert [p["verb"] for p in _payloads(db)] == ["create", "create"]


def test_secrets_are_redacted_in_the_trail_and_kept_on_the_row(db):
    svc = ConfigService(user_id="user-7")
    integration = {"token": "ghp_secret", "org": "acme"}
    system = {"api_key": "super-secret", "nested": {"password": "pw"}, "host": "h"}
    assert svc.set_integration_config("github", integration, integration_name="GitHub")
    assert svc.set_system_config("audit.t.secret", system)

    assert integration["token"] == "ghp_secret"
    assert system["api_key"] == "super-secret"
    assert _integration(db, "github")["token"] == "ghp_secret"
    assert _system(db, "audit.t.secret")["api_key"] == "super-secret"
    assert _system(db, "audit.t.secret")["nested"]["password"] == "pw"

    by_key = {p["config_key"]: p for p in _payloads(db)}
    assert by_key["github"]["new_value"] == redact_secrets("github", integration)
    assert "token" not in by_key["github"]["new_value"]
    assert "ghp_secret" not in str(by_key["github"])
    redacted = by_key["audit.t.secret"]["new_value"]
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert redacted["host"] == "h"
    assert "super-secret" not in str(by_key["audit.t.secret"])


def test_stamped_head_without_the_trail_table_keeps_the_legacy_row(db):
    """create_all plus stamp_schema_head names the head revision and has no table."""
    with db.begin() as conn:
        conn.execute(text("ALTER TABLE audit_trail RENAME TO audit_trail_hidden"))
    try:
        ConfigService(user_id="runner").record_audit(
            "backup",
            "restore",
            "restore",
            None,
            {"snapshot_id": "abc", "expired_count": 0},
        )
        with db.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT changed_by, action FROM config_audit_log "
                    "WHERE config_key = 'restore'"
                )
            ).one()
            hidden = conn.execute(
                text("SELECT to_regclass('public.audit_trail_hidden')")
            ).scalar_one()
        assert (row.changed_by, row.action) == ("runner", "restore")
        assert hidden == "audit_trail_hidden"
    finally:
        with db.begin() as conn:
            conn.execute(text("ALTER TABLE audit_trail_hidden RENAME TO audit_trail"))


def test_legacy_audit_row_when_the_trail_revision_is_absent(db):
    with db.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '0001_baseline'"))
    try:
        assert ConfigService(user_id="user-3").set_system_config(
            "audit.t.legacy", {"name": "legacy"}
        )
        with db.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT changed_by, action, new_value FROM config_audit_log "
                    "WHERE config_key = 'audit.t.legacy'"
                )
            ).one()
            trail_rows = conn.execute(
                text("SELECT count(*) FROM audit_trail")
            ).scalar_one()
        assert (row.changed_by, row.action, row.new_value) == (
            "user-3",
            "create",
            {"name": "legacy"},
        )
        assert trail_rows == 0
    finally:
        with db.begin() as conn:
            conn.execute(
                text("UPDATE alembic_version SET version_num = :v"),
                {"v": trail.REVISION},
            )
