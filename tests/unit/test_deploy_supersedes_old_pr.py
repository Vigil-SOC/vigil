"""When a rule's new version gets its deploy PR, Vigil closes the PR it opened
for an older version of that rule, so a reviewer can't merge the stale rule.
Run against an in-memory GitHub (the lab shim's endpoints) over httpx."""

import base64
import json
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
import pytest

import core.secrets
from core.detections.inventory import rollout

TOKEN = "ghp_secret_never_logged"
REPO = "acme/detections"
TARGET = {"git_url": f"https://github.com/{REPO}.git", "subdirectory": "rules"}


class FakeGitHub:
    """The REST calls _open_pr and _supersede make, on one repo."""

    def __init__(self):
        self.branches = {"main": "sha-main"}
        self.files = {}  # (branch, path) -> bytes
        self.pulls = []  # dicts as GitHub returns them
        self.comments = {}  # number -> [body]
        self.calls = []
        self.fail = None  # (method, path regex) -> answer 500

    def pr(self, branch, state="open", merged=False, head_repo=REPO, title="t"):
        number = len(self.pulls) + 1
        self.pulls.append(
            {
                "number": number,
                "state": state,
                "merged_at": "2026-11-01T00:00:00Z" if merged else None,
                "title": title,
                "html_url": f"https://github.com/{REPO}/pull/{number}",
                "head": {"ref": branch, "repo": {"full_name": head_repo}},
            }
        )
        self.branches.setdefault(branch, f"sha-{branch}")
        return number

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.calls.append((method, path, request.headers.get("authorization")))
        if self.fail and method == self.fail[0] and re.search(self.fail[1], path):
            return httpx.Response(500, json={"message": "boom"})
        base = f"/repos/{REPO}"
        params = request.url.params
        body = json.loads(request.content or b"{}")
        if method == "GET" and path == f"{base}/pulls":
            owner, _, branch = params["head"].partition(":")
            want = params.get("state", "open")
            found = [
                p
                for p in self.pulls
                if owner == REPO.split("/")[0]
                and p["head"]["ref"] == branch
                and (want == "all" or p["state"] == want)
            ]
            return httpx.Response(200, json=found)
        if method == "GET" and path == base:
            return httpx.Response(200, json={"default_branch": "main"})
        m = re.fullmatch(rf"{base}/contents/(.+)", path)
        if m and method == "GET":
            data = self.files.get((params["ref"], m[1]))
            if data is None:
                return httpx.Response(404, json={"message": "Not Found"})
            content = base64.b64encode(data).decode()
            return httpx.Response(200, json={"sha": "blob", "content": content})
        if m and method == "PUT":
            self.files[(body["branch"], m[1])] = base64.b64decode(body["content"])
            return httpx.Response(201, json={})
        m = re.fullmatch(rf"{base}/git/ref/heads/(.+)", path)
        if m and method == "GET":
            if m[1] not in self.branches:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json={"object": {"sha": self.branches[m[1]]}})
        if method == "POST" and path == f"{base}/git/refs":
            self.branches[body["ref"].removeprefix("refs/heads/")] = body["sha"]
            return httpx.Response(201, json={})
        if method == "POST" and path == f"{base}/pulls":
            number = self.pr(body["head"], title=body["title"])
            return httpx.Response(201, json=self.pulls[number - 1])
        m = re.fullmatch(rf"{base}/issues/(\d+)/comments", path)
        if m and method == "GET":
            said = self.comments.get(int(m[1]), [])
            return httpx.Response(200, json=[{"body": b} for b in said])
        if m and method == "POST":
            self.comments.setdefault(int(m[1]), []).append(body["body"])
            return httpx.Response(201, json={})
        m = re.fullmatch(rf"{base}/pulls/(\d+)", path)
        if m and method == "PATCH":
            self.pulls[int(m[1]) - 1]["state"] = body["state"]
            return httpx.Response(200, json=self.pulls[int(m[1]) - 1])
        return httpx.Response(404, json={"message": f"no route {method} {path}"})


def _copy(version, rule_id=7, deploy=None):
    """A live copy; ``deploy`` is what an earlier sweep recorded on this version."""
    return rollout.Copy(
        rule_id=rule_id,
        version=version,
        name="Certutil download",
        language="elastic",
        stage="live",
        stage_at=datetime.now(timezone.utc),
        checks={"deploy": deploy} if deploy else {},
        rule={},
        key="key-7",
        toml=f'[rule]\nname = "v{version}"\n',
        path="certutil.toml",
    )


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
def deploy(github, monkeypatch):
    """_deploy with the DB stubbed; what it records per (rule, version)."""
    recorded = {}

    class _Db:
        @contextmanager
        def session_scope(self):
            yield object()

    monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(rollout.exceptions, "listed", lambda _s, _rid: [])
    monkeypatch.setattr(rollout, "_stopped", lambda: False)
    monkeypatch.setattr(rollout, "_asks_first", lambda: False)  # Act
    monkeypatch.setattr(core.secrets, "get_secret", lambda key, default=None: TOKEN)

    def record(c, out, *_):  # *_: the actor, where _deploy passes one
        recorded[(c.rule_id, c.version)] = out
        return out

    monkeypatch.setattr(rollout, "_record_deploy", record)
    monkeypatch.setattr(
        rollout,
        "_record_superseded",
        lambda c, old, new, now: recorded.__setitem__(
            (c.rule_id, old["version"]), {"pr_url": old["pr_url"], "superseded_by": new}
        ),
    )

    def run(copy, target=TARGET):
        rollout._deploy(
            [copy],
            {"elastic": target},
            datetime.now(timezone.utc),
            rollout.RolloutReport(),
        )
        return recorded

    return run


def _state(github, number):
    return github.pulls[number - 1]["state"]


def test_the_new_versions_pr_closes_the_old_ones(github, deploy):
    v1 = github.pr("vigil/rule-7-v1")
    recorded = deploy(_copy(2))
    new = recorded[(7, 2)]["pr_url"]
    assert _state(github, v1) == "closed"
    assert github.comments[v1] == ["Superseded by #2 (rule Certutil download v2)"]
    assert recorded[(7, 1)] == {
        "pr_url": f"https://github.com/{REPO}/pull/{v1}",
        "superseded_by": new,
    }


def test_every_older_open_version_is_closed(github, deploy):
    v1, v2 = github.pr("vigil/rule-7-v1"), github.pr("vigil/rule-7-v2")
    deploy(_copy(3))
    assert _state(github, v1) == _state(github, v2) == "closed"


def test_a_merged_pr_is_never_touched(github, deploy):
    v1 = github.pr("vigil/rule-7-v1", state="closed", merged=True)
    deploy(_copy(2))
    assert v1 not in github.comments
    assert not any(m == "PATCH" for m, *_ in github.calls)


def test_prs_vigil_did_not_open_are_never_touched(github, deploy):
    mine = github.pr("feature/tune-certutil")  # a person's branch
    other_rule = github.pr("vigil/rule-99-v1")
    fork = github.pr("vigil/rule-7-v1", head_repo="someone/detections")
    deploy(_copy(2))
    for n in (mine, other_rule, fork):
        assert _state(github, n) == "open" and n not in github.comments


def test_a_first_version_looks_for_nothing_older(github, deploy):
    deploy(_copy(1))
    assert [p for m, p, _ in github.calls if "comments" in p or m == "PATCH"] == []


def test_a_new_pr_a_person_closed_supersedes_nothing(github, deploy):
    v1 = github.pr("vigil/rule-7-v1")
    github.pr("vigil/rule-7-v2", state="closed")  # a reviewer rejected v2
    deploy(_copy(2))
    assert _state(github, v1) == "open"


def test_a_failed_close_keeps_the_new_pr_and_is_retried_without_a_second_comment(
    github, deploy
):
    v1 = github.pr("vigil/rule-7-v1")
    github.fail = ("PATCH", r"/pulls/\d+$")
    first = dict(deploy(_copy(2))[(7, 2)])
    # The v2 PR is open: its record says so (the Stop dialog shows it opened),
    # and the failed close sits beside it rather than in place of it.
    assert first["pr_url"].endswith("/pull/2")
    assert "error" not in first
    assert "500" in first["supersede_error"]
    assert _state(github, v1) == "open"
    # Next sweep, from what was recorded: v2's PR is reused and v1 gets closed.
    github.fail = None
    recorded = deploy(_copy(2, deploy=first))
    assert recorded[(7, 2)]["pr_url"] == first["pr_url"]
    assert "supersede_error" not in recorded[(7, 2)]
    assert _state(github, v1) == "closed"
    assert github.comments[v1] == ["Superseded by #2 (rule Certutil download v2)"]
    assert sum(1 for p in github.pulls if p["head"]["ref"] == "vigil/rule-7-v2") == 1


def test_a_failed_comment_is_retried_and_posted_once(github, deploy):
    v1 = github.pr("vigil/rule-7-v1")
    github.fail = ("POST", r"/comments$")
    first = dict(deploy(_copy(2))[(7, 2)])
    assert first["pr_url"] and "500" in first["supersede_error"]
    assert _state(github, v1) == "open" and v1 not in github.comments
    github.fail = None
    deploy(_copy(2, deploy=first))
    assert github.comments[v1] == ["Superseded by #2 (rule Certutil download v2)"]
    assert _state(github, v1) == "closed"


def test_a_recorded_pr_with_nothing_left_to_close_is_not_retried(github, deploy):
    done = {"version": 2, "at": "x", "pr_url": f"https://github.com/{REPO}/pull/9"}
    deploy(_copy(2, deploy=done))
    assert github.calls == []


def test_one_failed_close_does_not_stop_the_others(github, deploy):
    v1, v2 = github.pr("vigil/rule-7-v1"), github.pr("vigil/rule-7-v2")
    github.fail = ("PATCH", rf"/pulls/{v1}$")
    recorded = deploy(_copy(3))
    assert _state(github, v1) == "open" and _state(github, v2) == "closed"
    assert recorded[(7, 3)]["pr_url"].endswith("/pull/3")
    assert f"/pulls/{v1}" in recorded[(7, 3)]["supersede_error"]
    assert recorded[(7, 2)]["superseded_by"] == recorded[(7, 3)]["pr_url"]
    assert (7, 1) not in recorded


def test_a_version_already_in_the_repo_closes_the_older_prs_too(github, deploy):
    v1 = github.pr("vigil/rule-7-v1")
    github.files[("main", "rules/certutil.toml")] = _copy(2).toml.encode()
    recorded = deploy(_copy(2))
    assert recorded[(7, 2)]["in_repo"] == "rules/certutil.toml on main"
    assert _state(github, v1) == "closed"
    assert github.comments[v1] == [
        "Superseded: rule Certutil download v2 is already in rules/certutil.toml on main"
    ]
    assert recorded[(7, 1)]["superseded_by"] == "rules/certutil.toml on main"
    # No branch or PR is made for a version that is already there.
    assert not any(p["head"]["ref"] == "vigil/rule-7-v2" for p in github.pulls)


def test_the_token_goes_only_in_the_header_and_never_to_a_log(github, deploy, caplog):
    github.pr("vigil/rule-7-v1")
    github.fail = ("POST", r"/comments$")
    with caplog.at_level(logging.DEBUG):
        recorded = deploy(_copy(2))
    assert {auth for *_, auth in github.calls} == {f"Bearer {TOKEN}"}
    assert TOKEN not in caplog.text
    assert TOKEN not in json.dumps(list(recorded.values()))


def test_a_non_github_target_gets_no_request(github, deploy):
    recorded = deploy(_copy(2), {"git_url": "https://gitlab.com/acme/detections.git"})
    assert github.calls == []
    assert "only GitHub" in recorded[(7, 2)]["error"]


def test_the_old_version_keeps_its_deploy_record_and_says_what_replaced_it(
    monkeypatch,
):
    from core.detections.inventory import sync

    old_checks = {"runs": {"result": "pass"}, "deploy": {"version": 1, "at": "x"}}
    written = []

    class _S:
        def scalar(self, _stmt):
            return old_checks

    class _Db:
        @contextmanager
        def session_scope(self):
            yield _S()

    monkeypatch.setattr(rollout, "get_db_manager", lambda: _Db())
    monkeypatch.setattr(sync, "_write_checks", lambda _s, rows: written.extend(rows))
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    rollout._record_superseded(
        _copy(2),
        {"version": 1, "pr_url": "https://gh/pull/1"},
        "https://gh/pull/2",
        now,
    )
    ((rule_id, version, checks),) = written
    assert (rule_id, version) == (7, 1)
    assert checks["runs"] == {"result": "pass"}
    assert checks["deploy"] == {
        "version": 1,
        "at": "x",
        "pr_url": "https://gh/pull/1",
        "superseded_by": "https://gh/pull/2",
        "superseded_at": now.isoformat(),
    }
