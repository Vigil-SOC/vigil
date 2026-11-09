"""A live copy whose deploy PR was closed unmerged can be deployed again (#4123):
the deploy reads the recorded PR's state from GitHub, not only that one is
recorded. Closed unmerged: a new PR opens for the current version, the new URL
is recorded, and the trail row names the person and the closed PR. Open or
merged: still skipped, so no duplicate. Run against an in-memory GitHub over
httpx (branches, files and PRs), with the real deploy writer."""

import base64
import json
import os
import re
from contextlib import contextmanager
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

import httpx
import pytest

import core.secrets
from core.detections.inventory import rollout

TOKEN = "ghp_secret_never_logged"
REPO = "acme/detections"
TARGET = {"git_url": f"https://github.com/{REPO}.git", "subdirectory": "rules"}
XML = (
    '<group name="vigil,"><rule id="100777" level="7"><if_sid>5710</if_sid>'
    "<match>canary</match><description>Canary login</description></rule></group>"
)


class FakeGitHub:
    """One repo: its branches' files, and its pull requests, as GitHub keeps them
    (one open PR per head; a closed one doesn't stop a new one)."""

    def __init__(self):
        self.files = {"main": {}}  # branch -> path -> bytes
        self.pulls = []
        self.calls = []
        self.bodies = {}  # PR number -> body
        self.newest_first = True  # how GitHub lists PRs
        self.refuse_create = False  # 422, as if another click opened it first

    def pr(self, branch, state="open", merged=False):
        self.files.setdefault(branch, dict(self.files["main"]))
        number = len(self.pulls) + 1
        self.pulls.append(
            {
                "number": number,
                "state": "closed" if merged else state,
                "merged_at": "2026-11-08T00:00:00Z" if merged else None,
                "html_url": f"https://github.com/{REPO}/pull/{number}",
                "head": {"ref": branch, "repo": {"full_name": REPO}},
            }
        )
        return self.pulls[-1]["html_url"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, unquote(request.url.path)
        body = json.loads(request.content or b"null")
        self.calls.append((method, path))
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        base = f"/repos/{REPO}"
        if method == "GET" and path == base:
            return httpx.Response(200, json={"default_branch": "main"})
        if path == f"{base}/pulls" and method == "GET":
            branch = request.url.params["head"].partition(":")[2]
            want = request.url.params.get("state", "open")
            found = [
                p
                for p in self.pulls
                if p["head"]["ref"] == branch and want in ("all", p["state"])
            ]
            return httpx.Response(200, json=found[::-1] if self.newest_first else found)
        if path == f"{base}/pulls" and method == "POST":
            if self.refuse_create:
                return httpx.Response(422, json={"message": "A pull request exists"})
            if any(
                p["head"]["ref"] == body["head"] and p["state"] == "open"
                for p in self.pulls
            ):
                return httpx.Response(422, json={"message": "A pull request exists"})
            self.pr(body["head"])
            self.bodies[self.pulls[-1]["number"]] = body["body"]
            return httpx.Response(201, json=self.pulls[-1])
        m = re.fullmatch(rf"{base}/pulls/(\d+)", path)
        if m and method == "GET":
            return httpx.Response(200, json=self.pulls[int(m[1]) - 1])
        m = re.fullmatch(rf"{base}/git/ref/heads/(.+)", path)
        if m and method == "GET":
            if m[1] not in self.files:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": f"sha-{m[1]}"}})
        if path == f"{base}/git/refs" and method == "POST":
            branch = body["ref"].removeprefix("refs/heads/")
            if branch in self.files:
                return httpx.Response(422, json={"message": "Reference already exists"})
            self.files[branch] = dict(self.files["main"])
            return httpx.Response(201, json={})
        m = re.fullmatch(rf"{base}/contents/(.+)", path)
        if m and method == "GET":
            data = self.files.get(request.url.params.get("ref", "main"), {}).get(m[1])
            if data is None:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(
                200,
                json={
                    "sha": f"blob-{len(data)}",
                    "content": base64.b64encode(data).decode(),
                },
            )
        if m and method == "PUT":
            self.files[body["branch"]][m[1]] = base64.b64decode(body["content"])
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": f"no route {method} {path}"})

    def opened(self):
        return [p for p in self.pulls if p["state"] == "open"]


@pytest.fixture
def github(monkeypatch):
    fake = FakeGitHub()
    real = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real(transport=httpx.MockTransport(fake.handle), **kw),
    )
    monkeypatch.setattr(core.secrets, "get_secret", lambda key, default=None: TOKEN)
    monkeypatch.setattr(rollout, "_stopped", lambda: False)
    monkeypatch.setattr(rollout, "_asks_first", lambda: False)
    monkeypatch.setattr(rollout, "_deploy_targets", lambda: {"wazuh": TARGET})
    return fake


def _copy(rule_id=17101, version=1, deploy=None):
    return rollout.Copy(
        rule_id=rule_id,
        version=version,
        name="Canary login",
        language="wazuh",
        stage="live",
        stage_at=rollout.NO_STAGE_AT,
        checks={"lint": {"result": "pass"}, **({"deploy": deploy} if deploy else {})},
        rule={},
        key="100777",
        toml=XML,
        path="wazuh/vigil_100777.xml",
    )


@pytest.fixture
def inventory(github, monkeypatch):
    """The DB stubbed around the real writer: the copies, and what each deploy
    and PR-state reading would record."""
    box = {"copies": [], "recorded": {}, "states": {}}

    class _Db:
        @contextmanager
        def session_scope(self):
            yield SimpleNamespace(scalars=lambda *_: [])

    monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(rollout, "_copies", lambda: box["copies"])
    monkeypatch.setattr(rollout, "handed_off", lambda ids: set())
    monkeypatch.setattr(rollout.exceptions, "listed", lambda _s, _rid: [])

    def record(c, outcome, actor="system"):
        box["recorded"][c.rule_id] = (outcome, actor)
        return outcome

    monkeypatch.setattr(rollout, "_record_deploy", record)
    monkeypatch.setattr(
        rollout,
        "_record_pr_state",
        lambda c, url, state, now: box["states"].__setitem__(c.rule_id, state),
    )
    return box


def _branch(copy):
    return f"vigil/rule-{copy.rule_id}-v{copy.version}"


def test_a_pr_closed_unmerged_is_deployed_again_as_a_new_pr(github, inventory):
    copy = _copy()
    closed = github.pr(_branch(copy), state="closed")
    copy.checks["deploy"] = {"version": 1, "pr_url": closed}
    inventory["copies"] = [copy]
    out = rollout.deploy_overlay_only([copy.rule_id], actor="alice")
    assert out["skipped"] == [] and out["failed"] == []
    ((opened),) = out["opened"]
    assert opened["pr_url"] == f"https://github.com/{REPO}/pull/2"  # a new PR
    assert [p["html_url"] for p in github.opened()] == [opened["pr_url"]]
    assert github.pulls[0]["state"] == "closed"  # the closed one is left as it was
    outcome, actor = inventory["recorded"][copy.rule_id]
    assert (outcome["pr_url"], outcome["again_after"], actor) == (
        opened["pr_url"],
        closed,
        "alice",
    )
    assert inventory["states"][copy.rule_id] == "closed"  # read from GitHub now
    assert github.files[_branch(copy)]["rules/vigil_100777.xml"] == XML.encode()
    # A GitHub reviewer can tell it's a redeploy, and of which PR.
    assert github.bodies[2].startswith(f"Deployed again by a person in Vigil: {closed}")


def test_a_deleted_branch_is_made_again(github, inventory):
    copy = _copy()
    closed = github.pr(_branch(copy), state="closed")
    del github.files[_branch(copy)]  # the reviewer deleted the branch on close
    copy.checks["deploy"] = {"version": 1, "pr_url": closed}
    inventory["copies"] = [copy]
    out = rollout.deploy_overlay_only([copy.rule_id], actor="alice")
    assert out["opened"][0]["pr_url"].endswith("/pull/2")
    assert ("POST", f"/repos/{REPO}/git/refs") in github.calls


@pytest.mark.parametrize(
    "state, merged, reason",
    [
        ("open", False, "its pull request is already open"),
        ("closed", True, "its pull request was merged"),
    ],
)
def test_an_open_or_merged_pr_is_still_skipped(
    github, inventory, state, merged, reason
):
    copy = _copy()
    url = github.pr(_branch(copy), state=state, merged=merged)
    copy.checks["deploy"] = {"version": 1, "pr_url": url}
    inventory["copies"] = [copy]
    out = rollout.deploy_overlay_only([copy.rule_id], actor="alice")
    assert out["opened"] == [] and len(github.pulls) == 1
    assert out["skipped"] == [{"rule_id": copy.rule_id, "reason": f"{reason}: {url}"}]


def test_a_pr_state_that_cant_be_read_opens_nothing(github, inventory):
    copy = _copy()
    copy.checks["deploy"] = {
        "version": 1,
        "pr_url": f"https://github.com/{REPO}/pull/9",
    }
    inventory["copies"] = [copy]
    out = rollout.deploy_overlay_only([copy.rule_id], actor="alice")
    assert out["opened"] == [] and github.pulls == []
    assert "could not be checked" in out["skipped"][0]["reason"]


def test_the_sweep_never_redeploys_a_pr_a_person_closed(github, inventory):
    # Only a person's Deploy again does: the sweep leaves a closed PR as their answer.
    copy = _copy()
    closed = github.pr(_branch(copy), state="closed")
    copy.checks["deploy"] = {"version": 1, "pr_url": closed}
    out = rollout._deploy(
        [copy], {"wazuh": TARGET}, rollout.NO_STAGE_AT, rollout.RolloutReport()
    )
    assert out == {} and len(github.pulls) == 1


def test_under_assist_deploying_again_waits_for_an_approval(
    github, inventory, monkeypatch
):
    from core.detections.inventory import rollout_approval

    copy = _copy()
    copy.checks["deploy"] = {
        "version": 1,
        "pr_url": github.pr(_branch(copy), state="closed"),
    }
    inventory["copies"] = [copy]
    monkeypatch.setattr(rollout, "_asks_first", lambda: True)
    asked = []
    monkeypatch.setattr(
        rollout_approval,
        "request_deploy",
        lambda c, who, again_after=None: asked.append(again_after) or "act-1",
    )
    out = rollout.deploy_overlay_only([copy.rule_id], actor="bob")
    assert out["asked"] == [{"rule_id": copy.rule_id, "action_id": "act-1"}]
    assert asked == [copy.checks["deploy"]["pr_url"]]  # the request names the closed PR
    assert len(github.pulls) == 1  # nothing opened before the approval


# --- real Postgres: a clone of an upgraded lab DB, the config trail in use -------

DSN = os.environ.get("VIGIL_TEST_PG_DSN")


@pytest.fixture
def pg(monkeypatch):
    """The live database the DSN names (export POSTGRES_* to match), with one
    live Wazuh copy added and removed again afterwards."""
    if not DSN:
        pytest.skip("VIGIL_TEST_PG_DSN not set")
    from sqlalchemy import delete

    from core.storage import connection
    from core.storage.models import (
        DetectionRule,
        DetectionRuleStage,
        DetectionRuleVersion,
    )

    url = urlparse(DSN)
    for key, value in {
        "POSTGRES_HOST": url.hostname,
        "POSTGRES_PORT": str(url.port),
        "POSTGRES_DB": url.path.lstrip("/"),
        "POSTGRES_USER": url.username,
        "POSTGRES_PASSWORD": url.password,
        "POSTGRES_SSL_MODE": "disable",
    }.items():
        monkeypatch.setenv(key, value)
    connection.DatabaseManager._instance = None
    monkeypatch.setattr(connection, "_db_manager", None)
    manager = connection.get_db_manager()
    manager.initialize(force=True)
    manager.create_tables()  # a create_tables DB has no audit_trail: the legacy log
    made = []
    yield manager, made
    with manager.session_scope() as session:
        for model in (DetectionRuleStage, DetectionRuleVersion):
            session.execute(delete(model).where(model.rule_id.in_(made)))
        session.execute(delete(DetectionRule).where(DetectionRule.id.in_(made)))
    manager._engine.dispose()
    connection.DatabaseManager._instance = None


def _has_trail(session):
    from sqlalchemy import text

    return bool(session.execute(text("select to_regclass('audit_trail')")).scalar())


def _audit_rows(session, rule_id, after):
    """The overlay_deploy trail rows for the rule after ``after``: the audit
    trail when in use (an upgraded DB), else the legacy config_audit_log (a
    create_tables one), each read as the trail's payload."""
    from sqlalchemy import text

    if not _has_trail(session):
        rows = session.execute(
            text(
                "select new_value, changed_by from config_audit_log "
                "where id > :after and config_key = 'overlay_deploy' "
                "and (new_value->>'rule_id')::int = :rid order by id"
            ),
            {"after": after, "rid": rule_id},
        ).all()
        return [{"new_value": v, "actor": who} for v, who in rows]
    trail = (
        session.execute(
            text(
                "select payload from audit_trail where seq > :after "
                "and payload->>'config_key' = 'overlay_deploy' "
                "and (payload->'new_value'->>'rule_id')::int = :rid order by seq"
            ),
            {"after": after, "rid": rule_id},
        )
        .scalars()
        .all()
    )
    return [json.loads(p) if isinstance(p, str) else p for p in trail]


def test_deploying_again_records_the_new_pr_and_the_person_on_the_trail(pg, github):
    from sqlalchemy import select, text

    from core.storage.models import (
        DetectionRule,
        DetectionRuleStage,
        DetectionRuleVersion,
    )

    manager, made = pg
    with manager.session_scope() as session:
        after = session.execute(
            text(
                "select coalesce(max(seq), 0) from audit_trail"
                if _has_trail(session)
                else "select coalesce(max(id), 0) from config_audit_log"
            )
        ).scalar()
        rule = DetectionRule(
            source="repo:overlay",
            native_key="100777",
            name="Canary login",
            language="wazuh",
            status="active",
            latest_version=1,
            path="wazuh/vigil_100777.xml",
        )
        session.add(rule)
        session.flush()
        made.append(rule.id)
        closed = github.pr(f"vigil/rule-{rule.id}-v1", state="closed")
        session.add(
            DetectionRuleVersion(
                rule_id=rule.id,
                version=1,
                fingerprint="f4123",
                content={"_content": XML},
                checks={
                    "lint": {"result": "pass"},
                    "deploy": {"version": 1, "pr_url": closed, "pr_state": "closed"},
                },
            )
        )
        session.add(
            DetectionRuleStage(
                rule_id=rule.id,
                version=1,
                stage="live",
                proof={},
                actor="system",
                reason="lab",
            )
        )
        rid = rule.id
    out = rollout.deploy_overlay_only([rid], actor="alice", approver=True)
    assert out["failed"] == [] and out["skipped"] == [], out
    new = out["opened"][0]["pr_url"]
    assert new != closed and new.endswith("/pull/2")
    with manager.session_scope() as session:
        deploy = session.scalar(
            select(DetectionRuleVersion.checks).where(
                DetectionRuleVersion.rule_id == rid
            )
        )["deploy"]
        rows = _audit_rows(session, rid, after)
    assert (deploy["pr_url"], deploy["again_after"]) == (new, closed)
    assert "pr_state" not in deploy  # the new PR's state is read on the next sweep
    ((row,),) = [rows]
    assert row["new_value"]["again_after"] == closed
    assert row["new_value"]["pr_url"] == new
    assert "alice" in json.dumps(row)  # the trail names the person


def test_the_race_fallback_takes_only_a_standing_pr_whatever_the_order(
    github, inventory, monkeypatch
):
    # Another click opens PR #2 just as this one posts; GitHub lists oldest first.
    copy = _copy()
    closed = github.pr(_branch(copy), state="closed")
    copy.checks["deploy"] = {"version": 1, "pr_url": closed}
    inventory["copies"] = [copy]
    github.newest_first = False
    handle = github.handle

    def raced(request):
        if request.method == "POST" and request.url.path.endswith("/pulls"):
            github.pr(_branch(copy))  # the other click's PR lands now
            github.refuse_create = True
        return handle(request)

    github.handle = raced
    monkeypatch.setattr(rollout.time, "sleep", lambda s: None)
    out = rollout.deploy_overlay_only([copy.rule_id], actor="alice")
    assert out["opened"][0]["pr_url"].endswith("/pull/2")  # not the closed #1


@pytest.mark.parametrize(
    "merged, reason",
    [
        (True, "its pull request was merged"),
        (False, "its pull request is already open"),
    ],
)
def test_a_stale_closed_reading_with_a_pr_standing_opens_nothing(
    github, inventory, merged, reason
):
    # The record names closed PR #1; the branch has since got PR #2, merged or open.
    copy = _copy()
    closed = github.pr(_branch(copy), state="closed")
    copy.checks["deploy"] = {"version": 1, "pr_url": closed}
    inventory["copies"] = [copy]
    handle = github.handle
    standing = []

    def later(request):
        # GitHub's state of #1 is read as closed; #2 lands before the deploy looks.
        if request.method == "GET" and request.url.path.endswith("/pulls/1"):
            response = handle(request)
            standing.append(github.pr(_branch(copy), state="open", merged=merged))
            return response
        return handle(request)

    github.handle = later
    out = rollout.deploy_overlay_only([copy.rule_id], actor="alice")
    assert out["opened"] == [] and len(github.pulls) == 2
    assert out["skipped"] == [
        {"rule_id": copy.rule_id, "reason": f"{reason}: {standing[0]}"}
    ]
    outcome, _ = inventory["recorded"][copy.rule_id]
    assert outcome["pr_url"] == standing[0] and "again_after" not in outcome
    assert outcome["pr_state"] == ("merged" if merged else "open")


def test_the_approval_for_a_redeploy_names_the_closed_pr(monkeypatch):
    from core.detections.inventory import rollout_approval

    made = {}

    class _Service:
        def list_actions(self, **kw):
            return []

        def create_action(self, **kw):
            made.update(kw)
            return SimpleNamespace(action_id="act-9")

    monkeypatch.setattr(rollout_approval, "ApprovalService", _Service)
    monkeypatch.setattr(rollout_approval, "_audit", lambda *a, **k: None)
    url = f"https://github.com/{REPO}/pull/57"
    assert rollout_approval.request_deploy(_copy(), "bob", again_after=url) == "act-9"
    assert made["title"] == "Deploy Canary login again"
    assert url in made["description"]
    assert "closed without merging" in made["description"]
    assert "has no deploy PR" not in made["description"]
    assert made["parameters"]["again_after"] == url
