"""Known activity against a real database at head.

Set ``KNOWN_ACTIVITY_TEST_DB=1`` and the ``POSTGRES_*`` settings of a disposable
database that ``python -m core.upgrade`` has taken to head. Without them this
module skips: no CI job here runs a database.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

_DB = {
    name: os.environ.get(name, "")
    for name in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
    )
}

pytestmark = pytest.mark.skipif(
    os.environ.get("KNOWN_ACTIVITY_TEST_DB") != "1",
    reason="needs a disposable database at head",
)

_FIRST_READS = (
    "/api/overview?range=all",
    "/api/triage?range=all",
    "/api/findings?limit=1000&exclusions=hide",
)


@pytest.fixture
def db(monkeypatch):
    for name, value in _DB.items():
        monkeypatch.setenv(name, value)
    from core.storage.unit_of_work import unit_of_work

    return unit_of_work


@pytest.fixture
def app(db):
    from services.api.main import app as real_app

    yield real_app
    real_app.dependency_overrides.clear()


def _user(db, role="role-analyst", service_account=False):
    from core.storage.models import Principal, User

    uid = f"u-{uuid.uuid4().hex[:10]}"
    with db() as session:
        session.add(Principal(principal_id=uid, kind="person", label=uid))
        session.flush()
        user = User(
            user_id=uid,
            principal_id=uid,
            username=uid,
            email=f"{uid}@example.org",
            password_hash="x",
            full_name=uid,
            role_id=role,
            is_active=True,
            service_account=service_account,
        )
        session.add(user)
        session.flush()
        session.expunge(user)
    return user


def _client(app, user):
    from core.auth.current_user import get_current_user

    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _finding(db, **context):
    from core.storage.models import Finding

    finding_id = f"f-{uuid.uuid4().hex[:12]}"
    with db() as session:
        session.add(
            Finding(
                finding_id=finding_id,
                data_source="synthetic",
                severity="high",
                entity_context=context,
            )
        )
    return finding_id


def _new_host():
    return f"web-{uuid.uuid4().hex[:8]}"


def _rule_body(host, **overrides):
    return {
        "entity_kind": "host",
        "entity_value": host,
        "reason": "Patch window",
        **overrides,
    }


def _trail(db, known_activity_id):
    with db() as session:
        rows = session.execute(
            text(
                "SELECT payload, actor_text, operator_text FROM audit_trail "
                "WHERE payload->>'object' = :id ORDER BY seq"
            ),
            {"id": known_activity_id},
        ).all()
    return rows


def _reads(client, finding_id):
    """Every read of an alert a person or the daemon's queue goes by."""
    reads = {path: client.get(path) for path in _FIRST_READS}
    reads["alert"] = client.get(f"/api/overview/alerts/{finding_id}")
    reads["finding"] = client.get(f"/api/v1/findings/{finding_id}")
    for path, response in reads.items():
        assert response.status_code == 200, (path, response.text)
    return {path: response.json() for path, response in reads.items()}


def _stable(reads):
    """The reads without the clock's own fields, which move between two calls."""

    def strip(value):
        if isinstance(value, dict):
            return {
                k: strip(v)
                for k, v in value.items()
                if k not in ("generated_at", "as_of", "now", "cached_at", "age_seconds")
            }
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return strip(reads)


def test_an_alert_with_an_active_rule_reads_exactly_as_before(db, app):
    from core.findings.exclusions import current_active_ips
    from core.storage.models import Finding

    host, ip = _new_host(), f"203.0.113.{uuid.uuid4().int % 200 + 20}"
    finding_id = _finding(db, hostnames=[host], src_ips=[ip])
    client = _client(app, _user(db))
    before = _stable(_reads(client, finding_id))
    with db() as session:
        row_before = session.get(Finding, finding_id).__dict__.copy()
    ips_before = current_active_ips()

    for body in (
        _rule_body(host),
        {"entity_kind": "ip", "entity_value": ip, "reason": "Scanner we run"},
    ):
        assert client.post("/api/known-activity", json=body).status_code == 201

    assert _stable(_reads(client, finding_id)) == before
    with db() as session:
        row_after = session.get(Finding, finding_id).__dict__.copy()
    assert current_active_ips() == ips_before
    row_before.pop("_sa_instance_state")
    row_after.pop("_sa_instance_state")
    assert row_after == row_before

    context = client.get(f"/api/known-activity/for-finding/{finding_id}").json()
    assert len(context["known_activity"]) == 2


def test_the_same_reads_do_see_an_exclusion(db, app):
    """The comparison above can see a hidden alert: an exclusion on the same
    address changes these reads."""
    ip = f"203.0.113.{uuid.uuid4().int % 200 + 20}"
    finding_id = _finding(db, src_ips=[ip])
    client = _client(app, _user(db))

    def listed():
        rows = client.get("/api/findings?limit=1000&exclusions=hide").json()["findings"]
        return finding_id in {f["finding_id"] for f in rows}

    assert listed()
    made = client.post("/api/exclusions", json={"ip": ip, "reason": "Control"})
    assert made.status_code == 201
    try:
        assert not listed()
    finally:
        client.post(f"/api/exclusions/{made.json()['exclusion_id']}/remove")


def test_a_rule_stops_applying_when_removed_or_expired(db, app):
    host = _new_host()
    finding_id = _finding(db, hostnames=[host])
    client = _client(app, _user(db))
    url = f"/api/known-activity/for-finding/{finding_id}"
    assert client.get(url).json()["known_activity"] == []

    removed = client.post("/api/known-activity", json=_rule_body(host)).json()
    expired = client.post("/api/known-activity", json=_rule_body(host)).json()
    assert len(client.get(url).json()["known_activity"]) == 2

    client.post(f"/api/known-activity/{removed['known_activity_id']}/remove")
    shown = client.get(url).json()["known_activity"]
    assert [r["known_activity_id"] for r in shown] == [expired["known_activity_id"]]

    with db() as session:
        session.execute(
            text(
                "UPDATE known_activity SET created_at = now() - interval '2 days', "
                "expires_at = now() - interval '1 day' "
                "WHERE known_activity_id = :id"
            ),
            {"id": expired["known_activity_id"]},
        )
    assert client.get(url).json()["known_activity"] == []

    history = client.get("/api/known-activity?include_ended=true").json()
    states = {r["known_activity_id"]: r["state"] for r in history["known_activity"]}
    assert states[removed["known_activity_id"]] == "removed"
    assert states[expired["known_activity_id"]] == "expired"


def test_create_and_remove_go_on_the_trail_as_the_signed_in_person(db, app):
    from core.audit.words import sentence

    user = _user(db)
    host = _new_host()
    client = _client(app, user)
    body = _rule_body(host, origin="case", origin_ref="case-synth-1")
    made = client.post(
        "/api/known-activity", json={**body, "created_by": "someone-else"}
    )
    assert made.status_code == 201
    rule = made.json()
    assert rule["created_by"] == user.username

    client.post(
        f"/api/known-activity/{rule['known_activity_id']}/remove",
        json={"reason": "Window over"},
    )
    client.post(f"/api/known-activity/{rule['known_activity_id']}/remove")

    rows = _trail(db, rule["known_activity_id"])
    assert [r.payload["action"] for r in rows] == [
        "known_activity.create",
        "known_activity.remove",
    ]
    for row in rows:
        assert row.payload["actor"] == user.principal_id
        assert row.payload["case_id"] == "case-synth-1"
        assert row.actor_text == user.username
        assert row.operator_text == host
        assert "Patch window" not in str(row.payload)
    assert sentence(rows[0].payload) == "Recorded known activity (host)"


def test_a_role_without_the_permission_is_refused(db, app):
    from core.memory.known_activity import read_active
    from core.time import utcnow

    host = _new_host()
    viewer = _client(app, _user(db, role="role-viewer"))
    assert viewer.get("/api/known-activity").status_code == 200
    assert viewer.post("/api/known-activity", json=_rule_body(host)).status_code == 403
    with db() as session:
        assert read_active(session, [f"host:{host}"], utcnow()) == []

    made = _client(app, _user(db)).post("/api/known-activity", json=_rule_body(host))
    rule_id = made.json()["known_activity_id"]
    viewer = _client(app, _user(db, role="role-viewer"))
    assert viewer.post(f"/api/known-activity/{rule_id}/remove").status_code == 403

    with db() as session:
        role_id = f"role-{uuid.uuid4().hex[:8]}"
        session.execute(
            text(
                "INSERT INTO roles (role_id, name, description, permissions, "
                "is_system_role) VALUES (:id, :id, 'synthetic', '{}', false)"
            ),
            {"id": role_id},
        )
    assert (
        _client(app, _user(db, role=role_id)).get("/api/known-activity").status_code
        == 403
    )


def test_a_service_account_cannot_record_a_rule(db, app):
    account = _client(app, _user(db, service_account=True))
    made = account.post("/api/known-activity", json=_rule_body(_new_host()))
    assert made.status_code == 403


def test_a_case_shows_what_its_alerts_name_and_the_rules_for_them(db, app):
    from core.storage.models import Case
    from core.storage.models.base import case_findings

    host = _new_host()
    finding_id = _finding(db, hostnames=[host], domains=["Example.org"])
    case_id = f"case-{uuid.uuid4().hex[:10]}"
    with db() as session:
        session.add(Case(case_id=case_id, title="Synthetic", status="open"))
        session.flush()
        session.execute(
            case_findings.insert().values(case_id=case_id, finding_id=finding_id)
        )
    client = _client(app, _user(db))
    assert client.get("/api/known-activity/for-case/absent").status_code == 404

    context = client.get(f"/api/known-activity/for-case/{case_id}").json()
    assert {"entity_kind": "host", "entity_value": host} in context["entities"]
    assert {"entity_kind": "domain", "entity_value": "example.org"} in context[
        "entities"
    ]
    assert context["known_activity"] == []
    assert context["default_expires_at"]

    client.post("/api/known-activity", json=_rule_body(host))
    shown = client.get(f"/api/known-activity/for-case/{case_id}").json()
    assert [r["entity_value"] for r in shown["known_activity"]] == [host]
