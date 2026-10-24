"""Retention and erasure (#2769).

A 30-day period is refused. A period does not delete before day 7. Erasure
leaves the chain verifying and clears the beside-columns. The last audit
manager cannot be erased. A restore re-applies an id that was only in the
outside file. Clocks are passed in; nothing sleeps.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from core.audit import feed, retention, trail
from core.auth import current_user as current_user_module
from core.auth import mcp_credential_service, principals, sign_ins, token_blacklist
from core.auth.auth_service import AuthService
from core.backup import restore
from core.config import get_settings
from core.storage.connection import get_db_manager
from core.storage.models import User
from core.storage.unit_of_work import unit_of_work
from core.upgrade import command
from services.api.middleware.auth import get_current_active_user
from services.api.middleware.rate_limit import limiter
from services.api.routers import audit as audit_router
from services.api.routers import auth
from tests.integration.test_upgrade_command import _run, _scratch

pytestmark = [pytest.mark.integration, pytest.mark.database]

PASSWORD = "Correct-Horse-Battery-9!"
NOW = datetime(2026, 10, 23, tzinfo=timezone.utc)


class FakeRedis:
    async def set(self, key, value, ex=None, nx=False):
        return True

    async def get(self, key):
        return None

    async def exists(self, key):
        return 0


@pytest.fixture(scope="module")
def engine(tmp_path_factory):
    with _scratch(False) as scratch:
        code, report = _run(scratch, tmp_path_factory.mktemp("up"), restore_point="rp")
        assert code == command.EXIT_OK, report
        manager = get_db_manager()
        saved = manager._engine, manager._session_factory
        manager._engine = scratch
        manager._session_factory = sessionmaker(bind=scratch)
        try:
            yield scratch
        finally:
            manager._engine, manager._session_factory = saved
            get_settings.cache_clear()


@pytest.fixture
def db(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE audit_trail, audit_trail_held"))
        conn.execute(text("UPDATE audit_trail_anchor SET seq = 0, entry_hash = ''"))
        conn.execute(
            text(
                "UPDATE audit_feed_state SET pending_announcements = '{}', "
                "last_checkpoint_at = now()"
            )
        )
        conn.execute(
            text("DELETE FROM system_config WHERE key = :key"),
            {"key": retention.CONFIG_KEY},
        )
        conn.execute(text("UPDATE users SET is_active = false"))
    get_settings.cache_clear()
    return engine


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(token_blacklist, "_get_client", lambda: FakeRedis())
    monkeypatch.setattr(current_user_module, "DEV_MODE", False)
    monkeypatch.setattr("core.auth.auth_service._is_dev_mode", lambda: False)
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(
        audit_router.router,
        prefix="/api/audit",
        dependencies=[Depends(get_current_active_user)],
    )
    limiter.reset()
    with TestClient(app) as c:
        yield c


def _user(username: str, role_id: str) -> str:
    with unit_of_work() as session:
        found = session.query(User).filter(User.username == username).one_or_none()
        if found is not None:
            found.is_active = True
            return found.principal_id
        user = User(
            user_id=f"user-{uuid.uuid4().hex[:12]}",
            username=username,
            email=f"{username}@example.com",
            password_hash=AuthService.hash_password(PASSWORD),
            full_name=username,
            role_id=role_id,
            is_active=True,
            is_verified=False,
            mfa_enabled=False,
            service_account=False,
        )
        principals.add_for_user(session, user)
        session.add(user)
        session.flush()
        return user.principal_id


def _activate(*principal_ids: str) -> None:
    with unit_of_work() as session:
        session.execute(text("UPDATE users SET is_active = false"))
        session.query(User).filter(User.principal_id.in_(principal_ids)).update(
            {User.is_active: True}, synchronize_session=False
        )


def _login(client, username: str) -> dict[str, str]:
    resp = client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    client.cookies.clear()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _seqs(engine) -> list[int]:
    with engine.connect() as conn:
        return list(
            conn.execute(text("SELECT seq FROM audit_trail ORDER BY seq")).scalars()
        )


def _label(engine, principal_id: str):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT label, deleted_at IS NULL FROM principals WHERE principal_id = :id"
            ),
            {"id": principal_id},
        ).one()


def _beside(engine, principal_id: str) -> tuple:
    with engine.connect() as conn:
        return conn.execute(
            text("""
                SELECT source_ip, actor_text, operator_text, attempted_name, personal
                FROM audit_trail
                WHERE payload->>'principal' = :id
                ORDER BY seq LIMIT 1
                """),
            {"id": principal_id},
        ).one()


def test_a_30_day_period_is_refused(db, client):
    ada = _user("ada", "role-admin")
    _activate(ada)
    admin = _login(client, "ada")

    refused = client.put("/api/audit/retention", headers=admin, json={"days": 30})
    assert refused.status_code == 400
    assert refused.json()["detail"] == "retention is at least 365 days"
    too_long = client.put("/api/audit/retention", headers=admin, json={"days": 36501})
    assert too_long.status_code == 400
    assert too_long.json()["detail"] == "retention is at most 36500 days"
    with db.connect() as conn:
        assert (
            conn.execute(
                text("SELECT 1 FROM system_config WHERE key = :key"),
                {"key": retention.CONFIG_KEY},
            ).first()
            is None
        )

    saved = client.put("/api/audit/retention", headers=admin, json={"days": 365})
    assert saved.status_code == 200, saved.text
    assert saved.json()["pending_days"] == 365
    assert saved.json()["effective_days"] is None
    assert saved.json()["unconfirmed"] is True

    polled = client.get("/api/audit/poll?after=0", headers=admin)
    assert polled.status_code == 200, polled.text
    assert any(
        row["payload"].get("action") == trail.ACTION_RETENTION
        for row in polled.json()["entries"]
    )
    after = client.get("/api/audit/retention", headers=admin)
    assert after.json()["unconfirmed"] is False


def test_a_period_does_not_delete_before_day_7(db):
    with unit_of_work() as session:
        trail.append(
            session,
            {"action": "old"},
            occurred_at=NOW - timedelta(days=400),
            source_ip="1.1.1.1",
        )
        trail.append(
            session,
            {"action": "kept"},
            occurred_at=NOW - timedelta(days=10),
            source_ip="2.2.2.2",
        )
        # A late row: old time, higher seq. Deleting through it would take `kept` with it.
        trail.append(
            session,
            {"action": "late"},
            occurred_at=NOW - timedelta(days=500),
            late=True,
            source_ip="3.3.3.3",
        )
        retention.set_period(session, 365, trail.SYSTEM, NOW)

    feed.maintenance_and_send(feed.SendClock(), now=NOW + timedelta(days=6))
    assert 1 in _seqs(db)

    feed.maintenance_and_send(feed.SendClock(), now=NOW + timedelta(days=7))
    left = _seqs(db)
    assert 1 not in left
    assert {2, 3} <= set(left)
    with unit_of_work() as session:
        assert trail.verify(session).ok

    # Once the last in-window row ages out, the tip itself is outside and goes.
    with unit_of_work() as session:
        session.execute(text("DELETE FROM audit_trail"))
        session.execute(text("UPDATE audit_trail_anchor SET seq = 0, entry_hash = ''"))
        session.execute(
            text("DELETE FROM system_config WHERE key = :key"),
            {"key": retention.CONFIG_KEY},
        )
        trail.append(
            session, {"action": "ancient"}, occurred_at=NOW - timedelta(days=900)
        )
        session.execute(
            text("""
                INSERT INTO system_config (key, value, description, config_type)
                VALUES (:key, CAST(:value AS jsonb), 'retention', 'audit')
                """),
            {
                "key": retention.CONFIG_KEY,
                "value": json.dumps(
                    {
                        "periods": [
                            {
                                "days": 365,
                                "set_at": (NOW - timedelta(days=8)).isoformat(),
                                "seq": 1,
                            }
                        ],
                        "erasures": [],
                    }
                ),
            },
        )
        retention.apply_due(session, NOW)
    assert _seqs(db) == []


def test_erasure_leaves_verify_green_and_clears_the_beside_columns(
    db, tmp_path, monkeypatch
):
    ada = _user("ada", "role-admin")
    mia = _user("mia", "role-manager")
    _activate(ada, mia)
    when = NOW - timedelta(days=1)
    with unit_of_work() as session:
        trail.append(
            session,
            {"action": "user.updated", "actor": mia, "principal": mia},
            occurred_at=when,
            source_ip="203.0.113.8",
            actor_text="Mia Example",
            operator_text="Ada Admin",
            attempted_name="mia",
            personal={"email": "mia@example.com"},
        )
        trail.hold(
            session,
            {"action": "auth.sign_in_failed", "principal": mia},
            when,
            attempted_name="mia",
            personal={"email": "mia@example.com"},
        )
        minted = mcp_credential_service.mint(
            mia, "mia's script", session=session, actor=ada
        )
        sign_ins.start(session, "si-mia", mia, None)
        assert minted is not None
        credential_id = minted.record.credential_id

    monkeypatch.delenv("VIGIL_UPGRADE_DIR", raising=False)
    get_settings.cache_clear()
    with unit_of_work() as session:
        with pytest.raises(
            retention.RetentionError, match="VIGIL_UPGRADE_DIR is unset"
        ):
            retention.erase(session, ada, mia, "", NOW)
    assert _beside(db, mia) == (
        "203.0.113.8",
        "Mia Example",
        "Ada Admin",
        "mia",
        {"email": "mia@example.com"},
    )

    monkeypatch.setenv("VIGIL_UPGRADE_DIR", str(tmp_path))
    get_settings.cache_clear()
    with unit_of_work() as session:
        retention.erase(session, ada, mia, "", NOW)

    assert _beside(db, mia) == (None, None, None, None, None)
    label, alive = _label(db, mia)
    assert label is None and alive
    with db.connect() as conn:
        user = conn.execute(
            text(
                "SELECT username, is_active, email FROM users WHERE principal_id = :id"
            ),
            {"id": mia},
        ).one()
        # Disabled, as account deletion does, so the blanked account cannot sign in again.
        assert user == ("mia", False, "mia@example.com")
        revoked = conn.execute(
            text(
                "SELECT revoked_at IS NOT NULL FROM mcp_credentials WHERE credential_id = :id"
            ),
            {"id": credential_id},
        ).scalar_one()
        ended = conn.execute(
            text("SELECT end_reason FROM sign_ins WHERE sign_in_id = 'si-mia'")
        ).scalar_one()
        held = conn.execute(
            text(
                "SELECT beside FROM audit_trail_held WHERE payload->>'principal' = :id"
            ),
            {"id": mia},
        ).scalar_one()
    assert revoked is True
    assert ended == sign_ins.REVOKED
    assert held == {}
    assert retention.read_ids(tmp_path / retention.LIST_NAME) == [mia]
    with unit_of_work() as session:
        assert trail.verify(session).ok
        payload = session.execute(
            text(
                "SELECT payload FROM audit_trail WHERE payload->>'principal' = :id "
                "AND payload->>'action' = 'user.updated'"
            ),
            {"id": mia},
        ).scalar_one()
    assert payload["actor"] == mia  # the hashed document is untouched


def test_two_managers_erasing_each_other_leave_one_holder(db, tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_UPGRADE_DIR", str(tmp_path))
    get_settings.cache_clear()
    # Own names: principal labels persist across tests, and ada and ben are blanked below.
    pat = _user("pat", "role-admin")
    quinn = _user("quinn", "role-admin")
    _activate(pat, quinn)
    with unit_of_work() as session:
        retention.erase(session, pat, quinn, "left", NOW)
    with unit_of_work() as session:
        retention.erase(session, quinn, pat, "left", NOW)
    feed.maintenance_and_send(feed.SendClock(), now=NOW + timedelta(days=7))
    # Each was the last active holder once the other was gone; only one may be blanked.
    assert [_label(db, p)[0] for p in (pat, quinn)].count(None) == 1
    feed.maintenance_and_send(feed.SendClock(), now=NOW + timedelta(days=8))
    assert [_label(db, p)[0] for p in (pat, quinn)].count(None) == 1


def test_the_last_audit_manager_cannot_be_erased(db, client, tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_UPGRADE_DIR", str(tmp_path))
    get_settings.cache_clear()
    ada = _user("ada", "role-admin")
    _activate(ada)
    admin = _login(client, "ada")

    refused = client.post(
        "/api/audit/erasure",
        headers=admin,
        json={"principal_id": ada, "reason": "retiring"},
    )
    assert refused.status_code == 400
    assert refused.json()["detail"] == "the last audit manager cannot be erased"
    assert _label(db, ada)[0] == "ada"

    ben = _user("ben", "role-admin")
    _activate(ada, ben)
    self_erase = client.post(
        "/api/audit/erasure",
        headers=admin,
        json={"principal_id": ada, "reason": "retiring"},
    )
    assert self_erase.status_code == 400
    assert self_erase.json()["detail"] == "you cannot erase your own principal"

    needs_reason = client.post(
        "/api/audit/erasure", headers=admin, json={"principal_id": ben, "reason": "  "}
    )
    assert needs_reason.status_code == 400
    assert needs_reason.json()["detail"] == "erasing an audit manager needs a reason"
    assert _label(db, ben)[0] == "ben"

    now = NOW
    with unit_of_work() as session:
        body = retention.erase(session, ada, ben, "left the team", now)
    assert body["erasures"][0]["principal_id"] == ben
    assert body["erasures"][0]["unconfirmed"] is True
    assert _label(db, ben)[0] == "ben"

    feed.maintenance_and_send(feed.SendClock(), now=now + timedelta(days=6))
    assert _label(db, ben)[0] == "ben"
    feed.maintenance_and_send(feed.SendClock(), now=now + timedelta(days=7))
    assert _label(db, ben)[0] is None
    assert ben in retention.read_ids(tmp_path / retention.LIST_NAME)
    with unit_of_work() as session:
        assert trail.verify(session).ok
        # Applied, and the announcement has not been carried, so the flag stays.
        assert retention.view(session, now + timedelta(days=7))["erasure_unconfirmed"]

    # A disabled account still holds audit.manage, so it waits. If that person
    # is the last active holder when the wait ends, the leader does not blank them.
    cal = _user("cal", "role-admin")
    dee = _user("dee", "role-admin")
    _activate(cal, dee)
    with unit_of_work() as session:
        session.query(User).filter(User.principal_id == dee).update(
            {User.is_active: False}, synchronize_session=False
        )
        with pytest.raises(retention.RetentionError, match="needs a reason"):
            retention.erase(session, cal, dee, "", now)
        retention.erase(session, cal, dee, "leaving", now)
    _activate(dee)  # cal is no longer active; dee is the last one who is
    feed.maintenance_and_send(feed.SendClock(), now=now + timedelta(days=7))
    assert _label(db, dee)[0] == "dee"


def test_restore_reapplies_an_id_that_was_only_in_the_outside_file(
    db, tmp_path, monkeypatch
):
    monkeypatch.setenv("VIGIL_UPGRADE_DIR", str(tmp_path))
    get_settings.cache_clear()
    mia = _user("mia", "role-manager")
    _activate(mia)
    with unit_of_work() as session:
        trail.append(
            session,
            {"action": "user.updated", "actor": mia, "principal": mia},
            source_ip="198.51.100.4",
            actor_text="Mia Example",
            personal={"email": "mia@example.com"},
        )
    # The snapshot was taken before this erasure, so its list does not have mia.
    (tmp_path / retention.LIST_NAME).write_text(mia + "\n", encoding="utf-8")
    root = tmp_path / "restored"
    snapshot = root / "staging" / retention.LIST_NAME
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("", encoding="utf-8")

    restore._reapply_erased(root, "/staging/db.dump")

    assert _beside(db, mia) == (None, None, None, None, None)
    assert _label(db, mia)[0] is None
    assert retention.read_ids(tmp_path / retention.LIST_NAME) == [mia]
    with unit_of_work() as session:
        assert trail.verify(session).ok
