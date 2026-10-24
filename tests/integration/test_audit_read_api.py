"""Read, export, and poll the trail (#2765), on a database the upgrade built.

Manager exports and is refused on poll. An Admin's sign-in polls, including a
cursor past the tip, and that admin's MCP credential does not. A service
account whose role holds ``audit.read`` polls; without it, and a Viewer
service account, the refusal names ``audit.read``. Export writes one entry
and closes the file with the tip. Poll writes nothing.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from core.audit import trail
from core.auth import current_user as current_user_module
from core.auth import mcp_credential_service, principals, token_blacklist
from core.auth.auth_service import AuthService
from core.storage.connection import get_db_manager
from core.storage.models import Principal, Role, User
from core.storage.unit_of_work import unit_of_work
from core.upgrade import command
from services.api.middleware.auth import get_current_active_user
from services.api.middleware.rate_limit import limiter
from services.api.routers import audit as audit_router
from services.api.routers import auth
from tests.integration.test_upgrade_command import _run, _scratch

pytestmark = [pytest.mark.integration, pytest.mark.database]

PASSWORD = "Correct-Horse-Battery-9!"
WHEN = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


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


def _user(username: str, role_id: str, *, service: bool = False) -> str:
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
            service_account=service,
        )
        principals.add_for_user(session, user)
        session.add(user)
        session.flush()
        return user.user_id


def _login(client, username: str) -> str:
    resp = client.post(
        "/api/auth/login",
        json={"username_or_email": username, "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    client.cookies.clear()
    return token


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _append(payload: dict, **kwargs) -> trail.Entry:
    with unit_of_work() as session:
        return trail.append(session, payload, **kwargs)


def _count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM audit_trail")).scalar_one()


def _tip(engine) -> tuple:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT seq, entry_hash, payload FROM audit_trail "
                "ORDER BY seq DESC LIMIT 1"
            )
        ).one()


def test_manager_can_export_and_cannot_poll(db, client):
    _user("ada", "role-admin")
    _user("mia", "role-manager")
    _append({trail.ACTION: "case.comment", trail.ACTOR: "ada"})
    admin = _bearer(_login(client, "ada"))
    manager = _bearer(_login(client, "mia"))

    exported = client.get("/api/audit/export", headers=manager)
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"].startswith("application/x-ndjson")

    refused = client.get("/api/audit/poll?after=0", headers=manager)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Permission denied: audit.forward required"

    # The same sign-in, presented as the cookie rather than a bearer.
    client.cookies.set("access_token", admin["Authorization"].split()[1])
    cookie = client.get("/api/audit/poll?after=0")
    client.cookies.clear()
    assert cookie.status_code == 200, cookie.text
    assert cookie.json()["entries"]


def test_a_cursor_past_the_tip_is_a_reset(db, client):
    _user("ada", "role-admin")
    admin = _bearer(_login(client, "ada"))
    # Login records auth.sign_in. This test is about an empty trail.
    with db.begin() as conn:
        conn.execute(text("TRUNCATE audit_trail, audit_trail_held"))
        conn.execute(text("UPDATE audit_trail_anchor SET seq = 0, entry_hash = ''"))

    caught_up = client.get("/api/audit/poll?after=0", headers=admin)
    assert caught_up.status_code == 200, caught_up.text
    assert caught_up.json() == {"entries": [], "tip": {"seq": 0, "hash": ""}}

    past = client.get("/api/audit/poll?after=1", headers=admin)
    assert past.json()["reset"] is True
    assert past.json()["restore"] is None
    assert past.json()["tip"] == {"seq": 0, "hash": ""}

    _append({trail.ACTION: "case.comment", trail.ACTOR: "ada"})
    at_tip = client.get("/api/audit/poll?after=1", headers=admin)
    assert "reset" not in at_tip.json()
    assert at_tip.json()["entries"] == []

    _append(
        {trail.ACTION: trail.ACTION_RESTORE, trail.ACTOR: trail.SYSTEM, "abandoned": 9}
    )
    reset = client.get("/api/audit/poll?after=99", headers=admin)
    body = reset.json()
    assert body["reset"] is True
    assert body["tip"]["seq"] == 2
    assert body["restore"]["payload"][trail.ACTION] == trail.ACTION_RESTORE
    assert body["restore"]["seq"] == 2


def test_an_mcp_credential_and_a_token_without_a_sign_in_cannot_poll(db, client):
    admin_id = _user("ada", "role-admin")
    minted = mcp_credential_service.mint(admin_id, "ada's script")
    refused = client.get("/api/audit/poll?after=0", headers=_bearer(minted.token))
    assert refused.status_code == 401
    assert "MCP credential" in refused.json()["detail"]

    with unit_of_work() as session:
        user = session.get(User, admin_id)
        token = AuthService.generate_jwt_token(user, "access")
    no_sid = client.get("/api/audit/poll?after=0", headers=_bearer(token))
    assert no_sid.status_code == 401
    assert no_sid.json()["detail"] == audit_router._SIGN_IN_REQUIRED


def test_export_writes_one_entry_and_poll_writes_none(db, client):
    admin_id = _user("ada", "role-admin")
    _append({trail.ACTION: "case.comment", trail.ACTOR: "not-the-caller"})
    admin = _bearer(_login(client, "ada"))
    # Login records auth.sign_in. Poll and export are what this count watches.
    before = _count(db)

    polled = client.get("/api/audit/poll?after=0", headers=admin)
    assert polled.status_code == 200, polled.text
    assert _count(db) == before

    exported = client.get("/api/audit/export?actor=not-the-caller", headers=admin)
    assert exported.status_code == 200, exported.text
    lines = [json.loads(line) for line in exported.text.splitlines()]
    checkpoint = lines[-1]["checkpoint"]
    seq, entry_hash, payload = _tip(db)
    assert (checkpoint["seq"], checkpoint["hash"]) == (seq, entry_hash)
    assert payload[trail.ACTION] == trail.ACTION_EXPORT
    assert payload[trail.ACTOR] == admin_id
    assert payload["filter"] == {"actor": "not-the-caller"}
    assert lines[0]["seq"] < checkpoint["seq"]
    assert _count(db) == before + 1


def test_the_filter_is_shared(db, client):
    _user("ada", "role-admin")
    with unit_of_work() as session:
        # agent:hunt is seeded by the principals revision.
        if session.get(Principal, "person-a") is None:
            session.add(Principal(principal_id="person-a", kind="person", label="A"))
    own = _append(
        {trail.ACTION: "case.comment", trail.ACTOR: "person-a"}, occurred_at=WHEN
    )
    behalf = _append(
        {
            trail.ACTION: "agent.run",
            trail.ACTOR: "agent:hunt",
            trail.ON_BEHALF_OF: "person-a",
        }
    )
    other = _append(
        {
            trail.ACTION: "case.comment",
            trail.ACTOR: "person-b",
            trail.ATTRIBUTION: trail.ATTRIBUTION_UNVERIFIED_LEGACY,
            trail.OBJECT: {"id": "case-9"},
        }
    )
    by_id = _append(
        {trail.ACTION: "case.update", trail.ACTOR: "person-a", trail.OBJECT: "case-9"}
    )
    open_intent = _append(
        {trail.ACTION: "tool.call", trail.ACTOR: "person-a", trail.AWAITING: True}
    )
    closed = _append(
        {trail.ACTION: "tool.call", trail.ACTOR: "person-a", trail.AWAITING: True}
    )
    result = _append(
        {
            trail.ACTION: "tool.result",
            trail.ACTOR: "person-a",
            trail.OUTCOME: trail.OUTCOME_DONE,
            trail.INTENT: closed.seq,
        }
    )
    late = _append({trail.ACTION: "auth.sign_out", trail.ACTOR: "person-a"}, late=True)

    admin = _bearer(_login(client, "ada"))

    def seqs(query: str) -> set[int]:
        resp = client.get(f"/api/audit/entries?{query}", headers=admin)
        assert resp.status_code == 200, resp.text
        return {row["seq"] for row in resp.json()["entries"]}

    assert seqs("person=person-a") >= {own.seq, behalf.seq}
    assert other.seq not in seqs("person=person-a")
    assert seqs("kind=agent") == {behalf.seq}
    assert seqs("area=tool") == {open_intent.seq, closed.seq, result.seq}
    assert seqs("object=case-9") == {other.seq, by_id.seq}
    assert seqs("outcome=outcome%20unknown") == {open_intent.seq}
    assert seqs("outcome=recorded%20late") == {late.seq}
    assert seqs("outcome=done") == {result.seq}
    assert other.seq not in seqs("attribution=verified")
    assert own.seq in seqs("attribution=verified")
    assert seqs("attribution=unverified%20legacy") == {other.seq}
    # A naive bound is UTC.
    assert own.seq in seqs("since=2024-06-01T12:00:00&until=2024-06-01T12:00:00")
    assert behalf.seq not in seqs("since=2024-06-01T12:00:00&until=2024-06-01T12:00:00")

    polled = client.get("/api/audit/poll?after=0&person=person-a", headers=admin)
    assert behalf.seq in {row["seq"] for row in polled.json()["entries"]}

    bad = client.get("/api/audit/entries?outcome=maybe", headers=admin)
    assert bad.status_code == 422


def test_verify_returns_the_first_break(db, client):
    _user("ada", "role-admin")
    first = _append({trail.ACTION: "case.comment", trail.ACTOR: "ada"})
    _append({trail.ACTION: "case.comment", trail.ACTOR: "ada"})
    admin = _bearer(_login(client, "ada"))

    ok = client.get("/api/audit/verify?first=1&last=2", headers=admin)
    assert ok.status_code == 200, ok.text
    assert ok.json()["ok"] is True
    assert ok.json()["first_bad"] is None

    with db.begin() as conn:
        conn.execute(
            text(
                "UPDATE audit_trail SET payload = "
                '\'{"action": "tampered"}\' WHERE seq = :s'
            ),
            {"s": first.seq},
        )
    broken = client.get("/api/audit/verify", headers=admin)
    assert broken.json()["first_bad"] == first.seq
    assert broken.json()["ok"] is False


def test_a_service_account_with_audit_read_can_poll(db, client):
    role_id = "role-siem"
    with unit_of_work() as session:
        role = session.get(Role, role_id)
        if role is None:
            session.add(
                Role(
                    role_id=role_id,
                    name="SIEM",
                    description="",
                    permissions={"audit.read": True},
                )
            )
        else:
            role.permissions = {"audit.read": True}
    _user("siem", role_id, service=True)
    # No Origin or Sec-Fetch-Site: a browser login is refused for this account.
    headers = _bearer(_login(client, "siem"))
    before = _count(db)

    polled = client.get("/api/audit/poll?after=0", headers=headers)
    assert polled.status_code == 200, polled.text
    assert _count(db) == before

    with unit_of_work() as session:
        session.get(Role, role_id).permissions = {}
    refused = client.get("/api/audit/poll?after=0", headers=headers)
    assert refused.status_code == 403
    assert refused.json()["detail"] == "Permission denied: audit.read required"

    _user("medic", "role-viewer", service=True)
    viewer = client.get(
        "/api/audit/poll?after=0", headers=_bearer(_login(client, "medic"))
    )
    assert viewer.status_code == 403
    assert viewer.json()["detail"] == "Permission denied: audit.read required"


def test_dev_mode_lets_a_service_account_poll_and_still_refuses_forward(
    db, client, monkeypatch
):
    viewer = _user("vera", "role-viewer")
    service = _user("admin", "role-admin", service=True)
    with unit_of_work() as session:
        assert session.get(Role, "role-admin").permissions["audit.forward"] is True
        assert session.get(Principal, service).kind == "service_account"

    monkeypatch.setattr("core.auth.auth_service._is_dev_mode", lambda: True)
    assert AuthService.check_permission(viewer, "audit.forward") is True
    assert AuthService.check_permission(service, "audit.forward") is False
    # Dev mode grants audit.read, so the poll gate allows this account.
    assert AuthService.check_permission(service, "audit.read") is True

    monkeypatch.setattr(current_user_module, "DEV_MODE", True)
    allowed = client.get("/api/audit/poll?after=0")
    assert allowed.status_code == 200, allowed.text
