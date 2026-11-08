"""Assist approvals on a real Postgres (#4068): a second carrier of the same
approval promotes nothing and opens no second PR, the request/approve/decline
path writes real approval and audit rows, and a second sweep asks nothing new.

Runs when VIGIL_TEST_PG_DSN names a throwaway database, e.g.
postgresql://postgres:pg@127.0.0.1:55468/postgres."""

import base64
import json
import os
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
import pytest
from sqlalchemy import func, select, text

from core.detections.inventory import readers, rollout, rollout_approval
from core.response.approval_service import ActionStatus, ApprovalService
from core.storage.models import (
    ApprovalAction,
    ConfigAuditLog,
    DetectionRule,
    DetectionRuleStage,
    DetectionRuleVersion,
)

DSN = os.environ.get("VIGIL_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="VIGIL_TEST_PG_DSN not set")
TARGET = {"git_url": "https://github.com/acme/detections.git", "subdirectory": "rules"}
SINCE = []
SECONDS = 60  # a deadlock fails a test in this long, not never


class FakeGitHub:
    """The REST calls a deploy PR makes, answered in memory at the HTTP layer
    (httpx's transport), so the real PR code runs end to end."""

    def __init__(self):
        self.lock = threading.Lock()
        self.files = {"main": {}}
        self.pulls = []
        self.opened = 0  # POST /pulls that made a PR

    def __call__(self, request):
        with self.lock:
            return self._answer(request)

    def _answer(self, request):
        path = request.url.path.removeprefix("/repos/acme/detections")
        query = dict(request.url.params)
        body = json.loads(request.content or b"{}")

        def reply(code, obj):
            return httpx.Response(code, json=obj, request=request)

        def view(pr):
            repo = {"full_name": "acme/detections"}
            return {**pr, "head": {"ref": pr["branch"], "repo": repo}}

        method = request.method
        if path == "" and method == "GET":
            return reply(200, {"default_branch": "main"})
        if path == "/pulls" and method == "GET":
            head = query.get("head")
            return reply(
                200,
                [
                    view(p)
                    for p in self.pulls
                    if not head or head == f"acme:{p['branch']}"
                ],
            )
        if path == "/pulls" and method == "POST":
            if any(p["branch"] == body["head"] for p in self.pulls):
                return reply(422, {"message": "A pull request already exists"})
            n = len(self.pulls) + 1
            self.pulls.append(
                {
                    "number": n,
                    "state": "open",
                    "merged_at": None,
                    "branch": body["head"],
                    "html_url": f"https://github.example/acme/detections/pull/{n}",
                }
            )
            self.opened += 1
            return reply(201, view(self.pulls[-1]))
        if path.startswith("/git/ref/heads/") and method == "GET":
            branch = path.removeprefix("/git/ref/heads/")
            if branch not in self.files:
                return reply(404, {"message": "Not Found"})
            return reply(200, {"object": {"sha": f"sha-{branch}"}})
        if path == "/git/refs" and method == "POST":
            branch = body["ref"].removeprefix("refs/heads/")
            if branch in self.files:
                return reply(422, {"message": "Reference already exists"})
            self.files[branch] = dict(self.files["main"])
            return reply(201, {})
        if path.startswith("/contents/"):
            name = path.removeprefix("/contents/")
            if method == "GET":
                data = self.files.get(query.get("ref", "main"), {}).get(name)
                if data is None:
                    return reply(404, {"message": "Not Found"})
                return reply(
                    200, {"sha": "blob", "content": base64.b64encode(data).decode()}
                )
            if method == "PUT":
                self.files[body["branch"]][name] = base64.b64decode(body["content"])
                return reply(201, {})
        return reply(404, {"message": f"unhandled {method} {path}"})


@pytest.fixture
def db(monkeypatch):
    url = urlparse(DSN)
    monkeypatch.setenv("POSTGRES_HOST", url.hostname)
    monkeypatch.setenv("POSTGRES_PORT", str(url.port))
    monkeypatch.setenv("POSTGRES_DB", url.path.lstrip("/"))
    monkeypatch.setenv("POSTGRES_USER", url.username)
    monkeypatch.setenv("POSTGRES_PASSWORD", url.password)
    monkeypatch.setenv("POSTGRES_SSL_MODE", "disable")
    # Server-side limits, so a lock wait that never ends is an error here.
    monkeypatch.setenv("PGOPTIONS", "-c lock_timeout=20000 -c statement_timeout=40000")
    from core.storage import connection

    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    manager = connection.get_db_manager()
    manager.initialize(force=True)
    manager.create_tables()
    with manager.session_scope() as session:
        session.execute(
            text(
                "DELETE FROM approval_actions "
                "WHERE action_type IN ('promote_rule', 'deploy_rule')"
            )
        )
        session.execute(text("DELETE FROM system_config WHERE key='autonomy.ceiling'"))
    with manager.session_scope() as session:
        # Audit rows outlive a test and ids can be reused, so each test reads
        # only what was written since it began.
        SINCE.append(session.execute(text("SELECT now()")).scalar())
    yield manager
    manager._engine.dispose()
    connection.DatabaseManager._instance = None


@pytest.fixture
def mine():
    """The rule ids this test seeded: the sweep sees no others."""
    return set()


@pytest.fixture
def github(monkeypatch, db, mine):
    """A fake GitHub behind the real PR code; only these rules are swept."""
    fake = FakeGitHub()
    real_client = httpx.Client

    def client(*args, **kwargs):
        return real_client(*args, transport=httpx.MockTransport(fake), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setattr(rollout, "_deploy_targets", lambda: {"elastic": TARGET})
    real_copies = rollout._copies
    monkeypatch.setattr(
        rollout, "_copies", lambda: [c for c in real_copies() if c.rule_id in mine]
    )
    import core.secrets

    monkeypatch.setattr(core.secrets, "get_secret", lambda name: "token")
    # Earlier tests' live rows (the last 24 hours) must not cap this one's request.
    limits = replace(rollout.DEFAULT_LIMITS, live_per_day=10**6)
    monkeypatch.setattr(rollout, "read_limits", lambda: limits)
    return fake


def _seed(db, mine, stage="shadow"):
    now = datetime.now(timezone.utc)
    week = [(now - timedelta(days=i)).date().isoformat() for i in range(30)]
    key = f"assist-{uuid.uuid4().hex[:12]}"
    with db.session_scope() as session:
        rule = DetectionRule(
            source=readers.OVERLAY_SOURCE,
            native_key=key,
            name=f"rule {key}",
            language="elastic",
            latest_version=2,
            path=f"{key}.toml",
        )
        session.add(rule)
        session.flush()
        session.add(
            DetectionRuleVersion(
                rule_id=rule.id,
                version=2,
                fingerprint="f",
                content={"_content": "[rule]\nname = 'x'\n"},
                checks={
                    "runs": {"days": {d: {"pass": 4, "fail": 0} for d in week}},
                    "shadow": {"days": {d: 3 for d in week}},
                    "sees": {"result": "pass"},
                    "lint": {"result": "pass", "findings": []},
                },
            )
        )
        session.add(
            DetectionRuleStage(
                rule_id=rule.id,
                version=2,
                stage=stage,
                actor="system",
                reason="new",
                at=now - timedelta(days=8 if stage == "shadow" else 1),
            )
        )
        mine.add(rule.id)
        return rule.id


def _sweep():
    report = rollout.RolloutReport()
    rollout.sweep(datetime.now(timezone.utc), report)
    return report


def _pending(rule_id, kind):
    return [
        a
        for a in ApprovalService().list_actions(status=ActionStatus.PENDING)
        if a.action_type == kind and a.parameters.get("rule_id") == rule_id
    ]


def _approved(rule_id, who="dana", kind="promote_rule"):
    (row,) = _pending(rule_id, kind)
    return ApprovalService().approve_action(row.action_id, approved_by=who)


def _live_rows(db, rule_id):
    with db.session_scope() as session:
        return session.scalar(
            select(func.count())
            .select_from(DetectionRuleStage)
            .where(
                DetectionRuleStage.rule_id == rule_id,
                DetectionRuleStage.stage == "live",
            )
        )


def _status(db, rule_id):
    with db.session_scope() as session:
        rows = session.scalars(
            select(ApprovalAction).where(
                ApprovalAction.action_type.in_(("promote_rule", "deploy_rule"))
            )
        ).all()
        return [r.status for r in rows if r.parameters["rule_id"] == rule_id]


def _audit(db, rule_id, event):
    """(who, reason) of each rule_approval entry, from the trail where the
    database has one and from config_audit_log where it does not."""
    with db.session_scope() as session:
        if session.execute(text("SELECT to_regclass('public.audit_trail')")).scalar():
            rows = session.execute(
                text(
                    "SELECT payload FROM audit_trail "
                    "WHERE payload->>'config_key' = 'rule_approval' "
                    "AND payload->>'verb' = :event AND occurred_at >= :since "
                    "ORDER BY seq"
                ),
                {"event": event, "since": SINCE[-1]},
            ).all()
            return [
                (p["actor"], p.get("change_reason"))
                for (p,) in rows
                if p["new_value"]["rule_id"] == rule_id
            ]
        rows = session.scalars(
            select(ConfigAuditLog)
            .where(
                ConfigAuditLog.config_key == "rule_approval",
                ConfigAuditLog.action == event,
                ConfigAuditLog.timestamp >= SINCE[-1].replace(tzinfo=None),
            )
            .order_by(ConfigAuditLog.id)
        ).all()
        return [
            (r.changed_by, r.change_reason)
            for r in rows
            if r.new_value["rule_id"] == rule_id
        ]


def _deploy_recorded(db, rule_id):
    with db.session_scope() as session:
        checks = session.scalar(
            select(DetectionRuleVersion.checks).where(
                DetectionRuleVersion.rule_id == rule_id
            )
        )
        return (checks or {}).get("deploy") or {}


def _together(*works):
    """Run each at once; a call that outlasts SECONDS is a deadlock."""
    gate = threading.Barrier(len(works))
    errors = []

    def go(work):
        try:
            gate.wait()
            work()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(w,), daemon=True) for w in works]
    for t in threads:
        t.start()
    for t in threads:
        t.join(SECONDS)
    assert not any(t.is_alive() for t in threads), "deadlocked"
    assert errors == []


def test_a_second_sweep_asks_nothing_new(db, github, mine):
    rid = _seed(db, mine)
    assert _sweep().asked == [f"{rid} v2"]
    assert _sweep().asked == []
    (row,) = _pending(rid, "promote_rule")
    assert "days in shadow" in row.reason
    assert _live_rows(db, rid) == 0 and github.opened == 0
    assert [who for who, _ in _audit(db, rid, "request")] == ["system"]


def test_approve_writes_the_stage_pr_and_audit_rows_with_who(db, github, mine):
    rid = _seed(db, mine)
    _sweep()
    rollout_approval.on_decided(_approved(rid), "dana")
    with db.session_scope() as session:
        stage = session.scalars(
            select(DetectionRuleStage).where(
                DetectionRuleStage.rule_id == rid, DetectionRuleStage.stage == "live"
            )
        ).one()
        assert stage.actor == "dana" and stage.reason.startswith("approved by dana")
    assert _status(db, rid) == ["executed"]
    assert github.opened == 1
    assert _deploy_recorded(db, rid)["pr_url"].endswith("/pull/1")
    assert [who for who, _ in _audit(db, rid, "approve")] == ["dana"]
    _sweep()  # nothing is repeated
    assert github.opened == 1 and _live_rows(db, rid) == 1


def test_decline_keeps_shadow_and_records_who_and_why(db, github, mine):
    rid = _seed(db, mine)
    _sweep()
    (pending,) = _pending(rid, "promote_rule")
    declined = ApprovalService().reject_action(pending.action_id, "too noisy", "dana")
    rollout_approval.on_decided(declined, "dana", "too noisy")
    with db.session_scope() as session:
        row = session.get(ApprovalAction, pending.action_id)
        assert (row.status, row.rejection_reason) == ("rejected", "too noisy")
    assert _live_rows(db, rid) == 0 and github.opened == 0
    assert _audit(db, rid, "decline") == [("dana", "too noisy")]
    assert rid not in {int(r.split()[0]) for r in _sweep().asked}


def test_a_failed_pr_leaves_the_rule_live_and_the_sweep_retries_only_the_pr(
    db, github, mine, monkeypatch
):
    rid = _seed(db, mine)
    _sweep()
    approved = _approved(rid)
    github.fail = True
    real_answer = github._answer

    def failing(request):
        if (
            github.fail
            and request.method == "POST"
            and request.url.path.endswith("/pulls")
        ):
            return httpx.Response(502, json={"message": "bad gateway"})
        return real_answer(request)

    monkeypatch.setattr(github, "_answer", failing)
    rollout_approval.on_decided(approved, "dana")
    assert _live_rows(db, rid) == 1 and github.opened == 0
    assert _status(db, rid) == ["approved"]  # the PR is still owed
    assert "error" in _deploy_recorded(db, rid)
    github.fail = False
    _sweep()  # under Assist: finds the approved row, opens only the PR
    assert _live_rows(db, rid) == 1 and github.opened == 1
    assert _status(db, rid) == ["executed"]


def test_a_crash_after_the_stage_commit_is_finished_by_the_next_sweep(
    db, github, mine, monkeypatch
):
    rid = _seed(db, mine)
    _sweep()
    approved = _approved(rid)

    def dies(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(rollout_approval, "_open_pr", dies)
    with pytest.raises(KeyboardInterrupt):
        rollout_approval.carry_out(approved, rollout.RolloutReport())
    monkeypatch.undo()  # the process is back; the fakes below are reinstalled
    assert _live_rows(db, rid) == 1 and github.opened == 0
    assert _status(db, rid) == ["approved"]


@pytest.mark.parametrize("round_", range(15))
def test_approve_racing_the_sweep_promotes_and_opens_one_pr(
    db, github, mine, monkeypatch, round_
):
    rid = _seed(db, mine)
    _sweep()
    approved = _approved(rid)
    real_copies = rollout._copies

    def slow_copies():  # widens the window between reading a copy and acting on it
        copies = real_copies()
        time.sleep(0.01)
        return copies

    monkeypatch.setattr(rollout, "_copies", slow_copies)
    _together(
        lambda: rollout_approval.on_decided(approved, "dana"),
        lambda: rollout_approval.carry_out_approved(rollout.RolloutReport()),
    )
    assert _live_rows(db, rid) == 1
    assert len(github.pulls) == 1
    assert _status(db, rid) == ["executed"]
    assert len(_audit(db, rid, "approve")) == 1


# --- the manual deploy -------------------------------------------------------


def test_a_manual_deploy_asks_and_approving_it_opens_the_pr(db, github, mine):
    rid = _seed(db, mine, stage="live")
    result = rollout.deploy_overlay_only([rid], actor="sam")
    assert [a["rule_id"] for a in result["asked"]] == [rid] and github.opened == 0
    assert [who for who, _ in _audit(db, rid, "request")] == ["sam"]
    rollout.deploy_overlay_only([rid], actor="sam")  # asking again: same request
    assert len(_pending(rid, "deploy_rule")) == 1
    rollout_approval.on_decided(_approved(rid, "dana", "deploy_rule"), "dana")
    assert github.opened == 1 and _status(db, rid) == ["executed"]
    assert [who for who, _ in _audit(db, rid, "approve")] == ["dana"]


def test_a_manual_deploy_by_an_approver_opens_the_pr_at_once(db, github, mine):
    rid = _seed(db, mine, stage="live")
    result = rollout.deploy_overlay_only([rid], actor="dana", approver=True)
    assert result["asked"] == [] and github.opened == 1
