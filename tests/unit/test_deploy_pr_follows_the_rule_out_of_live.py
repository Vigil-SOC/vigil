"""A rule that leaves live takes its deploy PR with it: the sweep closes the PR
with a comment naming where the rule went, who moved it and why; a re-promotion
of that version reopens it. Run against an in-memory GitHub over httpx, with
every call captured."""

import json
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import core.secrets
from core.detections.inventory import rollout, rollout_approval

TOKEN = "ghp_secret_never_logged"
REPO = "acme/detections"
TARGET = {"git_url": f"https://github.com/{REPO}.git", "subdirectory": "rules"}
NOW = datetime(2026, 11, 30, 8, 0, tzinfo=timezone.utc)


class FakeGitHub:
    """The REST calls the deploy, close and reopen make, on one repo."""

    def __init__(self):
        self.pulls = []
        self.comments = {}  # number -> [body]
        self.calls = []  # (method, path, json body)
        self.fail = None  # (method, path regex) -> answer 500

    def pr(self, branch, state="open", merged=False):
        number = len(self.pulls) + 1
        self.pulls.append(
            {
                "number": number,
                "state": state,
                "merged_at": "2026-11-20T00:00:00Z" if merged else None,
                "html_url": f"https://github.com/{REPO}/pull/{number}",
                "head": {"ref": branch, "repo": {"full_name": REPO}},
            }
        )
        return number

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        body = json.loads(request.content or b"null")
        self.calls.append((method, path, body))
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if self.fail and method == self.fail[0] and re.search(self.fail[1], path):
            return httpx.Response(500, json={"message": "boom"})
        base = f"/repos/{REPO}"
        if method == "GET" and path == f"{base}/pulls":
            branch = request.url.params["head"].partition(":")[2]
            want = request.url.params.get("state", "open")
            return httpx.Response(
                200,
                json=[
                    p
                    for p in self.pulls
                    if p["head"]["ref"] == branch and want in ("all", p["state"])
                ],
            )
        m = re.fullmatch(rf"{base}/pulls/(\d+)", path)
        if m and method == "GET":
            return httpx.Response(200, json=self.pulls[int(m[1]) - 1])
        if m and method == "PATCH":
            self.pulls[int(m[1]) - 1]["state"] = body["state"]
            return httpx.Response(200, json=self.pulls[int(m[1]) - 1])
        m = re.fullmatch(rf"{base}/issues/(\d+)/comments", path)
        if m and method == "GET":
            said = self.comments.get(int(m[1]), [])
            return httpx.Response(200, json=[{"body": b} for b in said])
        if m and method == "POST":
            self.comments.setdefault(int(m[1]), []).append(body["body"])
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": f"no route {method} {path}"})

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != "GET"]


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    real = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real(transport=httpx.MockTransport(fake.handle), **kw),
    )
    return fake


@pytest.fixture
def recorded(github, monkeypatch):
    """The DB stubbed: what the close and the deploy record per (rule, version)."""
    out = {}

    class _Db:
        @contextmanager
        def session_scope(self):
            yield object()

    monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(rollout.exceptions, "listed", lambda _s, _rid: [])
    monkeypatch.setattr(rollout, "_stopped", lambda: False)
    monkeypatch.setattr(core.secrets, "get_secret", lambda key, default=None: TOKEN)
    monkeypatch.setattr(
        rollout,
        "_record_withdrawn",
        lambda w, change: out.__setitem__((w.rule_id, w.version), change),
    )

    def record(c, outcome, *_):
        out[(c.rule_id, c.version)] = outcome
        return outcome

    monkeypatch.setattr(rollout, "_record_deploy", record)
    return out


def _left(number, stage, actor, reason, version=1):
    deploy = {
        "version": version,
        "at": "2026-11-15T07:57:38+00:00",
        "pr_url": f"https://github.com/{REPO}/pull/{number}",
    }
    return rollout.Withdrawal(
        rule_id=16733,
        version=version,
        name="Lab e2e beacon e2evil",
        language="elastic",
        deploy=deploy,
        stage=stage,
        actor=actor,
        reason=reason,
    )


def _withdraw(items, target=TARGET):
    report = rollout.RolloutReport()
    rollout._withdraw(items, {"elastic": target}, NOW, report)
    return report


def test_a_persons_revert_closes_the_pr_saying_who_and_why(github, recorded):
    n = github.pr("vigil/rule-16733-v1")
    report = _withdraw([_left(n, "shadow", "lab_admin", "another look first")])
    assert github.pulls[n - 1]["state"] == "closed"
    assert github.comments[n] == [
        "Closed by Vigil: rule Lab e2e beacon e2evil v1 is no longer live. "
        "lab_admin moved it to shadow: another look first"
    ]
    assert recorded[(16733, 1)] == {
        "withdrawn": {
            "at": NOW.isoformat(),
            "stage": "shadow",
            "actor": "lab_admin",
            "reason": "another look first",
            "closed": True,
        }
    }
    assert report.deploys == [f"16733 v1: closed https://github.com/{REPO}/pull/{n}"]


def test_vigil_turning_it_off_for_lint_closes_the_pr(github, recorded):
    n = github.pr("vigil/rule-16733-v1")
    _withdraw([_left(n, "off", "system", "fails lint: keyed to ip 10.20.30.40")])
    assert github.pulls[n - 1]["state"] == "closed"
    assert github.comments[n] == [
        "Closed by Vigil: rule Lab e2e beacon e2evil v1 is no longer live. "
        "Vigil moved it to off: fails lint: keyed to ip 10.20.30.40"
    ]
    assert github.writes() == [
        ("POST", f"/repos/{REPO}/issues/{n}/comments"),
        ("PATCH", f"/repos/{REPO}/pulls/{n}"),
    ]


def test_a_merged_pr_is_left_as_it_is_and_recorded_so(github, recorded):
    n = github.pr("vigil/rule-16733-v1", state="closed", merged=True)
    _withdraw([_left(n, "off", "system", "fails lint")])
    assert github.writes() == []
    withdrawn = recorded[(16733, 1)]["withdrawn"]
    assert withdrawn["closed"] is False
    assert withdrawn["note"] == "merged before the rule left live"


def test_a_pr_a_person_closed_stays_closed_and_is_never_reopened(github, recorded):
    n = github.pr("vigil/rule-16733-v1", state="closed")
    _withdraw([_left(n, "shadow", "lab_admin", "x")])
    assert github.writes() == []
    withdrawn = recorded[(16733, 1)]["withdrawn"]
    assert (withdrawn["closed"], withdrawn["note"]) == (False, "already closed")
    # Live again: the sweep has nothing to do for this version's PR.
    deploy = {**_left(n, "live", "system", "").deploy, "withdrawn": withdrawn}
    assert rollout._deploy_due(_copy(deploy)) is False


def test_a_failed_close_is_retried_and_comments_once(github, recorded):
    n = github.pr("vigil/rule-16733-v1")
    github.fail = ("PATCH", rf"/pulls/{n}$")
    report = _withdraw([_left(n, "shadow", "lab_admin", "x")])
    assert github.pulls[n - 1]["state"] == "open"
    assert "500" in recorded[(16733, 1)]["withdraw_error"]
    assert "failed" in report.deploys[0]
    github.fail = None
    _withdraw([_left(n, "shadow", "lab_admin", "x")])
    assert github.pulls[n - 1]["state"] == "closed"
    assert len(github.comments[n]) == 1
    assert recorded[(16733, 1)]["withdrawn"]["closed"] is True


def test_nothing_is_closed_while_stopped(github, recorded, monkeypatch):
    n = github.pr("vigil/rule-16733-v1")
    monkeypatch.setattr(rollout, "_stopped", lambda: True)
    _withdraw([_left(n, "shadow", "lab_admin", "x")])
    assert github.calls == [] and recorded == {}


def test_a_non_github_target_gets_no_request(github, recorded):
    n = github.pr("vigil/rule-16733-v1")
    _withdraw(
        [_left(n, "shadow", "lab_admin", "x")],
        {"git_url": "https://gitlab.com/acme/detections.git"},
    )
    assert github.calls == []
    assert "only GitHub" in recorded[(16733, 1)]["withdraw_error"]


def test_a_pr_outside_the_targets_repo_gets_no_request(github, recorded):
    w = _left(1, "shadow", "lab_admin", "x")
    w.deploy["pr_url"] = "https://github.com/someone-else/rules/pull/1"
    _withdraw([w])
    assert github.calls == []
    assert recorded[(16733, 1)]["withdrawn"]["note"] == (
        "not in the deploy target's repo"
    )


def _copy(deploy):
    return rollout.Copy(
        rule_id=16733,
        version=1,
        name="Lab e2e beacon e2evil",
        language="elastic",
        stage="live",
        stage_at=NOW,
        checks={"deploy": deploy, "lint": {"result": "pass"}},
        rule={},
        key="labe2e-beacon-e2evil",
        toml='[rule]\nname = "Lab e2e beacon e2evil"\n',
        path="elastic/labe2e-beacon-e2evil.toml",
    )


def test_a_re_promotion_reopens_the_pr_vigil_closed(github, recorded):
    n = github.pr("vigil/rule-16733-v1")
    _withdraw([_left(n, "shadow", "lab_admin", "another look first")])
    deploy = {**_left(n, "live", "system", "").deploy, **recorded[(16733, 1)]}
    copy = _copy(deploy)
    assert rollout._deploy_due(copy) is True
    rollout._deploy([copy], {"elastic": TARGET}, NOW, rollout.RolloutReport())
    assert github.pulls[n - 1]["state"] == "open"
    assert github.comments[n][-1] == (
        "Reopened by Vigil: rule Lab e2e beacon e2evil v1 is live again."
    )
    assert len(github.pulls) == 1  # the same PR, no second one
    out = recorded[(16733, 1)]
    assert out["pr_url"] == f"https://github.com/{REPO}/pull/{n}"
    assert out["reopened"] is True and "withdrawn" not in out
    # Recorded so, a later sweep has nothing more to do for it.
    assert rollout._deploy_due(_copy(out)) is False


def test_a_retried_reopen_comments_once(github, recorded):
    n = github.pr("vigil/rule-16733-v1")
    _withdraw([_left(n, "shadow", "lab_admin", "x")])
    copy = _copy({**_left(n, "live", "system", "").deploy, **recorded[(16733, 1)]})
    github.fail = ("PATCH", rf"/pulls/{n}$")
    rollout._deploy([copy], {"elastic": TARGET}, NOW, rollout.RolloutReport())
    assert github.pulls[n - 1]["state"] == "closed"
    github.fail = None
    rollout._deploy([copy], {"elastic": TARGET}, NOW, rollout.RolloutReport())
    assert github.pulls[n - 1]["state"] == "open"
    assert (
        github.comments[n].count(
            "Reopened by Vigil: rule Lab e2e beacon e2evil v1 is live again."
        )
        == 1
    )


@pytest.mark.parametrize("asks", [False, True])
def test_the_sweep_closes_left_live_prs_after_its_deploys(monkeypatch, asks):
    """Under Assist too: closing a PR asks no one; the rule already left live."""
    calls = []

    class _S:
        def scalar(self, _q):
            return 0

    class _Db:
        @contextmanager
        def session_scope(self):
            yield _S()

    monkeypatch.setattr(rollout, "_copies", lambda: [])
    monkeypatch.setattr(rollout, "handed_off", lambda ids: set())
    monkeypatch.setattr(rollout, "_stopped", lambda: False)
    monkeypatch.setattr(rollout, "_asks_first", lambda: asks)
    monkeypatch.setattr(
        rollout_approval, "carry_out_approved", lambda report: calls.append("deploy")
    )
    monkeypatch.setattr(rollout, "_deploy_targets", lambda: {"elastic": TARGET})
    monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(rollout, "_deploy", lambda *a, **k: calls.append("deploy"))
    left = [_left(1, "off", "system", "fails lint")]
    monkeypatch.setattr(rollout, "_left_live", lambda: left)
    monkeypatch.setattr(
        rollout, "_withdraw", lambda items, *a: calls.append(("withdraw", items))
    )
    rollout.sweep(NOW, rollout.RolloutReport())
    assert calls == ["deploy", ("withdraw", left)]


DSN = os.environ.get("VIGIL_TEST_POSTGRES_DSN")


@pytest.mark.skipif(not DSN, reason="VIGIL_TEST_POSTGRES_DSN not set")
def test_left_live_reads_open_prs_of_rules_no_longer_live(monkeypatch):
    """On a real Postgres: which versions' PRs the sweep would close, and the
    stage row (latest by time) that says who moved the rule and why."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from core.detections.inventory import readers
    from core.storage.models import (
        Base,
        DetectionRule,
        DetectionRuleStage,
        DetectionRuleVersion,
    )

    engine = create_engine(DSN)
    tables = [
        DetectionRule.__table__,
        DetectionRuleVersion.__table__,
        DetectionRuleStage.__table__,
    ]
    # Works on any throwaway database, empty or a copy with rules of its own.
    Base.metadata.create_all(engine, tables=tables)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE detection_rules RESTART IDENTITY CASCADE"))
    factory = sessionmaker(bind=engine)

    class Manager:
        @contextmanager
        def session_scope(self):
            session = factory()
            try:
                yield session
                session.commit()
            finally:
                session.close()

    monkeypatch.setattr(rollout, "get_db_manager", lambda: Manager())
    t0 = datetime(2026, 11, 8, tzinfo=timezone.utc)

    def pr(n, version, **extra):
        return {
            "deploy": {
                "version": version,
                "at": "x",
                "pr_url": f"https://github.com/{REPO}/pull/{n}",
                **extra,
            }
        }

    rules = {
        # name: (versions {n: checks}, stage rows [(version, stage, actor, reason, at)])
        "reverted": (
            {1: pr(1, 1)},
            [
                (1, "shadow", "system", "new", 0),
                (1, "live", "system", "week", 7),
                (1, "shadow", "ann", "look again", 8),
            ],
        ),
        "linted-off": (
            {1: pr(2, 1), 2: {}},
            [
                (1, "shadow", "system", "new", 0),
                (1, "live", "system", "week", 7),
                (2, "off", "system", "fails lint", 9),
            ],
        ),
        # Written later but stamped earlier: the live row by time is current.
        "still-live": (
            {1: pr(3, 1)},
            [
                (1, "shadow", "system", "new", 0),
                (1, "live", "system", "week", 7),
                (1, "shadow", "ann", "late write", 6),
            ],
        ),
        "done": (
            {1: pr(4, 1, withdrawn={"closed": True})},
            [(1, "live", "system", "week", 7), (1, "shadow", "ann", "x", 8)],
        ),
        "superseded": (
            {1: pr(5, 1, superseded_by="u"), 2: {}},
            [
                (1, "live", "system", "week", 7),
                (2, "shadow", "system", "new version", 8),
            ],
        ),
        "no-pr": (
            {1: {"deploy": {"version": 1, "at": "x", "in_repo": "r on main"}}},
            [(1, "live", "system", "week", 7), (1, "off", "system", "removed", 8)],
        ),
    }
    ids = {}
    with Manager().session_scope() as s:
        for name, (versions, stage_rows) in rules.items():
            rule = DetectionRule(
                source=readers.OVERLAY_SOURCE,
                native_key=name,
                name=name,
                language="elastic",
                status="active",
                latest_version=max(versions),
                path=f"elastic/{name}.toml",
            )
            s.add(rule)
            s.flush()
            ids[name] = rule.id
            for v, checks in versions.items():
                s.add(
                    DetectionRuleVersion(
                        rule_id=rule.id,
                        version=v,
                        fingerprint=f"{name}-{v}",
                        content={},
                        checks=checks,
                    )
                )
            for v, stage, actor, reason, day in stage_rows:
                s.add(
                    DetectionRuleStage(
                        rule_id=rule.id,
                        version=v,
                        stage=stage,
                        actor=actor,
                        reason=reason,
                        proof={},
                        at=t0 + timedelta(days=day),
                    )
                )
                s.flush()
    got = {
        (w.name, w.version): (w.stage, w.actor, w.reason, w.deploy["pr_url"][-1])
        for w in rollout._left_live()
    }
    assert got == {
        ("reverted", 1): ("shadow", "ann", "look again", "1"),
        ("linted-off", 1): ("off", "system", "fails lint", "2"),
    }
