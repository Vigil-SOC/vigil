"""The audit leader and syslog feed (#2767), on a database the upgrade built.

One replica sends; a failed batch is sent again from the same sequence; turning
the feed on is a trail entry; an MCP credential, a Manager and a service
account cannot set it; an upgrade run is imported once.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.audit import feed, trail
from core.auth import current_user as current_user_module
from core.auth import mcp_credential_service, token_blacklist
from core.storage.unit_of_work import unit_of_work
from services.api.middleware.auth import get_current_active_user
from services.api.middleware.rate_limit import limiter
from services.api.routers import audit as audit_router
from services.api.routers import auth
from tests.integration.test_audit_read_api import (  # noqa: F401 - fixture
    FakeRedis,
    _bearer,
    _login,
    _user,
    engine,
)

pytestmark = [pytest.mark.integration, pytest.mark.database]


@pytest.fixture
def db(engine, monkeypatch):  # noqa: F811
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE audit_trail, audit_trail_held"))
        conn.execute(text("UPDATE audit_trail_anchor SET seq = 0, entry_hash = ''"))
        conn.execute(text("DELETE FROM audit_feed_state"))
        conn.execute(text("INSERT INTO audit_feed_state (id) VALUES (1)"))
        conn.execute(
            text("DELETE FROM integration_configs WHERE integration_id = :i"),
            {"i": feed.INTEGRATION_ID},
        )
    # No TLS material: the system's trust store, no client certificate.
    monkeypatch.setattr("core.secrets_manager.get_secret", lambda k, d=None: d)
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


class Wire:
    """Stands in for the TLS socket: what was written, or the failure to raise."""

    def __init__(self):
        self.frames: list[bytes] = []
        self.fail: BaseException | None = None
        self.connects = 0

    def __call__(self, dest):
        self.connects += 1
        if self.fail is not None:
            raise self.fail
        return self

    def sendall(self, data: bytes) -> None:
        while data:
            size, _, rest = data.partition(b" ")
            self.frames.append(rest[: int(size)])
            data = rest[int(size) :]

    def close(self) -> None:
        pass

    def seqs(self) -> list[int]:
        return [int(f.split(b"externalId=")[1].split(b" ")[0]) for f in self.frames]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _enable(bind) -> None:
    with bind.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO integration_configs (integration_id, enabled, config)"
                " VALUES (:i, true, CAST(:c AS jsonb))"
            ),
            {"i": feed.INTEGRATION_ID, "c": json.dumps({"host": "siem", "port": 6514})},
        )


def _append(n: int) -> None:
    for i in range(n):
        with unit_of_work() as session:
            trail.append(session, {trail.ACTION: "case.comment", "i": i})


def _actions(bind) -> list[str]:
    with bind.connect() as conn:
        return list(
            conn.execute(
                text("SELECT payload->>'action' FROM audit_trail ORDER BY seq")
            ).scalars()
        )


def _tip(bind) -> int:
    with bind.connect() as conn:
        return conn.execute(text("SELECT max(seq) FROM audit_trail")).scalar_one()


def _cursor(bind) -> int:
    with bind.connect() as conn:
        return conn.execute(
            text("SELECT delivered_seq FROM audit_feed_state")
        ).scalar_one()


def test_a_second_replica_does_not_send(db):
    _enable(db)
    _append(3)
    first, second = Wire(), Wire()
    a = feed.Leader(db, connect=first)
    b = feed.Leader(db, connect=second)
    try:
        a.tick()
        b.tick()
        assert a.leading and not b.leading
        # Three entries and the leader's first timed checkpoint.
        assert first.seqs() == [1, 2, 3, 4]
        assert second.connects == 0

        # The leader goes away; the other replica takes over from the cursor.
        a.stop()
        _append(1)
        b.tick()
        assert b.leading
        assert second.seqs() == [5]
        assert _cursor(db) == 5
    finally:
        a.stop()
        b.stop()


def test_a_failed_send_retries_from_the_same_seq(db):
    _enable(db)
    _append(3)
    wire, clock = Wire(), Clock()
    leader = feed.Leader(db, connect=wire, clock=clock)
    try:
        wire.fail = ConnectionRefusedError("siem down")
        leader.tick()
        assert _cursor(db) == 0
        assert _actions(db)[-1] == feed.ACTION_FAILED

        leader.tick()  # inside the backoff: no attempt
        assert wire.connects == 1
        for _ in range(8):  # every retry fails; the wait stops growing at 60s
            clock.now += feed.BACKOFF_CAP_SECONDS
            leader.tick()
        assert wire.connects == 9
        assert leader._retry_at - clock.now == feed.BACKOFF_CAP_SECONDS
        assert _actions(db).count(feed.ACTION_FAILED) == 1, "one entry per outage"

        wire.fail = None
        clock.now += feed.BACKOFF_CAP_SECONDS
        leader.tick()
        leader.tick()
        tip = _tip(db)
        assert wire.seqs() == list(range(1, tip + 1)), "nothing skipped or twice"
        actions = _actions(db)
        assert actions.count(feed.ACTION_RECOVERED) == 1
        assert actions.index(feed.ACTION_FAILED) < actions.index(feed.ACTION_RECOVERED)
        assert _cursor(db) == tip
        with Session(db) as session:
            assert feed.health(session)["behind"] == 0
    finally:
        leader.stop()


def test_the_checkpoint_waits_for_new_entries(db):
    with Session(db) as session, session.begin():
        assert feed.checkpoint(session) is None  # an empty trail
    _append(2)
    with Session(db) as session, session.begin():
        entry = feed.checkpoint(session)
        assert entry is not None
        assert feed.checkpoint(session) is None  # not due
        later = session.execute(text("SELECT now()")).scalar_one() + timedelta(
            minutes=16
        )
        assert feed.checkpoint(session, later) is None  # due, but nothing new
    with db.connect() as conn:
        payload = conn.execute(
            text("SELECT payload FROM audit_trail WHERE seq = :s"), {"s": entry.seq}
        ).scalar_one()
    assert payload[trail.ACTION] == trail.ACTION_CHECKPOINT
    assert payload["checkpoint"]["seq"] == 2


def test_an_upgrade_run_is_imported_once(db, tmp_path):
    lines = [
        {
            "time": "2026-10-01T10:00:00+00:00",
            "from": "0.8.0",
            "to": "0.9.0",
            "outcome": "applied",
            "restore_point": "rp-1",
            "reason": "restore point",
        },
        {
            "time": "2026-10-02T10:00:00+00:00",
            "from": "0.9.0",
            "to": "0.9.1",
            "outcome": "applied",
            "restore_point": "overridden",
            "reason": "ops ack: disk full, no restore point",
        },
    ]
    history = tmp_path / feed.HISTORY
    history.write_text("".join(json.dumps(line) + "\n" for line in lines))
    for _ in range(2):
        with Session(db) as session, session.begin():
            feed.import_upgrades(session, tmp_path)
    # A line still being written waits for its newline.
    with history.open("a") as f:
        f.write('{"time": "2026-10-03T10:00:00+00:00", "outcome": "refused"')
    with Session(db) as session, session.begin():
        assert feed.import_upgrades(session, tmp_path) == 0

    with db.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT payload, operator_text, late FROM audit_trail"
                " WHERE payload->>'action' = :a ORDER BY seq"
            ),
            {"a": feed.ACTION_UPGRADE},
        ).all()
    assert len(rows) == 2
    payload, operator_text, late = rows[1]
    assert payload[trail.ACTOR] == trail.SYSTEM
    assert payload["result"] == "applied"
    assert operator_text == "ops ack: disk full, no restore point"
    assert late is True


def test_turning_the_feed_on_writes_an_entry(db, client):
    admin_id = _user("ada", "role-admin")
    admin = _bearer(_login(client, "ada"))
    resp = client.put(
        "/api/audit/feed",
        json={"enabled": True, "host": "siem.example", "port": 6514},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is True
    assert resp.json()["health"]["behind"] == 1

    with db.connect() as conn:
        payload = conn.execute(
            text("SELECT payload FROM audit_trail ORDER BY seq DESC LIMIT 1")
        ).scalar_one()
    assert payload[trail.ACTION] == "config.change"
    assert payload[trail.ACTOR] == admin_id
    assert payload["config_key"] == feed.INTEGRATION_ID
    assert payload["enabled"] is True
    assert payload["new_value"] == {"host": "siem.example", "port": 6514}


def test_a_credential_cannot_turn_it_on(db, client):
    on = {"enabled": True, "host": "siem.example", "port": 6514}
    admin_id = _user("ada", "role-admin")
    minted = mcp_credential_service.mint(admin_id, "ada's script")
    refused = client.put("/api/audit/feed", json=on, headers=_bearer(minted.token))
    assert refused.status_code == 401
    assert "MCP credential" in refused.json()["detail"]

    _user("mia", "role-manager")
    manager = _bearer(_login(client, "mia"))
    assert client.put("/api/audit/feed", json=on, headers=manager).status_code == 403
    assert client.get("/api/audit/feed", headers=manager).status_code == 403
    assert client.post("/api/audit/feed/test", headers=manager).status_code == 403

    with Session(db) as session:
        assert feed.settings(session) == (False, None, None)
