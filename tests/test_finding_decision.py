"""An analyst decides on one finding in one replay-safe step, on the trail.

Needs a real Postgres at head: run ``python -m core.upgrade`` against it, then set
``VIGIL_TEST_POSTGRES_DSN`` to it, logged in as ``vigil_app``. Every test makes its
own findings and accounts, so the trail, which only ever grows, can be shared.
"""

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import Request

DSN = os.environ.get("VIGIL_TEST_POSTGRES_DSN")
pytestmark = [
    pytest.mark.skipif(not DSN, reason="VIGIL_TEST_POSTGRES_DSN not set"),
    # A signed-in person is the actor here, not the run the suite binds by default.
    pytest.mark.unbound,
]

ANALYST = "role-analyst"
VIEWER = "role-viewer"
FINDINGS_ONLY = {"findings.read": True, "findings.write": True}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class World:
    """The running route, against the real database, as any account."""

    def __init__(self):
        from fastapi import FastAPI

        from core.auth.current_user import get_current_user
        from services.api.errors import register_exception_handlers
        from services.api.routers.findings import router

        self.app = FastAPI()
        register_exception_handlers(self.app)
        self.app.include_router(router, prefix="/api/findings")
        self.app.dependency_overrides[get_current_user] = self._signed_in

    @staticmethod
    async def _signed_in(request: Request):
        from core.cases.agent_closure import note_http_actor
        from core.storage.connection import get_db_manager
        from core.storage.models import User

        with get_db_manager().session_scope() as session:
            user = session.get(User, request.headers["x-test-user"])
            session.expunge(user)
        note_http_actor(
            username=user.username,
            principal_id=user.principal_id,
            via="cookie",
            person=True,
        )
        return user

    def client(self):
        from fastapi.testclient import TestClient

        return TestClient(self.app, raise_server_exceptions=False)

    def user(self, role_id: str = ANALYST):
        from core.auth import principals
        from core.storage.connection import get_db_manager
        from core.storage.models import User

        user_id = _new_id("u")
        with get_db_manager().session_scope() as session:
            user = User(
                user_id=user_id,
                username=user_id,
                email=f"{user_id}@example.test",
                full_name="Synthetic Analyst",
                password_hash="not-a-hash",
                role_id=role_id,
                is_active=True,
            )
            principals.add_for_user(session, user)
            session.add(user)
        return user_id

    def role(self, permissions: dict) -> str:
        from core.storage.connection import get_db_manager
        from core.storage.models import Role

        role_id = _new_id("role")
        with get_db_manager().session_scope() as session:
            session.add(
                Role(
                    role_id=role_id,
                    name=role_id,
                    description="Synthetic",
                    permissions=permissions,
                )
            )
        return role_id

    def finding(self, *, severity="high", title="Synthetic beacon from host-7"):
        from core.storage.connection import get_db_manager
        from core.storage.models import Finding

        finding_id = _new_id("f")
        with get_db_manager().session_scope() as session:
            session.add(
                Finding(
                    finding_id=finding_id,
                    data_source="splunk",
                    severity=severity,
                    title=title,
                    description="Synthetic description",
                )
            )
        return finding_id

    def decide(self, user, finding_id, outcome, request_id=None, **extra):
        return self.client().post(
            f"/api/findings/{finding_id}/decide",
            headers={"x-test-user": user},
            json={
                "outcome": outcome,
                "request_id": request_id or uuid.uuid4().hex,
                **extra,
            },
        )

    def read(self, sql: str, **params):
        from sqlalchemy import text

        from core.storage.connection import get_db_manager

        with get_db_manager().session_scope() as session:
            return session.execute(text(sql), params).all()

    def write(self, sql: str, **params) -> None:
        from sqlalchemy import text

        from core.storage.connection import get_db_manager

        with get_db_manager().session_scope() as session:
            session.execute(text(sql), params)

    def entries(self, finding_id: str) -> list[dict]:
        from core.audit import trail
        from core.storage.connection import get_db_manager

        with get_db_manager().session_scope() as session:
            return trail.read(session, trail.make_query(object_ref=finding_id))

    def writes(self, finding_id: str) -> dict:
        """Everything a decision on this finding could have written."""
        linked = self.read(
            "SELECT case_id FROM case_findings WHERE finding_id = :f", f=finding_id
        )
        noise = self.read(
            "SELECT noise_marked_at FROM findings WHERE finding_id = :f", f=finding_id
        )[0][0]
        return {
            "noise": noise,
            "cases": [row[0] for row in linked],
            "queued": self.read(
                "SELECT id FROM intake_triggers WHERE finding_id = :f", f=finding_id
            ),
            "entries": self.entries(finding_id),
        }


NOTHING = {"noise": None, "cases": [], "queued": [], "entries": []}


@pytest.fixture
def world(monkeypatch):
    from core.storage.connection import get_db_manager
    from core.storage.database_data_service import DatabaseDataService
    from services.api.routers import findings

    monkeypatch.setattr(
        "core.storage.connection._load_connection_string_secret", lambda: DSN
    )
    get_db_manager().initialize(force=True)
    monkeypatch.setattr(findings, "data_service", DatabaseDataService())
    return World()


def test_noise_marks_the_finding_and_lands_on_the_trail(world):
    user, finding = world.user(), world.finding()

    res = world.decide(user, finding, "noise", reason="  known scanner ")

    assert res.status_code == 200
    assert res.json() == {
        "finding_id": finding,
        "outcome": "noise",
        "case_id": None,
        "already_queued": None,
        "replayed": False,
    }
    row = world.read(
        "SELECT noise_marked_at, noise_marked_by, noise_marked_by_kind, noise_reason"
        " FROM findings WHERE finding_id = :f",
        f=finding,
    )[0]
    assert row[0] is not None
    assert row[1:] == (user, "analyst", "known scanner")
    [entry] = world.entries(finding)
    assert entry["payload"]["action"] == "finding.decided"
    assert entry["payload"]["actor"] == user
    assert entry["sentence"] == f"Marked {finding} as noise"
    # Free text sits beside the hash, so erasure can blank it.
    assert entry["operator_text"] == "known scanner"
    assert "known scanner" not in str(entry["payload"])


def test_triage_queues_once_and_says_when_it_was_already_queued(world):
    user, finding = world.user(), world.finding(severity="critical")

    first = world.decide(user, finding, "triage")
    second = world.decide(user, finding, "triage")

    assert first.json()["already_queued"] is False
    assert second.json()["already_queued"] is True
    assert second.json()["replayed"] is False
    [(state, priority, kind)] = world.read(
        "SELECT state, priority, kind FROM intake_triggers WHERE finding_id = :f",
        f=finding,
    )
    assert (state, priority, kind) == ("queued", "critical", "detection")
    assert [e["sentence"] for e in world.entries(finding)] == [
        f"Sent {finding} to triage"
    ] * 2


def test_take_case_opens_one_with_its_sla_and_the_actor_owns_it(world):
    user, finding = world.user(), world.finding(severity="HIGH")

    body = world.decide(user, finding, "take_case").json()

    case_id = body["case_id"]
    assert body["outcome"] == "take_case" and body["replayed"] is False
    [(title, priority, assignee, status)] = world.read(
        "SELECT title, priority, assignee, status FROM cases WHERE case_id = :c",
        c=case_id,
    )
    assert (title, priority, assignee, status) == (
        "Synthetic beacon from host-7",
        "high",
        user,
        "open",
    )
    assert world.writes(finding)["cases"] == [case_id]
    assert world.read("SELECT 1 FROM case_slas WHERE case_id = :c", c=case_id) == [(1,)]

    from core.audit import trail
    from core.storage.connection import get_db_manager

    with get_db_manager().session_scope() as session:
        on_case = trail.read(session, trail.make_query(object_ref=case_id))
    assert [e["payload"]["action"] for e in on_case] == [
        "case.create",
        "case.assign",
        "finding.decided",
    ]
    assert {e["payload"]["actor"] for e in on_case} == {user}
    assert on_case[-1]["sentence"] == f"Took {finding} as a case"


@pytest.mark.parametrize("severity", [None, "informational"])
def test_take_case_gives_a_severity_no_priority_names_medium(world, severity):
    user, finding = world.user(), world.finding(severity=severity)

    case_id = world.decide(user, finding, "take_case").json()["case_id"]

    assert world.read("SELECT priority FROM cases WHERE case_id = :c", c=case_id) == [
        ("medium",)
    ]


def test_take_case_assigns_an_open_linked_case_instead_of_opening_one(world):
    first, second, finding = world.user(), world.user(), world.finding()
    case_id = world.decide(first, finding, "take_case").json()["case_id"]

    taken = world.decide(second, finding, "take_case").json()

    assert taken["case_id"] == case_id
    assert world.writes(finding)["cases"] == [case_id]
    assert world.read("SELECT assignee FROM cases WHERE case_id = :c", c=case_id) == [
        (second,)
    ]


def test_take_case_opens_a_new_case_when_the_linked_one_is_closed(world):
    user, finding = world.user(), world.finding()
    closed = world.decide(user, finding, "take_case").json()["case_id"]
    world.write("UPDATE cases SET status = 'closed' WHERE case_id = :c", c=closed)

    reopened = world.decide(user, finding, "take_case").json()["case_id"]

    assert reopened != closed
    assert sorted(world.writes(finding)["cases"]) == sorted([closed, reopened])


def test_a_service_account_cannot_be_the_owner(world):
    user, finding = world.user(), world.finding()
    world.write("UPDATE users SET service_account = true WHERE user_id = :u", u=user)

    assert world.decide(user, finding, "take_case").status_code == 400
    assert world.writes(finding) == NOTHING


@pytest.mark.parametrize("outcome", ["noise", "triage", "take_case"])
def test_a_repeated_request_id_replays_and_writes_nothing(world, outcome):
    user, finding, request_id = world.user(), world.finding(), uuid.uuid4().hex

    first = world.decide(user, finding, outcome, request_id).json()
    before = world.writes(finding)
    again = world.decide(user, finding, outcome, request_id).json()

    assert first["replayed"] is False and again["replayed"] is True
    assert {**again, "replayed": False} == first
    assert world.writes(finding) == before
    assert len(before["entries"]) == 1


def test_a_repeated_request_id_answers_with_the_outcome_on_record(world):
    user, finding, request_id = world.user(), world.finding(), uuid.uuid4().hex
    world.decide(user, finding, "noise", request_id)

    again = world.decide(user, finding, "take_case", request_id).json()

    assert (again["outcome"], again["replayed"]) == ("noise", True)
    assert world.writes(finding)["cases"] == []


def test_a_request_id_is_scoped_to_its_finding(world):
    user, request_id = world.user(), uuid.uuid4().hex
    one, other = world.finding(), world.finding()

    world.decide(user, one, "noise", request_id)
    res = world.decide(user, other, "noise", request_id).json()

    assert res["replayed"] is False
    assert world.writes(other)["noise"] is not None


@pytest.mark.parametrize("outcome", ["noise", "triage", "take_case"])
def test_concurrent_duplicates_act_once(world, outcome):
    user, finding, request_id = world.user(), world.finding(), uuid.uuid4().hex
    count = 12
    barrier = threading.Barrier(count)

    def post(_):
        barrier.wait()
        return world.decide(user, finding, outcome, request_id)

    with ThreadPoolExecutor(count) as pool:
        replies = list(pool.map(post, range(count)))

    assert [r.status_code for r in replies] == [200] * count
    bodies = [r.json() for r in replies]
    assert sum(1 for b in bodies if not b["replayed"]) == 1
    assert len({(b["outcome"], b["case_id"], b["already_queued"]) for b in bodies}) == 1
    written = world.writes(finding)
    assert len(written["entries"]) == 1
    assert len(written["cases"]) == (1 if outcome == "take_case" else 0)
    assert len(written["queued"]) == (1 if outcome == "triage" else 0)


def test_two_analysts_taking_at_once_open_one_case(world):
    users = [world.user() for _ in range(6)]
    finding = world.finding()
    barrier = threading.Barrier(len(users))

    def post(user):
        barrier.wait()
        return world.decide(user, finding, "take_case")

    with ThreadPoolExecutor(len(users)) as pool:
        replies = list(pool.map(post, users))

    assert [r.status_code for r in replies] == [200] * len(users)
    assert len({r.json()["case_id"] for r in replies}) == 1
    written = world.writes(finding)
    assert len(written["cases"]) == 1
    assert len(written["entries"]) == len(users)


@pytest.mark.parametrize("outcome", ["noise", "triage", "take_case"])
def test_a_role_without_findings_write_is_refused_and_writes_nothing(world, outcome):
    user, finding = world.user(VIEWER), world.finding()

    res = world.decide(user, finding, outcome)

    assert res.status_code == 403
    assert world.writes(finding) == NOTHING


@pytest.mark.parametrize(
    "held",
    [
        {**FINDINGS_ONLY, "cases.write": True},
        {**FINDINGS_ONLY, "cases.assign": True},
        FINDINGS_ONLY,
    ],
)
def test_take_case_needs_both_case_permissions_before_any_write(world, held):
    user, finding = world.user(world.role(held)), world.finding()

    res = world.decide(user, finding, "take_case")

    assert res.status_code == 403
    assert "cases." in res.json()["detail"]
    assert world.writes(finding) == NOTHING


def test_findings_write_alone_still_marks_noise_and_sends_to_triage(world):
    user, finding = world.user(world.role(FINDINGS_ONLY)), world.finding()

    assert world.decide(user, finding, "noise").status_code == 200
    assert world.decide(user, finding, "triage").status_code == 200


def test_replay_is_refused_to_a_role_that_may_not_decide(world):
    analyst, viewer = world.user(), world.user(VIEWER)
    finding, request_id = world.finding(), uuid.uuid4().hex
    world.decide(analyst, finding, "noise", request_id)

    assert world.decide(viewer, finding, "noise", request_id).status_code == 403


@pytest.mark.parametrize("outcome", ["noise", "triage", "take_case"])
def test_a_refused_trail_entry_writes_nothing(world, monkeypatch, outcome):
    from core.audit import trail

    real = trail.append

    def refuse_the_decision(session, payload, **kwargs):
        if payload[trail.ACTION] == trail.ACTION_FINDING_DECIDED:
            raise RuntimeError("the trail refused this entry")
        return real(session, payload, **kwargs)

    monkeypatch.setattr(trail, "append", refuse_the_decision)
    user, finding = world.user(), world.finding()
    cases_before = world.read("SELECT count(*) FROM cases")

    res = world.decide(user, finding, outcome)

    assert res.status_code == 500
    assert world.writes(finding) == NOTHING
    assert world.read("SELECT count(*) FROM cases") == cases_before


@pytest.mark.parametrize("outcome", ["noise", "triage", "take_case"])
def test_a_paused_trail_answers_503_and_writes_nothing(world, monkeypatch, outcome):
    from datetime import datetime, timedelta, timezone

    from core.audit import trail

    def paused(_session):
        raise trail.Paused(1, datetime.now(timezone.utc) + timedelta(seconds=30))

    monkeypatch.setattr(trail, "_gate", paused)
    user, finding = world.user(), world.finding()

    res = world.decide(user, finding, outcome)

    assert res.status_code == 503
    assert res.json()["code"] == "AUDIT_PAUSED"
    assert world.writes(finding) == NOTHING


def test_undo_clears_the_mark_and_lands_on_the_trail(world):
    user, finding = world.user(), world.finding()
    world.decide(user, finding, "noise", reason="known scanner")

    res = world.client().delete(
        f"/api/findings/{finding}/noise", headers={"x-test-user": user}
    )

    assert res.status_code == 200
    assert world.read(
        "SELECT noise_marked_at, noise_marked_by, noise_reason FROM findings"
        " WHERE finding_id = :f",
        f=finding,
    ) == [(None, None, None)]
    actions = [e["payload"]["action"] for e in world.entries(finding)]
    assert actions == ["finding.decided", "finding.noise_cleared"]
    assert world.entries(finding)[-1]["sentence"] == (
        f"Cleared the noise mark on {finding}"
    )
    assert world.entries(finding)[-1]["payload"]["actor"] == user


def test_undo_with_no_mark_records_nothing(world):
    user, finding = world.user(), world.finding()

    res = world.client().delete(
        f"/api/findings/{finding}/noise", headers={"x-test-user": user}
    )

    assert res.status_code == 200
    assert world.entries(finding) == []


def test_a_refused_undo_entry_leaves_the_mark(world, monkeypatch):
    from core.audit import trail

    user, finding = world.user(), world.finding()
    world.decide(user, finding, "noise")

    def refuse(*_args, **_kwargs):
        raise RuntimeError("the trail refused this entry")

    monkeypatch.setattr(trail, "append", refuse)
    res = world.client().delete(
        f"/api/findings/{finding}/noise", headers={"x-test-user": user}
    )

    assert res.status_code == 500
    assert world.writes(finding)["noise"] is not None


def test_the_replaced_routes_are_gone(world):
    user, finding = world.user(), world.finding()
    client = world.client()
    headers = {"x-test-user": user}

    assert (
        client.post(f"/api/findings/{finding}/noise", headers=headers).status_code
        == 405
    )
    assert (
        client.post(f"/api/findings/{finding}/intake", headers=headers).status_code
        == 404
    )


def test_an_unknown_finding_and_an_unknown_outcome_are_refused(world):
    user = world.user()

    assert world.decide(user, "f-missing", "noise").status_code == 404
    assert world.decide(user, world.finding(), "close").status_code == 422
    blank = world.client().post(
        f"/api/findings/{world.finding()}/decide",
        headers={"x-test-user": user},
        json={"outcome": "noise", "request_id": ""},
    )
    assert blank.status_code == 422


def test_the_replay_lookup_reads_the_index(world):
    from sqlalchemy import text

    from core.storage.connection import get_db_manager

    with get_db_manager().session_scope() as session:
        session.execute(text("SET LOCAL enable_seqscan = off"))
        plan = session.execute(text("""
                EXPLAIN SELECT payload FROM public.audit_trail
                WHERE payload->>'action' = 'finding.decided'
                  AND payload->>'finding_id' = 'f'
                  AND payload->>'request_id' = 'r'
                """)).all()
    assert "uq_audit_trail_finding_decision" in " ".join(row[0] for row in plan)
