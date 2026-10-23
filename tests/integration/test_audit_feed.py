"""The syslog feed (#2767), on a database ``python -m core.upgrade`` built.

A second replica does not send. A failed send retries from the same sequence.
Turning the feed on writes one entry. A credential cannot turn it on. Importing
upgrade history twice records each line once.
"""

from __future__ import annotations

import json
import socket
import ssl
import subprocess
import threading
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from core.audit import feed, trail
from core.auth import current_user as current_user_module
from core.auth import mcp_credential_service, principals, token_blacklist
from core.auth.auth_service import AuthService
from core.auth.permissions import MCP_CREDENTIAL_DENIED
from core.storage.connection import get_db_manager
from core.storage.models import User
from core.storage.unit_of_work import unit_of_work
from core.upgrade import command
from services.api.routers import audit_feed, auth
from services.api.routers import config as config_router
from tests.integration.test_upgrade_command import _run, _scratch

pytestmark = [pytest.mark.integration, pytest.mark.database]

PASSWORD = "Correct-Horse-Battery-9!"


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


@pytest.fixture
def db(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE audit_trail, audit_trail_held"))
        conn.execute(text("UPDATE audit_trail_anchor SET seq = 0, entry_hash = ''"))
        conn.execute(text("""
                UPDATE audit_feed_state SET
                    last_delivered_seq = NULL, last_delivered_at = NULL,
                    last_error = NULL, failing = false,
                    last_checkpoint_at = now(), checkpoint_through = 0,
                    history_offset = 0
            """))
        conn.execute(
            text("DELETE FROM integration_configs WHERE integration_id = :id"),
            {"id": feed.INTEGRATION_ID},
        )
    return engine


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(token_blacklist, "_get_client", lambda: FakeRedis())
    monkeypatch.setattr(current_user_module, "DEV_MODE", False)
    monkeypatch.setattr("core.auth.auth_service._is_dev_mode", lambda: False)
    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(audit_feed.router, prefix="/api/audit/feed")
    app.include_router(config_router.router, prefix="/api/config")
    with TestClient(app) as test_client:
        yield test_client


def _user(username: str, role_id: str) -> str:
    with unit_of_work() as session:
        found = session.query(User).filter(User.username == username).one_or_none()
        if found is not None:
            return found.user_id
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
        )
        principals.add_for_user(session, user)
        session.add(user)
        session.flush()
        return user.user_id


def _login(client, username: str) -> dict[str, str]:
    resp = client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    client.cookies.clear()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _entries(engine, action: str) -> list:
    with engine.connect() as conn:
        return (
            conn.execute(
                text(
                    "SELECT seq, payload, operator_text FROM audit_trail "
                    "WHERE payload->>'action' = :action ORDER BY seq"
                ),
                {"action": action},
            )
            .mappings()
            .all()
        )


def _cursor(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT last_delivered_seq, failing FROM audit_feed_state")
        ).one()


def _enable(engine, port: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("""
                INSERT INTO integration_configs
                    (integration_id, enabled, config, integration_name, integration_type)
                VALUES (:id, true, CAST(:cfg AS jsonb), 'Audit trail', 'syslog')
            """),
            {
                "id": feed.INTEGRATION_ID,
                "cfg": json.dumps({"host": "127.0.0.1", "port": port}),
            },
        )


def _secrets(monkeypatch, ca: str) -> None:
    def _get(key, default=None):
        return ca if key == feed.CA_SECRET else default

    monkeypatch.setattr(feed, "get_secret", _get)


def _cert(tmp: Path) -> tuple[Path, Path]:
    cert, key = tmp / "cert.pem", tmp / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def _external_ids(payload: bytes) -> list[int]:
    found = []
    i = 0
    while i < len(payload):
        space = payload.find(b" ", i)
        assert space > i
        length = int(payload[i:space])
        message = payload[space + 1 : space + 1 + length]
        assert b"CEF:0|Vigil|SOC|" in message
        marker = b"externalId="
        start = message.find(marker) + len(marker)
        end = message.find(b" ", start)
        found.append(int(message[start:end]))
        i = space + 1 + length
    return found


def test_a_failed_send_retries_from_the_same_sequence(db, monkeypatch, tmp_path):
    cert, key = _cert(tmp_path)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    got: list[bytes] = []
    ready: list[int] = []

    def serve():
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(4)
        sock.settimeout(15)
        port = sock.getsockname()[1]
        ready.append(port)
        try:
            refused, _ = sock.accept()
            refused.close()
            for _ in range(2):
                raw, _ = sock.accept()
                tls = ctx.wrap_socket(raw, server_side=True)
                buf = b""
                try:
                    while True:
                        chunk = tls.recv(65536)
                        if not chunk:
                            break
                        buf += chunk
                except (ssl.SSLError, socket.timeout, OSError):
                    pass
                got.append(buf)
                tls.close()
        finally:
            sock.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert _wait_port(ready, thread)
    _secrets(monkeypatch, cert.read_text())
    _enable(db, ready[0])
    with unit_of_work() as session:
        trail.append(session, {trail.ACTION: "case.comment", trail.ACTOR: "system"})

    clock = feed.SendClock()
    clock.now = 10
    feed.maintenance_and_send(clock)
    assert _cursor(db).last_delivered_seq is None
    assert len(_entries(db, feed.ACTION_FAILED)) == 1

    # Still inside the backoff: nothing is sent, and nothing is skipped.
    feed.maintenance_and_send(clock)
    assert _cursor(db).last_delivered_seq is None
    assert len(_entries(db, feed.ACTION_FAILED)) == 1

    clock.now = 11
    feed.maintenance_and_send(clock)
    delivered = _cursor(db).last_delivered_seq
    assert delivered is not None
    assert _external_ids(got[0])[0] == 1
    assert delivered in _external_ids(got[0])

    clock.now = 12
    feed.maintenance_and_send(clock)
    thread.join(15)
    assert not thread.is_alive()
    sent = _external_ids(got[0]) + _external_ids(got[1])
    assert sent == list(range(1, _cursor(db).last_delivered_seq + 1))
    assert len(_entries(db, feed.ACTION_RECOVERED)) == 1
    assert len(_entries(db, feed.ACTION_FAILED)) == 1


def _wait_port(ready: list, thread: threading.Thread) -> bool:
    for _ in range(50):
        if ready:
            return True
        thread.join(0.1)
        if not thread.is_alive() and not ready:
            raise AssertionError("syslog listener exited before it bound a port")
    raise AssertionError("syslog listener did not bind")


def test_a_second_replica_does_not_send(db, monkeypatch):
    sent: list[int] = []
    release = threading.Event()

    def _block(dest, messages):
        sent.append(len(messages))
        release.wait(5)

    monkeypatch.setattr(feed, "deliver", _block)
    _secrets(
        monkeypatch, "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    )
    _enable(db, 6514)
    with unit_of_work() as session:
        trail.append(session, {trail.ACTION: "case.comment", trail.ACTOR: "system"})

    results: list[bool] = []
    lost = threading.Event()

    def run(conn):
        led = False
        try:
            led = feed.attempt(conn, feed.SendClock(), False)
        finally:
            if not led:
                lost.set()
        results.append(led)

    conns = [
        db.connect().execution_options(isolation_level="AUTOCOMMIT") for _ in range(2)
    ]
    threads = [
        threading.Thread(target=run, args=(conn,), daemon=True) for conn in conns
    ]
    try:
        for thread in threads:
            thread.start()
        assert lost.wait(5), "the replica that lost the lock did not return"
        for _ in range(50):
            if sent:
                break
            threading.Event().wait(0.05)
        assert sent == [1]
        release.set()
        for thread in threads:
            thread.join(5)
        assert sorted(results) == [False, True]
    finally:
        release.set()
        for conn in conns:
            conn.execute(text("SELECT pg_advisory_unlock_all()"))
            conn.close()


def test_turning_the_feed_on_writes_one_entry(db, client, monkeypatch):
    _user("ada", "role-admin")
    store: dict[str, str] = {}
    monkeypatch.setattr(
        feed, "set_secret", lambda key, value: store.__setitem__(key, value) or True
    )
    monkeypatch.setattr(
        feed, "get_secret", lambda key, default=None: store.get(key, default)
    )
    monkeypatch.setattr(
        feed, "delete_secret", lambda key: store.pop(key, None) is not None
    )
    admin = _login(client, "ada")
    before = _count(db)
    saved = client.put(
        "/api/audit/feed",
        headers=admin,
        json={
            "enabled": True,
            "host": "siem.example",
            "port": 6514,
            "ca_pem": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["enabled"] is True
    assert saved.json()["ca_set"] is True
    assert _count(db) == before + 1
    entry = _entries(db, "config.change")
    assert len(entry) == 1
    assert entry[0]["payload"]["config_key"] == feed.INTEGRATION_ID
    assert entry[0]["payload"]["verb"] in {"create", "update"}
    assert "BEGIN CERTIFICATE" not in json.dumps(entry[0]["payload"])
    assert "MIIB" in store[feed.CA_SECRET]


def test_a_credential_cannot_turn_the_feed_on(db, client):
    admin_id = _user("ada", "role-admin")
    _user("mia", "role-manager")
    minted = mcp_credential_service.mint(admin_id, "ada's script")
    body = {"enabled": True, "host": "siem.example", "port": 6514}
    manager_headers = _login(client, "mia")
    before = _count(db)

    credential = client.put(
        "/api/audit/feed",
        headers={"Authorization": f"Bearer {minted.token}"},
        json=body,
    )
    assert credential.status_code == 403, credential.text
    assert credential.json()["detail"] == MCP_CREDENTIAL_DENIED

    manager = client.put("/api/audit/feed", headers=manager_headers, json=body)
    assert manager.status_code == 403, manager.text
    assert manager.json()["detail"] == "Permission denied: audit.forward required"
    assert _count(db) == before


def test_the_integrations_save_cannot_change_the_feed(db, client, monkeypatch):
    _user("ada", "role-admin")
    _user("mia", "role-manager")
    store: dict[str, str] = {}
    monkeypatch.setattr(
        feed, "set_secret", lambda key, value: store.__setitem__(key, value) or True
    )
    monkeypatch.setattr(
        feed, "get_secret", lambda key, default=None: store.get(key, default)
    )
    admin = _login(client, "ada")
    manager_headers = _login(client, "mia")
    saved = client.put(
        "/api/audit/feed",
        headers=admin,
        json={
            "enabled": True,
            "host": "siem.example",
            "port": 6514,
            "ca_pem": "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n",
        },
    )
    assert saved.status_code == 200, saved.text
    before = _count(db)

    class _Bridge:
        def derive_remote_mcp_env(self):
            return {}

    client.app.state.integration_bridge = _Bridge()
    posted = client.post(
        "/api/config/integrations",
        headers=manager_headers,
        json={
            "enabled_integrations": [],
            "integrations": {
                feed.INTEGRATION_ID: {
                    "host": "evil.example",
                    "port": 9,
                    "enabled": False,
                }
            },
        },
    )
    assert posted.status_code == 200, posted.text
    assert _count(db) == before
    again = client.get("/api/audit/feed", headers=admin)
    assert again.json()["enabled"] is True
    assert again.json()["host"] == "siem.example"
    listed = client.get("/api/config/integrations", headers=admin)
    assert feed.INTEGRATION_ID not in listed.json()["integrations"]
    assert feed.INTEGRATION_ID not in listed.json()["enabled_integrations"]


def test_importing_upgrade_history_twice_records_it_once(db, monkeypatch, tmp_path):
    line = json.dumps(
        {
            "outcome": "applied",
            "from": "0.1.0",
            "to": "0.2.0",
            "reason": "operator said so",
            "restore_point": "rp",
        }
    )
    path = tmp_path / "history.jsonl"
    path.write_text(line + "\n" + line + "\n")
    monkeypatch.setattr(feed, "_history_file", lambda: path)

    feed.maintenance_and_send(feed.SendClock())
    feed.maintenance_and_send(feed.SendClock())

    rows = _entries(db, feed.ACTION_UPGRADE)
    assert len(rows) == 1
    assert rows[0]["operator_text"] == "operator said so"
    assert rows[0]["payload"]["actor"] == trail.SYSTEM
    assert "operator said so" not in json.dumps(rows[0]["payload"])


def test_a_checkpoint_is_written_once_per_quiet_interval(db):
    with db.begin() as conn:
        conn.execute(
            text(
                "UPDATE audit_feed_state SET last_checkpoint_at = now() - interval '16 minutes', "
                "checkpoint_through = 0"
            )
        )
    with unit_of_work() as session:
        trail.append(session, {trail.ACTION: "case.comment", trail.ACTOR: "system"})

    feed.maintenance_and_send(feed.SendClock())
    feed.maintenance_and_send(feed.SendClock())
    assert len(_entries(db, trail.ACTION_CHECKPOINT)) == 1


def _count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM audit_trail")).scalar_one()
