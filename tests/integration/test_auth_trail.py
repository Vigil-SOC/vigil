"""Sign-in, account and credential events on the audit trail (#2760), through
the real auth and users routes on a database ``python -m core.upgrade`` built.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from core.audit import trail
from core.auth import mcp_credential_service
from core.storage.models import Principal, User
from core.time import utcnow
from tests.integration.test_principals_sign_ins import (  # noqa: F401 - fixtures
    PASSWORD,
    _claims,
    _login,
    _user,
    client,
    db,
    redis,
)

pytestmark = [pytest.mark.integration, pytest.mark.database]


def _entries(db, action=None) -> list[dict]:
    with db() as s:
        rows = s.execute(
            text(
                "SELECT payload, late, source_ip, attempted_name FROM audit_trail "
                "ORDER BY seq"
            )
        ).all()
    return [
        {**p, "_late": late, "_source_ip": ip, "_attempted_name": name}
        for p, late, ip, name in rows
        if action is None or p["action"] == action
    ]


def _credential_id(db, user_id: str) -> str:
    # The fixture's sessions expire on commit, so minted.record is detached.
    with db() as s:
        return s.execute(
            text("SELECT credential_id FROM mcp_credentials WHERE user_id = :u"),
            {"u": user_id},
        ).scalar_one()


def _refuse_appends(db):
    with db.begin() as s:
        s.execute(
            text(
                "ALTER TABLE audit_trail ADD CONSTRAINT refuse CHECK (false) NOT VALID"
            )
        )


def _accept_appends(db):
    with db.begin() as s:
        s.execute(text("ALTER TABLE audit_trail DROP CONSTRAINT refuse"))


def _bearer(client, username: str) -> dict:
    return {"Authorization": f"Bearer {_login(client, username)['access_token']}"}


def test_refused_logins_write_one_entry_then_a_closing_count(db, client):
    ana = _user("ana", "role-analyst")
    for _ in range(3):
        resp = client.post(
            "/api/auth/login",
            json={"username_or_email": "ana", "password": "wrong-password-1!"},
        )
        assert resp.status_code == 401

    [first] = _entries(db, "auth.sign_in_failed")
    assert first["principal"] == ana and first["reason"] == "bad_password"
    assert "actor" not in first
    assert (first["_attempted_name"], first["_source_ip"]) == ("ana", "testclient")
    with db() as s:
        # The upgrade's roles.revised row may already occupy seq 1.
        first_failure_seq = s.execute(
            text(
                "SELECT min(seq) FROM audit_trail "
                "WHERE payload->>'action' = 'auth.sign_in_failed'"
            )
        ).scalar_one()

    with db.begin() as s:
        trail.close_windows(s, now=utcnow() + timedelta(minutes=5), everything=True)
    first, closing = _entries(db, "auth.sign_in_failed")
    assert closing["collapsed"]["count"] == 3
    assert closing["collapsed"]["first_seq"] == first_failure_seq
    # Nothing secret is recorded.
    with db() as s:
        dumped = s.execute(text("SELECT payload::text FROM audit_trail")).scalars()
        assert not any("wrong-password" in row for row in dumped)


def test_deleting_a_user_writes_the_trail_in_the_same_transaction(db, client):
    admin = _user("admin1", "role-admin")
    ana = _user("ana", "role-analyst")
    mcp_credential_service.mint(ana, "ana's laptop", actor=ana)
    credential_id = _credential_id(db, ana)
    bearer = _bearer(client, "admin1")

    with db.begin() as s:
        s.execute(
            text(
                "ALTER TABLE audit_trail ADD CONSTRAINT refuse "
                "CHECK (payload->>'action' <> 'credential.revoked') NOT VALID"
            )
        )
    # The app's handler would make this a 500; here the error surfaces.
    lenient = TestClient(client.app, raise_server_exceptions=False)
    assert lenient.delete(f"/api/users/{ana}", headers=bearer).status_code == 500
    with db() as s:
        assert s.get(Principal, ana).deleted_at is None
        assert s.get(User, ana).is_active is True
    assert _entries(db, "user.deleted") == []
    _accept_appends(db)

    assert client.delete(f"/api/users/{ana}", headers=bearer).status_code == 200
    [deleted] = _entries(db, "user.deleted")
    [revoked] = _entries(db, "credential.revoked")
    assert (deleted["actor"], deleted["principal"]) == (admin, ana)
    assert revoked["credential_id"] == credential_id
    assert revoked["actor"] == admin


def test_a_trail_failure_rolls_back_an_update_but_not_a_disable_or_sign_out(db, client):
    _user("admin1", "role-admin")
    ana = _user("ana", "role-analyst")
    admin = _bearer(client, "admin1")
    tokens = _login(client, "ana")
    sid = _claims(tokens["access_token"])["sid"]

    _refuse_appends(db)
    resp = client.post(
        "/api/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert resp.status_code == 200, resp.text

    resp = client.put(
        f"/api/users/{ana}",
        json={"role_id": "role-viewer", "is_active": False},
        headers=admin,
    )
    assert resp.status_code == 500
    with db() as s:
        user = s.get(User, ana)
        # The role change rolled back with its entry; the disable stood.
        assert (user.role_id, user.is_active) == ("role-analyst", False)

    _accept_appends(db)
    with db.begin() as s:
        assert trail.drain_late(s) == 2
    [disabled] = _entries(db, "user.disabled")
    [signed_out] = _entries(db, "auth.sign_out")
    assert disabled["_late"] and disabled["principal"] == ana
    assert signed_out["_late"] and signed_out["actor"] == ana
    assert signed_out["sign_in"] == sid
    assert _entries(db, "user.role_changed") == []
    assert PASSWORD not in str(_entries(db))


def test_an_expired_credential_is_recorded_once(db, client):
    ana = _user("ana", "role-analyst")
    minted = mcp_credential_service.mint(
        ana, "old", expires_at=utcnow() - timedelta(days=1), actor=ana
    )
    credential_id = _credential_id(db, ana)
    [mint] = _entries(db, "credential.minted")
    assert (mint["actor"], mint["credential_id"]) == (ana, credential_id)
    assert "old" not in mint.values()

    for _ in range(2):
        assert mcp_credential_service.authenticate(minted.token) is None
    [expired] = _entries(db, "credential.expired")
    assert expired == {
        "action": "credential.expired",
        "actor": "system",
        "credential_id": credential_id,
        "_late": False,
        "_source_ip": None,
        "_attempted_name": None,
    }
    with db.begin() as s:
        assert trail.close_windows(s, everything=True) == 0
        assert trail.drain_late(s) == 0
